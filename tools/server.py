"""tools/server.py — rag-mcp MCP 서버.

역할
  런북, RCA(근본 원인 분석) 등 운영 문서로 이루어진 지식 베이스에 대한 검색 도구를
  MCP(streamable-http)로 제공합니다.

구성
  - 공통 검색 경로 `_search`: 질의 임베딩 → 하이브리드 검색 → (선택) 리랭킹 → 응답 구성
  - MCP 도구: rag_search, search_runbooks, rag_collections, rag_health
  - (선택) MCP 쓰기 도구: rag_add_document, rag_delete_document — RAG_MCP_WRITE=true 일 때만 등록,
    문서 디렉터리의 draft/ 아래만 다룸 (실제 로직은 documents.py)

설계 원칙
  - 기본은 읽기 전용: MCP 도구는 검색만 합니다. 지식 베이스 기록은 rag-ingest(ingest.py)로
    이루어집니다. 모델이 문서를 추가·삭제하는 쓰기 도구는 운영자가 RAG_MCP_WRITE=true로 켤 때만
    등록되며, 꺼져 있으면 도구 목록에도 나타나지 않습니다.
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
from qdrant_client.models import FieldCondition, Filter, MatchValue

import documents
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


# --- 공통 검색 경로 -----------------------------------------------------------
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
                    f"first with the ingestion command (`rag-ingest`; see README.md)."
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


# --- MCP 도구 (LLM에 노출됨) --------------------------------------------------
@mcp.tool()
def rag_search(
    query: str,
    doc_type: str | None = None,
    cluster: str | None = None,
    component: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """지식 베이스(런북, RCA 등 운영 문서) 전체에 대한 시맨틱 검색.

    문제를 진단하는 동안 관련 문서를 가져올 때 호출하세요 — 컴포넌트의 런북, 같은 증상을
    다룬 RCA나 운영 문서 등. 키워드가 아니라 의미로 검색하므로 증상을 서술하세요.

    Args:
        query: 찾고 있는 내용 (자연어). 예:
            '노드 재부팅 후 Longhorn 볼륨이 attaching 상태에서 멈춤'.
        doc_type: 선택적 필터 — 'runbook', 'rca', 'note', 또는 수집 시 사용한
            사용자 정의 유형. 생략하면 전체를 검색합니다.
        cluster: 선택적 소프트 필터 — 이 클러스터 태그(예: 'prod-01')가 붙은 지식으로
            한정합니다. 클러스터로 한정한 검색 결과가 비어 있으면 서버가 (다른 필터는
            유지한 채) 모든 클러스터를 대상으로 다시 검색하고 `cluster_narrowed: false`를
            보고하므로, 같은 클러스터 결과가 비었다고 전체 범위의 선례가 가려지지
            않습니다. 전체 범위로 검색하려면 생략하세요.
        component: 선택적 하드 필터 — 이 컴포넌트 태그(예: 'longhorn')가 붙은 지식으로
            한정합니다. `cluster`와 달리 빈 결과일 때 대체 검색이 없습니다: 비어 있으면
            이 태그가 붙은 지식이 없다는 뜻입니다.
        limit: 반환할 최대 결과 수 (1-20). 기본값 5.
    """
    return _search(query, doc_type, cluster, component, limit)


@mcp.tool()
def search_runbooks(
    query: str,
    cluster: str | None = None,
    component: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """런북 / 문서화된 절차만 검색합니다.

    rag_search(..., doc_type='runbook')의 단축 도구입니다. 컴포넌트나 작업에 대해
    정해진 절차를 알고 싶을 때 사용하세요. `component`(예: 'longhorn')를 넘기면 해당
    컴포넌트의 런북으로 범위를 좁힙니다.

    Args:
        query: 컴포넌트 또는 작업 (자연어).
        cluster: 선택적 소프트 필터 — 해당 클러스터 태그가 붙은 런북만 검색하며, 같은
            클러스터 결과가 비어 있으면 전체 범위로 대체 검색합니다.
        component: 선택적 하드 필터 — 이 컴포넌트 태그가 붙은 런북만 검색합니다.
        limit: 최대 결과 수 (1-20). 기본값 5.
    """
    return _search(query, "runbook", cluster, component, limit)


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
    """두 의존성 Qdrant와 임베딩 모델의 접근 가능 여부를 확인합니다.

    의존성별 상태를 반환합니다. 검색이 실패하면 먼저 이 도구를 호출해 문제가 벡터
    DB인지 임베딩 모델인지 구분하세요.
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


# --- MCP 쓰기 도구 (선택 — RAG_MCP_WRITE=true 일 때만 LLM에 노출) -------------------
# 꺼져 있으면 함수 자체를 등록하지 않으므로, 모델은 이 도구가 있는지도 모릅니다.
if documents.WRITE_ENABLED:
    @mcp.tool()
    def rag_add_document(
        title: str,
        content: str,
        doc_type: str = "note",
        tags: list[str] | None = None,
        component: str | None = None,
        cluster: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """지식 베이스에 마크다운 문서를 추가하고 바로 검색할 수 있게 색인합니다.

        사용자가 문서 추가(저장)를 명시적으로 요청했을 때만 사용하세요. 대화 내용을 임의로 저장하지
        마세요. 문서는 서버의 문서 디렉터리 중 `draft/<doc_type>s/<제목>.md` 에 초안으로 저장되고 바로
        검색됩니다. 응답의 `source`가 그 경로입니다. 같은 경로에 문서가 있으면 overwrite=true 일 때만
        바꿉니다. 사람이 관리하는 정식 문서(draft/ 밖)는 이 도구로 만들거나 바꿀 수 없습니다.

        Args:
            title: 문서 제목. 파일 이름도 여기서 만들어집니다.
            content: 마크다운 본문 (front matter 없이 본문만).
            doc_type: 문서 유형 — 'runbook', 'rca', 'note' 등 (소문자). 기본값 'note'.
            tags: 선택 — 태그 목록 (예: ['longhorn', 'storage']).
            component: 선택 — 컴포넌트 이름 (검색 하드 필터에 쓰임, 예: 'longhorn').
            cluster: 선택 — 클러스터 이름 (검색 소프트 필터에 쓰임, 예: 'prod-01').
            overwrite: 같은 경로의 기존 문서를 바꿀지 여부. 기본값 false.
        """
        return documents.add_document(title, content, doc_type, tags, component, cluster, overwrite)

    @mcp.tool()
    def rag_delete_document(source: str) -> dict[str, Any]:
        """지식 베이스의 초안 문서(draft/ 아래) 하나를 삭제합니다 (문서 파일과 검색용 청크를 함께 삭제).

        사용자가 삭제를 명시적으로 요청하고, 삭제할 문서를 확인한 뒤에만 사용하세요. 되돌릴 수
        없습니다. 삭제할 문서는 검색 결과나 rag_add_document 응답의 `source`로 지정합니다.
        `source`가 'draft/'로 시작하는 문서만 지울 수 있고, 정식 문서는 지울 수 없습니다.
        파일이 이미 없고 청크만 남아 있어도 청크를 정리합니다.

        Args:
            source: 문서 디렉터리 기준 문서 경로 (예: 'draft/runbooks/longhorn-볼륨-복구-절차.md').
        """
        return documents.delete_document(source)


# --- 실행 ---------------------------------------------------------------------
def main() -> None:
    emb = embeddings.describe()
    rr = reranker.describe()
    log.info(
        "starting rag-mcp on %s:%s (qdrant=%s, collection=%s, embed=%s:%s@%s, "
        "hybrid=%s, rerank=%s, write_tool=%s)",
        MCP_HOST, MCP_PORT, QDRANT_URL, COLLECTION,
        emb["provider"], emb["model"], emb["base_url"],
        vectorstore.sparse_available(),  # 시작 시 BM25를 로드 (빠른 실패)
        f"{rr['provider']}:{rr['model']}" if rr["enabled"] else "off",
        "on" if documents.WRITE_ENABLED else "off",
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
