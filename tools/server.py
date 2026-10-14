"""tools/server.py — rag-mcp MCP 서버.

역할
  마크다운 문서로 이루어진 지식 베이스에 대한 검색 도구를
  MCP(streamable-http)로 제공합니다.

구성
  - 검색 경로 `_search` (rag_search): 질의 임베딩 → 하이브리드 검색 → 사내 리랭커로 리랭킹 → 응답 구성
  - MCP 도구: rag_search, rag_collections, rag_health

문서 디렉터리 (RAG_KNOWLEDGE_DIR)
  사람이 관리하는 문서를 official/ 아래에 둡니다. 서버는 문서 디렉터리를 읽지도 쓰지도 않고 Qdrant만
  검색합니다. 결과의 source(예: official/foo.md)가 원본 문서의 경로입니다.

설계 원칙
  - 읽기 전용: MCP 도구는 검색만 합니다. 모델은 지식 베이스를 바꿀 수 없고, 기록은 사람이 rag-ingest
    (ingest.py)로 합니다.
  - 벤더 중립: 채팅 LLM은 연결한 MCP 클라이언트가 정하고, 임베딩은 embeddings.py를 거쳐 OpenAI 호환
    엔드포인트를 씁니다.

주의
  @mcp.tool() 함수의 docstring은 LLM에게 그대로 전달되는 도구 설명입니다. 동작을 바꾸면 함께
  고치세요.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse

import embeddings
import reranker
import vectorstore

# --- 설정 ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("rag-mcp")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION = os.environ.get("QDRANT_COLLECTION", "rag_kb")  # ingest.py와 같은 기본값

HTTP_TIMEOUT = float(os.environ.get("RAG_TIMEOUT_SECONDS", "30"))
DEFAULT_LIMIT = int(os.environ.get("RAG_DEFAULT_LIMIT", "5"))
MAX_LIMIT = int(os.environ.get("RAG_MAX_LIMIT", "20"))
SNIPPET_CHARS = int(os.environ.get("RAG_SNIPPET_CHARS", "1200"))

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8084"))

mcp = FastMCP("rag", host=MCP_HOST, port=MCP_PORT)

# 오래 유지되는 클라이언트 하나; Qdrant 연결은 열어 두는 비용이 적습니다.
_qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=HTTP_TIMEOUT)


# --- 검색 경로 -----------------------------------------------------------
def _clamp_limit(limit: int) -> int:
    if limit < 1:
        return 1
    return min(limit, MAX_LIMIT)


def _search(query: str, limit: int) -> dict[str, Any]:
    """rag_search 의 검색 경로: 질의 임베딩 → 하이브리드 검색 → 리랭킹 → 응답 구성."""
    query = (query or "").strip()
    if not query:
        return {"status": "error", "error": "query must be a non-empty string"}
    # 사내 리랭커는 필수입니다. 설정이 빠졌으면 임베딩 서버 주소처럼 오류로 알립니다.
    if not reranker.configured():
        return {"status": "error", "error": reranker.NOT_CONFIGURED}

    limit = _clamp_limit(limit)
    # 후보를 넉넉히 가져오고(하이브리드 검색의 재현율), 최종 상위 `limit`개는 리랭커가 고르게
    # 합니다(정밀도).
    fetch = max(limit, reranker.CANDIDATES)

    try:
        vector = embeddings.embed(query, "query")
    except embeddings.EmbeddingError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 어떤 임베딩 실패든 그대로 드러냄
        return {"status": "error", "error": f"embedding failed: {exc}"}

    try:
        # 하이브리드가 켜져 있으면 하이브리드(밀집 + BM25 희소, RRF 결합), 아니면 밀집 전용.
        points = list(vectorstore.query(
            _qdrant, COLLECTION, vector, query,
            limit=fetch,
        ))
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return {
                "status": "error",
                "error": (
                    f"collection '{COLLECTION}' not found in Qdrant. Populate it "
                    f"first with the ingestion command (`rag-ingest`; see README.md)."
                ),
            }
        return {"status": "error", "error": f"qdrant error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"qdrant search failed: {exc}"}
    # 후보를 (스니펫이 아닌) 청크 전체 텍스트로 리랭킹합니다. 리랭커 호출이 실패하면(서버 중단 등)
    # 경고를 남기고 하이브리드 검색 순서로 돌려주므로, 리랭커 장애로 검색이 멈추지 않습니다.
    rerank_scores: list[float | None] = [None] * len(points)
    reranked = False
    if points:
        docs = [(p.payload or {}).get("text", "") for p in points]
        try:
            order = reranker.rerank(query, docs)
            points = [points[i] for i, _ in order]
            rerank_scores = [s for _, s in order]
            reranked = True
        except reranker.RerankError as exc:
            log.warning("rerank failed (%s); returning search order", exc)
            rerank_scores = [None] * len(points)

    points = points[:limit]
    rerank_scores = rerank_scores[:limit]

    hits = []
    for point, rr in zip(points, rerank_scores):
        payload = point.payload or {}
        text = payload.get("text", "")
        hit = {
            "score": round(point.score, 4),
            "title": payload.get("title"),
            "source": payload.get("source"),
            "text": text[:SNIPPET_CHARS],
            "truncated": len(text) > SNIPPET_CHARS,
        }
        if rr is not None:
            hit["rerank_score"] = round(rr, 4)
        hits.append(hit)

    return {
        "status": "ok",
        "collection": COLLECTION,
        "query": query,
        "reranked": reranked,
        "count": len(hits),
        "results": hits,
    }


# --- MCP 도구 (LLM에 노출됨) --------------------------------------------------
@mcp.tool()
def rag_search(query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """지식 베이스 전체에 대한 시맨틱 검색.

    질문이나 작업과 관련된 문서를 가져올 때 호출하세요. 키워드가 아니라 의미로 검색하므로
    찾는 내용을 문장으로 서술하세요.
    결과의 `source`는 원본 문서의 경로(예: 'official/foo.md')입니다. 답할 때 출처로 인용하세요.

    Args:
        query: 찾고 있는 내용 (자연어). 예:
            '노드 재부팅 후 Longhorn 볼륨이 attaching 상태에서 멈춤'.
        limit: 반환할 최대 결과 수 (1-20). 기본값 5.
    """
    return _search(query, limit)


@mcp.tool()
def rag_collections() -> dict[str, Any]:
    """Qdrant 컬렉션 목록과 현재 지식 베이스의 포인트 수를 보여 줍니다.

    검색을 실행하기 전에 먼저 이 도구로 지식 베이스가 존재하고 채워져 있는지
    확인하세요.
    """
    try:
        names = [c.name for c in _qdrant.get_collections().collections]
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error": f"could not reach Qdrant at {QDRANT_URL}: {exc}",
        }

    active_points: int | None = None
    if COLLECTION in names:
        try:
            active_points = _qdrant.count(COLLECTION, exact=True).count
        except Exception:  # noqa: BLE001 - count is best-effort
            active_points = None

    return {
        "status": "ok",
        "qdrant_url": QDRANT_URL,
        "active_collection": COLLECTION,
        "active_collection_exists": COLLECTION in names,
        "active_collection_points": active_points,
        "collections": names,
    }


@mcp.tool()
def rag_health() -> dict[str, Any]:
    """의존성(Qdrant, 사내 임베딩 서버, 사내 리랭커)의 접근 가능 여부를 확인합니다.

    의존성별 상태를 반환합니다. 검색이 실패하거나 결과가 이상하면 먼저 이 도구를 호출해 문제가 벡터
    DB인지, 임베딩 서버인지, 리랭커인지 구분하세요.
    """
    health: dict[str, Any] = {"status": "ok"}

    try:
        _qdrant.get_collections()
        health["qdrant"] = {"reachable": True, "url": QDRANT_URL}
    except Exception as exc:  # noqa: BLE001
        health["status"] = "degraded"
        health["qdrant"] = {"reachable": False, "url": QDRANT_URL, "error": str(exc)}

    emb = embeddings.describe()
    try:
        embeddings.embed("healthcheck", "query")
        health["embeddings"] = {"reachable": True, **emb}
    except Exception as exc:  # noqa: BLE001
        health["status"] = "degraded"
        health["embeddings"] = {"reachable": False, **emb, "error": str(exc)}

    rr = reranker.describe()
    try:
        reranker.rerank("healthcheck", ["healthcheck"])
        health["reranker"] = {"reachable": True, **rr}
    except Exception as exc:  # noqa: BLE001
        health["status"] = "degraded"
        health["reranker"] = {"reachable": False, **rr, "error": str(exc)}
    health["retrieval"] = vectorstore.describe()  # 하이브리드 켜짐/꺼짐 + 키워드 벡터 방식(kiwi-bm25)

    return health


# --- 실행 ---------------------------------------------------------------------
def main() -> None:
    emb = embeddings.describe()
    rr = reranker.describe()
    log.info(
        "starting rag-mcp on %s:%s (qdrant=%s, collection=%s, embed=%s@%s, "
        "hybrid=%s, rerank=%s)",
        MCP_HOST, MCP_PORT, QDRANT_URL, COLLECTION,
        emb["model"], emb["base_url"],
        vectorstore.sparse_available(),  # 시작 시 BM25를 로드 (빠른 실패)
        f"{rr['model']}@{rr['base_url']}" if rr["base_url"] else "NOT SET",
    )
    if not rr["base_url"]:
        log.warning("%s; rag_search will return an error until it is set", reranker.NOT_CONFIGURED)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
