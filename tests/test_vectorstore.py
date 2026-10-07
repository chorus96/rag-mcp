"""Vectorstore: 명명된 벡터 구성과 하이브리드/밀집 질의 라우팅.

희소 계층을 모킹하므로 FastEmbed가 필요 없습니다. 하이브리드가 prefetch + RRF
결합을 사용하는지, 희소 벡터가 없거나 hybrid=False이면 밀집 전용을 사용하는지,
모델이 없으면 희소 헬퍼가 None으로 대체되는지 확인합니다."""

import sys
from pathlib import Path

from qdrant_client.models import FusionQuery, SparseVector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vectorstore  # noqa: E402


class _FakeResult:
    def __init__(self, points):
        self.points = points


class _FakeClient:
    def __init__(self):
        self.calls: list[dict] = []

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResult(["point"])


def test_named_vectors_dense_only():
    assert vectorstore.named_vectors([0.1, 0.2], None) == {vectorstore.DENSE: [0.1, 0.2]}


def test_named_vectors_includes_sparse():
    sv = SparseVector(indices=[1, 5], values=[0.5, 0.9])
    nv = vectorstore.named_vectors([0.1], sv)
    assert nv[vectorstore.DENSE] == [0.1]
    assert nv[vectorstore.SPARSE] is sv


def test_query_dense_fallback_when_no_sparse(monkeypatch):
    monkeypatch.setattr(vectorstore, "embed_query_sparse", lambda _t: None)
    client = _FakeClient()
    points = vectorstore.query(client, "kb", [0.1, 0.2], "q", query_filter=None, limit=5)
    assert points == ["point"]
    call = client.calls[0]
    assert call["using"] == vectorstore.DENSE
    assert call["query"] == [0.1, 0.2]
    assert "prefetch" not in call


def test_query_hybrid_uses_prefetch_and_fusion(monkeypatch):
    monkeypatch.setattr(
        vectorstore, "embed_query_sparse",
        lambda _t: SparseVector(indices=[1], values=[0.9]),
    )
    client = _FakeClient()
    vectorstore.query(client, "kb", [0.1], "CrashLoopBackOff", query_filter=None, limit=7)
    call = client.calls[0]
    assert len(call["prefetch"]) == 2                      # 밀집 + 희소
    assert isinstance(call["query"], FusionQuery)          # RRF 결합
    assert call["limit"] == 7


def test_query_hybrid_false_forces_dense(monkeypatch):
    # hybrid=False이면 희소 벡터를 아예 계산하지 않아야 합니다.
    def _boom(_t):
        raise AssertionError("sparse must not be embedded when hybrid=False")

    monkeypatch.setattr(vectorstore, "embed_query_sparse", _boom)
    client = _FakeClient()
    vectorstore.query(client, "kb", [0.1], "q", query_filter=None, limit=3, hybrid=False)
    assert client.calls[0]["using"] == vectorstore.DENSE


def test_sparse_helpers_degrade_without_model(monkeypatch):
    monkeypatch.setattr(vectorstore, "_load_bm25", lambda: None)
    assert vectorstore.sparse_available() is False
    assert vectorstore.embed_documents_sparse(["a", "b"]) == [None, None]
    assert vectorstore.embed_query_sparse("q") is None
