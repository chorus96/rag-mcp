"""tools/vectorstore.py 테스트 — 벡터 구성과 질의 경로.

확인하는 것
  - 명명된 벡터: 밀집 벡터만, 또는 밀집 + 희소 벡터를 올바른 이름으로 담음
  - 질의 경로: 희소 벡터가 있으면 prefetch 두 개 + RRF 결합(하이브리드),
    없거나 hybrid=False 이면 밀집 검색만 함 (hybrid=False 이면 희소 벡터를 계산조차 안 함)
  - 대체 동작: BM25 모델을 불러올 수 없으면 희소 관련 함수가 None 을 돌려줌. FastEmbed(선택 의존성)가 없는데
    Qdrant/bm25 를 고르면 설치 안내를 남기고 밀집 전용으로 동작함

방법
  - 희소 벡터 계산과 Qdrant 클라이언트를 가짜로 바꿔, FastEmbed나 Qdrant 없이 실행합니다.
"""

import sys
from pathlib import Path

from qdrant_client.models import FusionQuery, SparseVector

# 소스가 tools/ 에 있으므로 import 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import vectorstore  # noqa: E402


# --- 테스트 도우미 ------------------------------------------------------------

class _FakeResult:
    def __init__(self, points):
        self.points = points


class _FakeClient:
    def __init__(self):
        self.calls: list[dict] = []

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResult(["point"])


# --- 명명된 벡터 --------------------------------------------------------------

def test_named_vectors_dense_only():
    assert vectorstore.named_vectors([0.1, 0.2], None) == {vectorstore.DENSE: [0.1, 0.2]}


def test_named_vectors_includes_sparse():
    sv = SparseVector(indices=[1, 5], values=[0.5, 0.9])
    nv = vectorstore.named_vectors([0.1], sv)
    assert nv[vectorstore.DENSE] == [0.1]
    assert nv[vectorstore.SPARSE] is sv


# --- 질의 경로 (하이브리드 / 밀집) --------------------------------------------

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


# --- BM25 모델이 없을 때 ------------------------------------------------------

def test_sparse_helpers_degrade_without_model(monkeypatch):
    monkeypatch.setattr(vectorstore, "_load_bm25", lambda: None)
    assert vectorstore.sparse_available() is False
    assert vectorstore.embed_documents_sparse(["a", "b"]) == [None, None]
    assert vectorstore.embed_query_sparse("q") is None


def test_fastembed_model_without_fastembed_falls_back_to_dense(monkeypatch, caplog):
    # FastEmbed는 선택 의존성입니다. 없는데 Qdrant/bm25 를 고르면 설치 안내를 남기고 밀집 전용으로 동작합니다.
    monkeypatch.setitem(sys.modules, "fastembed", None)  # import fastembed -> ImportError
    monkeypatch.setattr(vectorstore, "BM25_MODEL", "Qdrant/bm25")
    monkeypatch.setattr(vectorstore, "_HYBRID_REQUESTED", True)
    monkeypatch.setattr(vectorstore, "_bm25", None)
    monkeypatch.setattr(vectorstore, "_bm25_loaded", False)
    assert vectorstore.sparse_available() is False
    assert "requirements-fastembed.txt" in caplog.text
