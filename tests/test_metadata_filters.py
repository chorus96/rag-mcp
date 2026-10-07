"""tools/server.py, tools/capture.py 테스트 — 검색 필터.

확인하는 것
  - Qdrant 필터 구성: doc_type / component 는 하드 필터, cluster 는 소프트 필터
  - 소프트 필터 재시도: 같은 클러스터 결과가 비면 클러스터 조건만 빼고(하드 필터는 유지)
    전체 범위로 다시 검색하고 `cluster_narrowed: false` 를 보고함
  - 단축 도구: search_incidents / search_runbooks 가 doc_type 을 고정함
  - 반복 장애 확인(capture.find_similar)도 같은 소프트 필터 규칙을 따르며,
    min_score 미만 결과는 "없음"으로 취급함

왜 중요한가
  "이 클러스터에서 전에 이런 일이 있었나?"를 물을 때, 클러스터 필터가 다른 클러스터의 선례를
  가려서는 안 됩니다. `cluster_narrowed` 로 "이 클러스터에는 없음"과 "어디에도 없음"을
  구분할 수 있어야 합니다.

방법
  - 임베딩과 vectorstore.query 를 가짜로 바꿔, 호출마다 넘어온 필터를 기록하고 미리 정한
    결과를 돌려줍니다. 실제 Qdrant에는 접근하지 않습니다.
"""

import sys
import types
from pathlib import Path

# 소스가 tools/ 에 있으므로 import 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import capture  # noqa: E402
import server  # noqa: E402


# --- 테스트 도우미 ------------------------------------------------------------

def _point(payload=None, score=0.9):
    return types.SimpleNamespace(payload=payload or {}, score=score, id="pt")


class _QueryRecorder:
    """`vectorstore.query`를 대신합니다: 호출마다 필터를 기록하고, 미리 설정한
    포인트 목록을 호출마다 하나씩 반환합니다 (다 쓰면 빈 목록)."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, client, collection, vector, query_text, *, query_filter, limit, **kw):
        self.calls.append((query_filter, limit))
        return self.results.pop(0) if self.results else []


def _conditions(query_filter):
    if query_filter is None:
        return {}
    return {c.key: c.match.value for c in query_filter.must}


def _patch_search(monkeypatch, results):
    monkeypatch.setattr(server.embeddings, "embed", lambda q, role: [0.1, 0.2])
    rec = _QueryRecorder(results)
    monkeypatch.setattr(server.vectorstore, "query", rec)
    return rec


# --- server._search — 필터 구성과 소프트 필터 재시도 --------------------------

def test_search_no_filters_no_query_filter(monkeypatch):
    rec = _patch_search(monkeypatch, [[_point()]])
    out = server._search("crashloop", None, None, None, 5)
    assert rec.calls[0][0] is None
    assert out["status"] == "ok"
    assert out["count"] == 1
    assert out["cluster"] is None
    assert out["cluster_narrowed"] is None
    assert out["component"] is None


def test_doc_type_and_component_build_hard_filters(monkeypatch):
    rec = _patch_search(monkeypatch, [[_point()]])
    server._search("volume stuck", "incident", None, "longhorn", 5)
    conds = _conditions(rec.calls[0][0])
    assert conds == {"doc_type": "incident", "component": "longhorn"}


def test_cluster_scoped_results_stay_narrowed(monkeypatch):
    rec = _patch_search(monkeypatch, [[_point()]])
    out = server._search("volume stuck", "incident", "prod-01", None, 5)
    assert len(rec.calls) == 1
    conds = _conditions(rec.calls[0][0])
    assert conds["cluster"] == "prod-01"
    assert conds["doc_type"] == "incident"
    assert out["cluster_narrowed"] is True
    assert "note" not in out


def test_cluster_empty_falls_back_fleet_wide(monkeypatch):
    # 범위를 한정한 호출이 빈 결과를 반환 -> 클러스터 조건 없이 재시도하고 그 사실을 알림.
    rec = _patch_search(monkeypatch, [[], [_point(payload={"title": "prior"})]])
    out = server._search("volume stuck", "incident", "prod-02", None, 5)
    assert len(rec.calls) == 2
    assert _conditions(rec.calls[0][0])["cluster"] == "prod-02"
    assert "cluster" not in _conditions(rec.calls[1][0])
    assert out["count"] == 1
    assert out["cluster_narrowed"] is False
    assert "note" in out


def test_fallback_keeps_hard_filters(monkeypatch):
    # 재시도는 클러스터 조건만 뺍니다. doc_type/component는 유지됩니다.
    rec = _patch_search(monkeypatch, [[], [_point()]])
    server._search("volume stuck", "runbook", "prod-01", "longhorn", 5)
    assert len(rec.calls) == 2
    assert _conditions(rec.calls[1][0]) == {"doc_type": "runbook", "component": "longhorn"}


def test_component_empty_does_not_retry(monkeypatch):
    # component는 하드 필터입니다: 빈 결과는 그대로 비어 있음 — 대체 검색 없음.
    rec = _patch_search(monkeypatch, [[]])
    out = server._search("volume stuck", None, None, "longhorn", 5)
    assert len(rec.calls) == 1
    assert out["count"] == 0
    assert out["cluster_narrowed"] is None


# --- 단축 도구 (search_incidents / search_runbooks) ---------------------------

def test_search_incidents_shortcut_scopes_doc_type_and_cluster(monkeypatch):
    rec = _patch_search(monkeypatch, [[_point()]])
    out = server.search_incidents("stuck attaching", "prod-01")
    conds = _conditions(rec.calls[0][0])
    assert conds["doc_type"] == "incident"
    assert conds["cluster"] == "prod-01"
    assert out["status"] == "ok"


def test_search_runbooks_shortcut_scopes_component(monkeypatch):
    rec = _patch_search(monkeypatch, [[_point()]])
    server.search_runbooks("rebuild procedure", component="longhorn")
    conds = _conditions(rec.calls[0][0])
    assert conds["doc_type"] == "runbook"
    assert conds["component"] == "longhorn"


def test_search_incidents_fleet_fallback(monkeypatch):
    rec = _patch_search(monkeypatch, [[], [_point()]])
    out = server.search_incidents("stuck attaching", "prod-03")
    assert len(rec.calls) == 2
    assert out["cluster_narrowed"] is False


# --- 반복 장애 확인 (capture.find_similar) ------------------------------------


def _patch_similar(monkeypatch, results):
    monkeypatch.setattr(capture.embeddings, "embed", lambda q, role: [0.1, 0.2])
    rec = _QueryRecorder(results)
    monkeypatch.setattr(capture.vectorstore, "query", rec)
    return rec


def test_find_similar_no_cluster_no_narrow(monkeypatch):
    rec = _patch_similar(monkeypatch, [[_point()]])
    out = capture.find_similar("volume stuck", cluster=None)
    assert len(rec.calls) == 1
    assert "cluster" not in _conditions(rec.calls[0][0])
    assert out["cluster_narrowed"] is None
    assert out["count"] == 1


def test_find_similar_cluster_narrowed_when_hits(monkeypatch):
    rec = _patch_similar(monkeypatch, [[_point()]])
    out = capture.find_similar("volume stuck", cluster="prod-01")
    assert "cluster" in _conditions(rec.calls[0][0])
    assert out["cluster_narrowed"] is True
    assert out["count"] == 1


def test_find_similar_cluster_empty_falls_back_fleet_wide(monkeypatch):
    rec = _patch_similar(monkeypatch, [[], [_point(payload={"title": "prior"})]])
    out = capture.find_similar("volume stuck", cluster="prod-02")
    assert len(rec.calls) == 2
    assert "cluster" in _conditions(rec.calls[0][0])
    assert "cluster" not in _conditions(rec.calls[1][0])
    assert out["cluster_narrowed"] is False
    assert out["count"] == 1


def test_find_similar_min_score_still_filters(monkeypatch):
    # 같은 클러스터 결과가 있어도 점수가 모두 임계값 미만이면 남는 결과가 없으므로,
    # 같은 클러스터 결과가 빈 것으로 취급 -> 전체 범위로 대체 검색.
    rec = _patch_similar(monkeypatch, [[_point(score=0.4)], [_point(score=0.9)]])
    out = capture.find_similar("volume stuck", cluster="prod-02", min_score=0.75)
    assert len(rec.calls) == 2
    assert out["cluster_narrowed"] is False
    assert out["count"] == 1
