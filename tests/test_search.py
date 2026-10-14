"""tools/server.py 테스트 — 검색 경로(rag_search).

확인하는 것
  - 검색 호출: vectorstore.query 를 한 번, 가져올 개수(limit)와 함께 호출
  - limit: 1~RAG_MAX_LIMIT 로 제한, 리랭커가 꺼져 있으면 정확히 limit 개만 가져옴
  - 응답 형식: 결과마다 score / title / source / text / truncated (메타데이터 필드 없음),
    긴 텍스트는 RAG_SNIPPET_CHARS 에서 잘라 truncated: true
  - 빈 질의는 오류
  - 컬렉션 기본값: 설정이 없어도 서버와 수집이 같은 컬렉션(rag_kb)을 씀

방법
  - 임베딩과 vectorstore.query 를 가짜로 바꿔, 호출마다 넘어온 인자를 기록하고 미리 정한
    결과를 돌려줍니다. 실제 Qdrant에는 접근하지 않습니다.
"""

import sys
import types
from pathlib import Path

# 소스가 tools/ 에 있으므로 import 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import server  # noqa: E402


# --- 테스트 도우미 ------------------------------------------------------------

def _point(payload=None, score=0.9):
    return types.SimpleNamespace(payload=payload or {}, score=score, id="pt")


class _QueryRecorder:
    """`vectorstore.query`를 대신합니다: 호출마다 인자를 기록하고 미리 설정한 포인트 목록을 돌려줍니다."""

    def __init__(self, points):
        self.points = points
        self.calls = []

    def __call__(self, client, collection, vector, query_text, *, limit, **kw):
        self.calls.append(limit)
        return self.points


def _patch_search(monkeypatch, points):
    monkeypatch.setattr(server.embeddings, "embed", lambda q, role: [0.1, 0.2])
    monkeypatch.setattr(server.reranker, "BASE_URL", "")  # 리랭커 꺼짐
    rec = _QueryRecorder(points)
    monkeypatch.setattr(server.vectorstore, "query", rec)
    return rec


# --- server._search -----------------------------------------------------------

def test_search_returns_plain_results(monkeypatch):
    rec = _patch_search(monkeypatch, [_point({"text": "body", "title": "a", "source": "official/a.md"})])
    out = server._search("crashloop", 5)
    assert rec.calls == [5]
    assert out["status"] == "ok" and out["count"] == 1 and out["reranked"] is False
    assert set(out) == {"status", "collection", "query", "reranked", "count", "results"}
    assert out["results"][0] == {"score": 0.9, "title": "a", "source": "official/a.md",
                                 "text": "body", "truncated": False}


def test_limit_is_clamped(monkeypatch):
    rec = _patch_search(monkeypatch, [])
    server._search("q", 0)
    server._search("q", 999)
    assert rec.calls == [1, server.MAX_LIMIT]


def test_long_text_is_truncated(monkeypatch):
    long = "x" * (server.SNIPPET_CHARS + 10)
    _patch_search(monkeypatch, [_point({"text": long})])
    hit = server._search("q", 5)["results"][0]
    assert len(hit["text"]) == server.SNIPPET_CHARS and hit["truncated"] is True


def test_empty_query_is_error(monkeypatch):
    rec = _patch_search(monkeypatch, [])
    assert server._search("  ", 5)["status"] == "error"
    assert rec.calls == []


def test_collection_default_matches_ingest():
    # 설정 파일에 QDRANT_COLLECTION 이 없어도 서버와 수집이 같은 컬렉션을 써야 합니다.
    import os
    import subprocess
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    env = {k: v for k, v in os.environ.items() if k != "QDRANT_COLLECTION"}
    out = subprocess.run(
        [sys.executable, "-c", "import server, ingest; print(server.COLLECTION, ingest.COLLECTION)"],
        cwd=tools_dir, capture_output=True, text=True, check=True, env={**env, "RAG_HYBRID": "false"},
    ).stdout.split()
    assert out == ["rag_kb", "rag_kb"]
