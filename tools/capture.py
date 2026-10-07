"""tools/capture.py — 내부 쓰기 API의 로직 (지식 플라이휠).

역할
  에이전트가 조사를 마치면 그 결과(RCA)를 지식 베이스에 기록해, 다음에 비슷한 알림이 왔을 때
  "전에 본 것"으로 알아볼 수 있게 합니다. 문서 단위 수집(ingest.py)을 장애 단위 기록으로
  보완합니다.

주요 함수 (server.py의 /internal/knowledge/* 라우트가 호출)
  - capture_incident: 장애/RCA 하나를 fingerprint 기준으로 기록 (재발이면 occurrence_count 증가)
  - find_similar:     반복 장애 사전 확인 — "이 증상을 본 적이 있나?" (밀집 검색만 사용)
  - record_feedback:  기록된 장애에 사람의 판단(상태, 신뢰도, 메모)을 덧붙임
  - stats:            지식 베이스 개수와 현재 설정

설계 원칙
  - LLM에 노출하지 않음: 어느 것도 @mcp.tool()이 아니며, 신뢰할 수 있는 프로세스만 HTTP로 호출합니다.
  - 수집과 질의의 일관성: 임베딩과 Qdrant를 이 서비스가 직접 다루므로, 기록과 질의가 같은 임베딩
    설정을 씁니다.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
)

import embeddings
import reranker
import vectorstore
from ingest import _chunk  # 배치 수집과 똑같은 청킹을 재사용 (DRY)

# --- 설정 ---------------------------------------------------------------------
log = logging.getLogger("rag-capture")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION = os.environ.get("QDRANT_COLLECTION")
HTTP_TIMEOUT = float(os.environ.get("RAG_TIMEOUT_SECONDS", "60"))
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "100"))

# ingest.py와 같은 고정 네임스페이스를 써서 ID가 한 공간에서 생성되게 합니다.
_ID_NAMESPACE = uuid.UUID("6f3a9c1e-9b2d-5a44-8c11-a1b2c3d4e5f6")

# 반복 장애 필터(cluster/alert/component)를 저렴하게 하려고 색인하는 페이로드 키.
# 이미 있는 인덱스를 만드는 것은 아무 일도 하지 않으며, 오류는 무시합니다.
_INDEXED_FIELDS = (
    "doc_type", "fingerprint", "status", "cluster",
    "namespace", "alertname", "component",
)

_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=HTTP_TIMEOUT)


# --- 내부 도우미 --------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(doc: dict[str, Any]) -> str:
    """반복 장애의 고정 식별자. 알림 fingerprint를 우선 사용하고, 없으면 식별
    레이블의 결정적 해시로 대체해 수동 기록도 중복 제거되게 합니다."""
    fp = (doc.get("fingerprint") or "").strip()
    if fp:
        return fp
    basis = "|".join(str(doc.get(k, "")) for k in ("alertname", "cluster", "namespace", "component", "title"))
    return f"auto:{uuid.uuid5(_ID_NAMESPACE, basis)}"


def _ensure_collection(dim: int) -> None:
    # 명명된 밀집 벡터(+ 하이브리드가 켜져 있으면 BM25 희소 벡터) 스키마 + 키워드
    # 인덱스. ingest.py와 공유하므로 쓰기 경로끼리 어긋나지 않습니다.
    vectorstore.ensure_collection(_client, COLLECTION, dim, payload_indexes=_INDEXED_FIELDS)


def _searchable_text(doc: dict[str, Any]) -> str:
    """검색용으로 임베딩하는 텍스트 — 증상 + 원인 + 해결책이므로, 나중에 같은
    증상의 알림이 오면 매칭됩니다. 전체 제안은 표시용으로 페이로드에 따로
    저장됩니다."""
    parts = [
        doc.get("title", ""),
        doc.get("symptom", ""),
        f"Root cause: {doc.get('root_cause', '')}",
        f"Fix: {doc.get('proposed_fix', '')}",
        " ".join(doc.get("tags", []) or []),
    ]
    return "\n".join(p for p in parts if p and p.strip())


def _existing_history(fingerprint: str) -> dict[str, Any]:
    """이 fingerprint의 이전 기록에 대한 {occurrence_count, first_seen}을 반환해,
    재발 시 값이 초기화되지 않고 증가하게 합니다."""
    try:
        found, _ = _client.scroll(
            collection_name=COLLECTION,
            scroll_filter=Filter(must=[FieldCondition(key="fingerprint", match=MatchValue(value=fingerprint))]),
            limit=1,
            with_payload=True,
        )
    except Exception:  # noqa: BLE001 - 컬렉션이 아직 없을 수 있음
        return {}
    if not found:
        return {}
    p = found[0].payload or {}
    return {"occurrence_count": p.get("occurrence_count", 1), "first_seen": p.get("first_seen")}


# --- 장애 기록 ----------------------------------------------------------------
def capture_incident(doc: dict[str, Any]) -> dict[str, Any]:
    """장애/RCA 하나를 fingerprint를 키로 지식 베이스에 업서트합니다.

    멱등적이며 재발을 인식합니다: 같은 fingerprint를 다시 기록하면 청크를 교체하고
    ``occurrence_count`` / ``last_seen``을 갱신합니다 (first_seen은 유지).
    """
    fingerprint = _fingerprint(doc)
    body = (doc.get("body") or doc.get("proposal") or _searchable_text(doc)).strip()
    if not body:
        return {"status": "error", "error": "nothing to capture (empty body)"}

    chunks = _chunk(body, CHUNK_SIZE, CHUNK_OVERLAP)
    # 첫 청크에는 조합한 검색용 텍스트를 담아, 본문이 긴 자유 형식 제안이어도
    # 증상 기반 검색에 걸리게 합니다.
    embed_texts = [_searchable_text(doc) or chunks[0]] + chunks[1:]
    try:
        vectors = [embeddings.embed(t, "document") for t in embed_texts]
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"embedding failed: {exc}"}

    _ensure_collection(len(vectors[0]))

    prior = _existing_history(fingerprint)
    occurrence = int(prior.get("occurrence_count") or 0) + 1
    first_seen = prior.get("first_seen") or _now()
    now = _now()
    status = doc.get("status") or ("recurring" if occurrence > 1 else "open")

    payload_base = {
        "doc_type": "incident",
        "fingerprint": fingerprint,
        "title": doc.get("title") or doc.get("alertname") or "incident",
        "source": doc.get("source") or "agent-investigation",
        "status": status,
        "cluster": doc.get("cluster"),
        "namespace": doc.get("namespace"),
        "alertname": doc.get("alertname"),
        "component": doc.get("component"),
        "workload": doc.get("workload"),
        "root_cause": doc.get("root_cause"),
        "proposed_fix": doc.get("proposed_fix"),
        "confidence": doc.get("confidence"),
        "risk": doc.get("risk"),
        "tags": doc.get("tags") or [],
        "proposal_id": doc.get("proposal_id"),
        "occurrence_count": occurrence,
        "first_seen": first_seen,
        "last_seen": now,
    }

    # 이 fingerprint의 이전 청크를 교체해, 더 짧게 다시 기록해도 오래된 청크가
    # 남지 않게 합니다.
    try:
        _client.delete(
            collection_name=COLLECTION,
            points_selector=Filter(must=[FieldCondition(key="fingerprint", match=MatchValue(value=fingerprint))]),
        )
    except Exception:  # noqa: BLE001
        pass

    # 밀집 벡터와 같은 텍스트(embed_texts)로 희소 벡터를 만들어, 두 신호가 모두
    # 조합한 검색용 내용을 가리키게 합니다. 하이브리드가 꺼져 있으면 [None...].
    sparse = vectorstore.embed_documents_sparse(embed_texts)
    points = [
        PointStruct(
            id=str(uuid.uuid5(_ID_NAMESPACE, f"incident:{fingerprint}#{i}")),
            vector=vectorstore.named_vectors(vec, sp),
            payload={**payload_base, "text": chunk, "chunk": i},
        )
        for i, (chunk, vec, sp) in enumerate(zip(chunks, vectors, sparse))
    ]
    _client.upsert(collection_name=COLLECTION, points=points)
    log.info("captured incident fp=%s (occurrence=%d, %d chunk(s))", fingerprint, occurrence, len(points))
    return {
        "status": "ok",
        "fingerprint": fingerprint,
        "occurrence_count": occurrence,
        "first_seen": first_seen,
        "last_seen": now,
        "incident_status": status,
        "chunks": len(points),
    }


# --- 반복 장애 확인 -----------------------------------------------------------
def find_similar(query: str, doc_type: str = "incident", limit: int = 3, min_score: float = 0.0,
                 cluster: str | None = None) -> dict[str, Any]:
    """반복 장애 사전 확인을 위한 시맨틱 조회. ``min_score`` 이상인 결과를 알림
    발송기가 표시하는 구조화된 페이로드 필드와 함께 반환합니다.

    ``cluster``는 검색 도구와 마찬가지로 소프트 필터입니다: 클러스터로 한정한 조회
    결과가 비어 있으면 전체 범위로 다시 조회하고 ``cluster_narrowed: false``를
    보고합니다 — 새 클러스터에서 증상이 처음 나타났을 때도 다른 클러스터에서 본
    선례가 나타나야 합니다.
    """
    query = (query or "").strip()
    if not query:
        return {"status": "error", "error": "query must be non-empty"}
    cluster = (cluster or "").strip() or None
    try:
        vector = embeddings.embed(query, "query")
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"embedding failed: {exc}"}

    def _run(narrow_cluster: str | None) -> list[Any]:
        conditions: list[Any] = []
        if doc_type:
            conditions.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
        if narrow_cluster:
            conditions.append(FieldCondition(key="cluster", match=MatchValue(value=narrow_cluster)))
        query_filter = Filter(must=conditions) if conditions else None
        # 밀집 전용: 반복 장애 감지는 코사인 값(min_score)으로 임계값을 판단하는데,
        # RRF 결합은 이 값을 유지하지 않습니다. LLM용 검색은 하이브리드를 씁니다.
        return list(vectorstore.query(
            _client, COLLECTION, vector, query,
            query_filter=query_filter, limit=max(1, limit), hybrid=False,
        ))

    def _hits(points: list[Any]) -> list[dict[str, Any]]:
        # fingerprint 기준으로 중복 제거 (장애 하나에 청크가 여러 개), 최고 점수만 유지.
        best: dict[str, dict[str, Any]] = {}
        for point in points:
            if point.score < min_score:
                continue
            p = point.payload or {}
            fp = p.get("fingerprint") or p.get("source") or str(point.id)
            if fp in best and best[fp]["score"] >= round(point.score, 4):
                continue
            best[fp] = {
                "score": round(point.score, 4),
                "fingerprint": p.get("fingerprint"),
                "title": p.get("title"),
                "cluster": p.get("cluster"),
                "root_cause": p.get("root_cause"),
                "proposed_fix": p.get("proposed_fix"),
                "status": p.get("status"),
                "occurrence_count": p.get("occurrence_count"),
                "last_seen": p.get("last_seen"),
                "proposal_id": p.get("proposal_id"),
            }
        return sorted(best.values(), key=lambda h: h["score"], reverse=True)

    try:
        points = _run(cluster)
        # None = 클러스터를 요청하지 않음; True = 해당 클러스터로 한정됨; False =
        # 소프트 필터의 전체 범위 대체 검색이 실행됨.
        cluster_narrowed: bool | None = None
        hits = _hits(points)
        if cluster is not None:
            cluster_narrowed = True
            if not hits:
                points = _run(None)
                cluster_narrowed = False
                hits = _hits(points)
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return {"status": "ok", "count": 0, "results": []}  # 빈 KB는 오류가 아님
        return {"status": "error", "error": f"qdrant error: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"qdrant search failed: {exc}"}

    return {"status": "ok", "query": query, "count": len(hits),
            "cluster": cluster, "cluster_narrowed": cluster_narrowed, "results": hits}


# --- 피드백과 통계 ------------------------------------------------------------
def record_feedback(fingerprint: str, status: str | None = None, confidence: str | None = None,
                    note: str | None = None) -> dict[str, Any]:
    """기록된 장애에 사람의 결정을 덧붙입니다 (피드백 루프).
    같은 fingerprint를 가진 모든 청크를 갱신합니다."""
    fingerprint = (fingerprint or "").strip()
    if not fingerprint:
        return {"status": "error", "error": "fingerprint required"}
    patch: dict[str, Any] = {"feedback_at": _now()}
    if status:
        patch["status"] = status
    if confidence:
        patch["confidence"] = confidence
    if note:
        patch["feedback_note"] = note
    try:
        _client.set_payload(
            collection_name=COLLECTION,
            payload=patch,
            points=Filter(must=[FieldCondition(key="fingerprint", match=MatchValue(value=fingerprint))]),
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"feedback update failed: {exc}"}
    log.info("recorded feedback fp=%s status=%s", fingerprint, status)
    return {"status": "ok", "fingerprint": fingerprint, "updated": patch}


def stats() -> dict[str, Any]:
    """관리자 대시보드나 UI용 지식 베이스 개수."""
    try:
        total = _client.count(COLLECTION, exact=True).count
    except Exception:  # noqa: BLE001
        return {"status": "ok", "collection": COLLECTION, "exists": False}

    def _count(doc_type: str) -> int | None:
        try:
            return _client.count(
                COLLECTION, exact=True,
                count_filter=Filter(
                    must=[FieldCondition(key="doc_type", match=MatchValue(value=doc_type))]
                ),
            ).count
        except Exception:  # noqa: BLE001 - 유형별 개수는 최선형
            return None

    return {
        "status": "ok", "collection": COLLECTION, "exists": True,
        "points": total,
        "incident_points": _count("incident"),
        "runbook_points": _count("runbook"),
        "embeddings": embeddings.describe(),
        "reranker": reranker.describe(),
        "retrieval": vectorstore.describe(),
    }
