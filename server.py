"""RAG 메모리 MCP 서버 — 런북, 과거 장애, RCA(근본 원인 분석)로 이루어진 Qdrant
지식 베이스에 대한 읽기 전용 시맨틱 검색.


읽기 전용 원칙
--------------
이 서버는 검색(SEARCH) 도구만 노출합니다. 지식 베이스는 별도 경로의 `ingest.py`
작업이 채우므로(README.md 참고), LLM에 노출되는 인터페이스는 읽기 전용으로
유지됩니다.

벤더 중립 설계
--------------
특정 LLM, UI, 임베딩 벤더에 묶여 있지 않습니다. 채팅 LLM은 연결하는 MCP
클라이언트(LibreChat, mcpo를 통한 Open WebUI, 직접 만든 UI/CLI 등)가 정합니다.
임베딩은 `embeddings.py`의 교체 가능한 제공자(Ollama / OpenAI 호환 엔드포인트)를
거치므로, 같은 서버가 코드 변경 없이 Ollama로 오프라인 동작하거나 호스팅
제공자와 함께 동작합니다.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import FieldCondition, Filter, MatchValue
from starlette.requests import Request
from starlette.responses import JSONResponse

import capture
import embeddings
import reranker
import vectorstore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("rag-mcp")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION = os.environ.get("QDRANT_COLLECTION")

HTTP_TIMEOUT = float(os.environ.get("RAG_TIMEOUT_SECONDS", "30"))
DEFAULT_LIMIT = int(os.environ.get("RAG_DEFAULT_LIMIT", "5"))
MAX_LIMIT = int(os.environ.get("RAG_MAX_LIMIT", "20"))
SNIPPET_CHARS = int(os.environ.get("RAG_SNIPPET_CHARS", "1200"))

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8084"))

mcp = FastMCP("rag", host=MCP_HOST, port=MCP_PORT)

# 오래 유지되는 클라이언트 하나; Qdrant 연결은 열어 두는 비용이 적습니다.
_qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=HTTP_TIMEOUT)


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        return 1
    return min(limit, MAX_LIMIT)


def _build_conditions(doc_type: str | None, component: str | None,
                      cluster: str | None) -> list[Any]:
    """하드 필터와 (선택적) 클러스터 범위 제한을 위한 Qdrant 페이로드 조건.
    `doc_type`과 `component`는 하드 필터입니다: 빈 결과는 그대로 비어 있습니다.
    `cluster`는 호출자가 소프트 필터로 넘기며, 대체 재시도에서는 빠집니다
    (`_search` 참고)."""
    conditions = []
    if doc_type:
        conditions.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
    if component:
        conditions.append(FieldCondition(key="component", match=MatchValue(value=component)))
    if cluster:
        conditions.append(FieldCondition(key="cluster", match=MatchValue(value=cluster)))
    return conditions


def _search(query: str, doc_type: str | None, cluster: str | None, component: str | None,
            limit: int) -> dict[str, Any]:
    """모든 검색 도구가 공유하는 검색 경로.

    `doc_type`과 `component`는 하드 필터입니다. `cluster`는 소프트 필터입니다:
    클러스터로 한정한 검색 결과가 비어 있으면 (하드 필터는 유지한 채) 전체 범위로
    다시 검색하므로, 새 클러스터에서 증상이 처음 나타났을 때도 다른 클러스터의
    실제 선례가 가려지지 않습니다 — 에이전트 프롬프트가 Kubernetes/Prometheus에
    적용하는 빈 결과 자기 보정과 같은 방식입니다. 응답에 `cluster_narrowed`를
    포함해, 호출자가 "이 클러스터에는 선례 없음"과 "어디에도 선례 없음"을 구분할
    수 있게 합니다.
    """
    query = (query or "").strip()
    if not query:
        return {"status": "error", "error": "query must be a non-empty string"}
    doc_type = (doc_type or "").strip() or None
    cluster = (cluster or "").strip() or None
    component = (component or "").strip() or None

    limit = _clamp_limit(limit)
    # 리랭커가 켜져 있으면 후보를 넉넉히 가져오고(밀집 검색의 재현율), 최종 상위
    # `limit`개는 크로스 인코더가 고르게 합니다(정밀도). 꺼져 있으면 정확히 limit개만 가져옵니다.
    fetch = max(limit, reranker.CANDIDATES) if reranker.enabled() else limit

    try:
        vector = embeddings.embed(query, "query")
    except embeddings.EmbeddingError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 어떤 임베딩 실패든 그대로 드러냄
        return {"status": "error", "error": f"embedding failed: {exc}"}

    def _run(narrow_cluster: str | None) -> list[Any]:
        conditions = _build_conditions(doc_type, component, narrow_cluster)
        query_filter = Filter(must=conditions) if conditions else None
        # 하이브리드가 켜져 있으면 하이브리드(밀집 + BM25 희소, RRF 결합), 아니면 밀집 전용.
        return list(vectorstore.query(
            _qdrant, COLLECTION, vector, query,
            query_filter=query_filter, limit=fetch,
        ))

    try:
        points = _run(cluster)
        # None = 클러스터를 요청하지 않음; True = 해당 클러스터로 한정됨; False =
        # 소프트 필터의 전체 범위 대체 검색이 실행됨.
        cluster_narrowed: bool | None = None
        if cluster is not None:
            cluster_narrowed = True
            if not points:
                # 소프트 필터: 같은 클러스터 결과가 없음 — "이전 발생 없음"이라고
                # 답하기 전에 전체 범위로 다시 검색 (새 클러스터에서 증상이 처음
                # 나타났을 때도 전체 범위의 선례가 보여야 함).
                points = _run(None)
                cluster_narrowed = False
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return {
                "status": "error",
                "error": (
                    f"collection '{COLLECTION}' not found in Qdrant. Populate it "
                    f"first with the ingestion job (see rag-mcp/README.md: "
                    f"`python ingest.py --path ./knowledge`)."
                ),
            }
        return {"status": "error", "error": f"qdrant error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"qdrant search failed: {exc}"}
    # 후보를 (스니펫이 아닌) 청크 전체 텍스트로 리랭킹합니다. 최선형(best-effort):
    # 어떤 실패든 밀집 검색 순서로 되돌아가므로 검색이 깨지지 않습니다.
    rerank_scores: list[float | None] = [None] * len(points)
    reranked = False
    if reranker.enabled() and len(points) > 1:
        docs = [(p.payload or {}).get("text", "") for p in points]
        try:
            order = reranker.rerank(query, docs)
            points = [points[i] for i, _ in order]
            rerank_scores = [s for _, s in order]
            reranked = True
        except reranker.RerankError as exc:
            log.warning("rerank failed (%s); falling back to dense order", exc)
            rerank_scores = [None] * len(points)

    points = points[:limit]
    rerank_scores = rerank_scores[:limit]

    hits = []
    for point, rr in zip(points, rerank_scores):
        payload = point.payload or {}
        text = payload.get("text", "")
        hit = {
            "score": round(point.score, 4),
            "doc_type": payload.get("doc_type"),
            "title": payload.get("title"),
            "source": payload.get("source"),
            "tags": payload.get("tags"),
            "text": text[:SNIPPET_CHARS],
            "truncated": len(text) > SNIPPET_CHARS,
        }
        if rr is not None:
            hit["rerank_score"] = round(rr, 4)
        hits.append(hit)

    response: dict[str, Any] = {
        "status": "ok",
        "collection": COLLECTION,
        "query": query,
        "doc_type": doc_type,
        "cluster": cluster,
        "cluster_narrowed": cluster_narrowed,
        "component": component,
        "reranked": reranked,
        "count": len(hits),
        "results": hits,
    }
    if cluster is not None and cluster_narrowed is False:
        response["note"] = (
            f"no matching knowledge tagged cluster={cluster!r}; expanded the search "
            f"across all clusters"
        )
    return response


@mcp.tool()
def rag_search(
    query: str,
    doc_type: str | None = None,
    cluster: str | None = None,
    component: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Semantic search across the knowledge base (runbooks, incidents, RCAs).

    Call this while diagnosing an issue to pull in historical context — prior
    incidents with the same symptom, the runbook for a component, past root
    causes. Retrieval is by meaning, not keywords, so describe the symptom.

    Args:
        query: What you're looking for, in natural language. Example:
            'Longhorn volume stuck in attaching state after node reboot'.
        doc_type: Optional filter — 'incident', 'runbook', 'rca', or a custom
            type used at ingestion time. Omit to search everything.
        cluster: Optional SOFT filter — restrict to knowledge tagged with this
            cluster (e.g. 'prod-01'). If a cluster-scoped search comes back empty
            the server retries across ALL clusters (keeping the other filters)
            and reports `cluster_narrowed: false`, so a fleet-wide precedent is
            never hidden behind an empty same-cluster result. Omit for fleet-wide.
        component: Optional HARD filter — restrict to knowledge tagged with this
            component (e.g. 'longhorn'). Unlike `cluster` there is no empty-result
            fallback: empty means nothing is tagged with it.
        limit: Max results to return (1-20). Defaults to 5.
    """
    return _search(query, doc_type, cluster, component, limit)


@mcp.tool()
def search_incidents(
    query: str,
    cluster: str | None = None,
    component: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Search only past incidents / RCAs for ones matching the current symptom.

    Shortcut for rag_search(..., doc_type='incident'). Use this when you want
    'has this happened before?' rather than 'what's the documented procedure?'.
    Pass the target `cluster` to ask 'has this happened before ON THIS CLUSTER?'
    (a soft narrow — empty same-cluster results fall back to all clusters).

    Args:
        query: The symptom or error, in natural language.
        cluster: Optional SOFT filter — cluster-tagged incidents only, with a
            fleet-wide fallback when the same-cluster search is empty.
        component: Optional HARD filter — incidents tagged with this component.
        limit: Max results (1-20). Defaults to 5.
    """
    return _search(query, "incident", cluster, component, limit)


@mcp.tool()
def search_runbooks(
    query: str,
    cluster: str | None = None,
    component: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Search only runbooks / documented procedures.

    Shortcut for rag_search(..., doc_type='runbook'). Use this when you want the
    established procedure for a component or task. Pass `component` (e.g.
    'longhorn') to scope to one component's runbooks.

    Args:
        query: The component or task, in natural language.
        cluster: Optional SOFT filter — cluster-tagged runbooks only, with a
            fleet-wide fallback when the same-cluster search is empty.
        component: Optional HARD filter — runbooks tagged with this component.
        limit: Max results (1-20). Defaults to 5.
    """
    return _search(query, "runbook", cluster, component, limit)


@mcp.tool()
def rag_collections() -> dict[str, Any]:
    """List Qdrant collections and the point count of the active knowledge base.

    Use this first to confirm the knowledge base exists and has been populated
    before running searches.
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
    """Reachability check for both dependencies: Qdrant and the embedding model.

    Returns per-dependency status. Call this first if searches are failing to
    tell whether the problem is the vector DB or the local embedding model.
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

    # 리랭킹은 선택 사항이며 최선형입니다. 설정만 보고합니다 (실제 호출 확인 없음).
    health["reranker"] = reranker.describe()
    health["retrieval"] = vectorstore.describe()  # 하이브리드 켜짐/꺼짐 + 희소 모델

    return health


# ---------------------------------------------------------------------------
# 내부 쓰기 API (MCP 도구 아님 — LLM에는 보이지 않음)
# ---------------------------------------------------------------------------
# 지식 "플라이휠": 신뢰할 수 있는 에이전트 프로세스가 조사를 마친 뒤 여기서 RCA를
# 기록하고 사람의 피드백을 남기며, /similar로 반복 장애 사전 확인을 합니다. 이것들은
# 일반 HTTP 라우트이므로 위의 읽기 전용 MCP 도구 인터페이스는 그대로입니다 — 모델은
# 검색만 할 수 있고 절대 쓸 수 없습니다.
# RAG_INTERNAL_TOKEN으로 선택적으로 보호합니다 (운영에서는 설정; 비워 두면 개발용으로 열림).
INTERNAL_TOKEN = os.environ.get("RAG_INTERNAL_TOKEN", "")


def _authorized(request: Request) -> bool:
    if not INTERNAL_TOKEN:
        return True  # 개발용: 열림
    presented = request.headers.get("x-internal-token") or ""
    auth = request.headers.get("authorization", "")
    if not presented and auth.lower().startswith("bearer "):
        presented = auth[7:]
    return bool(presented) and hmac.compare_digest(presented, INTERNAL_TOKEN)


async def _guarded(request: Request, fn) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse({"status": "error", "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json() if request.method == "POST" else {}
    except Exception:  # noqa: BLE001
        body = {}
    try:
        return JSONResponse(fn(body))
    except Exception as exc:  # noqa: BLE001 - 호출자에게 원인 없는 500을 절대 돌려주지 않음
        log.exception("internal knowledge route failed")
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


@mcp.custom_route("/internal/knowledge/capture", methods=["POST"])
async def _capture_route(request: Request) -> JSONResponse:
    return await _guarded(request, lambda b: capture.capture_incident(b))


@mcp.custom_route("/internal/knowledge/similar", methods=["POST"])
async def _similar_route(request: Request) -> JSONResponse:
    return await _guarded(request, lambda b: capture.find_similar(
        b.get("query", ""), b.get("doc_type", "incident"),
        int(b.get("limit", 3)), float(b.get("min_score", 0.0)), b.get("cluster"),
    ))


@mcp.custom_route("/internal/knowledge/feedback", methods=["POST"])
async def _feedback_route(request: Request) -> JSONResponse:
    return await _guarded(request, lambda b: capture.record_feedback(
        b.get("fingerprint", ""), b.get("status"), b.get("confidence"), b.get("note"),
    ))


@mcp.custom_route("/internal/knowledge/stats", methods=["GET"])
async def _stats_route(request: Request) -> JSONResponse:
    return await _guarded(request, lambda _b: capture.stats())


def main() -> None:
    emb = embeddings.describe()
    rr = reranker.describe()
    log.info(
        "starting rag-mcp on %s:%s (qdrant=%s, collection=%s, embed=%s:%s@%s, "
        "hybrid=%s, rerank=%s)",
        MCP_HOST, MCP_PORT, QDRANT_URL, COLLECTION,
        emb["provider"], emb["model"], emb["base_url"],
        vectorstore.sparse_available(),  # 시작 시 BM25를 로드 (빠른 실패)
        f"{rr['provider']}:{rr['model']}" if rr["enabled"] else "off",
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
