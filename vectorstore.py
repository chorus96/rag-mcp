"""공유 Qdrant 벡터 저장소 계층: 명명된 밀집 벡터 + BM25 희소 벡터 (하이브리드 검색).

검색 1단계. 밀집 벡터는 의미를 잡아내지만 운영 텍스트에서 중요한 정확한 토큰 —
에러 문자열(`CrashLoopBackOff`), 리소스 ID(`c-xxxxx`), 컴포넌트 이름(`Longhorn`) —
을 놓칩니다. 희소 BM25 벡터가 이를 찾아냅니다. 청크마다 두 벡터를 모두 저장하고
(Qdrant 명명된 벡터) 질의 시점에 Reciprocal Rank Fusion(RRF)으로 결합하므로, 재현율이
두 신호의 이점을 모두 얻습니다.

희소 벡터는 FastEmbed의 `Qdrant/bm25` 모델로 로컬에서 만듭니다 — API 키가 필요 없고
오프라인에서도 동작해 스택의 나머지 부분과 맞습니다. IDF는 컬렉션의
`Modifier.IDF`로 서버 측에서 적용되므로, 질의 쪽은 단어 존재 여부만 있으면 됩니다.

단일 기준점: `ingest.py`, `capture.py`, `server.py`가 모두 여기를 거치므로, 쓰기
경로와 읽기 경로 사이에서 컬렉션 스키마와 벡터 이름이 어긋나지 않습니다.

우아한 성능 저하: FastEmbed를 import/로드할 수 없거나 하이브리드가 꺼져 있으면
(`RAG_HYBRID=false`), 컬렉션은 밀집 전용이 되고 질의는 일반 밀집 검색으로 대체됩니다.
아무것도 깨지지 않습니다 — 키워드 신호만 잃을 뿐입니다.

스키마 참고: 명명된 벡터(`dense`)를 사용하므로, 이전의 이름 없는 벡터 컬렉션과
호환되지 않습니다. 하이브리드로 옮기려면 한 번 다시 수집해야 합니다
(`python ingest.py --recreate`).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Fusion,
    FusionQuery,
    Modifier,
    Prefetch,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

log = logging.getLogger("rag-vectorstore")

# 명명된 벡터 키. 모든 읽기/쓰기 경로가 일치하도록 상수로 둡니다.
DENSE = "dense"
SPARSE = "bm25"

# 코드 변경 없이 하이브리드를 강제로 끌 수 있습니다 (밀집 전용이지만 스키마는 여전히
# 명명된 벡터). 기본값은 켜짐이며, 실제 상태는 FastEmbed 로드 여부에도 달려 있습니다.
_HYBRID_REQUESTED = os.environ.get("RAG_HYBRID", "true").strip().lower() not in (
    "0", "false", "no", "off", ""
)
BM25_MODEL = os.environ.get("RAG_SPARSE_MODEL", "Qdrant/bm25")

_bm25 = None            # 지연 로드되는 FastEmbed 모델
_bm25_loaded = False    # 로드를 시도한 적이 있는지


def _load_bm25() -> Any | None:
    """FastEmbed BM25 모델을 지연 import + 생성합니다. 캐시됩니다. 하이브리드가
    꺼져 있거나 FastEmbed를 쓸 수 없으면 None을 반환합니다 (로그는 한 번만)."""
    global _bm25, _bm25_loaded
    if _bm25_loaded:
        return _bm25
    _bm25_loaded = True
    if not _HYBRID_REQUESTED:
        log.info("hybrid search disabled (RAG_HYBRID=false); using dense-only")
        return None
    try:
        from fastembed import SparseTextEmbedding

        _bm25 = SparseTextEmbedding(model_name=BM25_MODEL)
        log.info("hybrid search enabled (sparse model=%s)", BM25_MODEL)
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 => 밀집 전용, 치명적이지 않음
        log.warning("FastEmbed unavailable (%s); falling back to dense-only", exc)
        _bm25 = None
    return _bm25


def sparse_available() -> bool:
    """BM25 희소 벡터를 만들 수 있으면 True (하이브리드 동작 중)."""
    return _load_bm25() is not None


def describe() -> dict[str, Any]:
    return {"hybrid": sparse_available(), "sparse_model": BM25_MODEL if _HYBRID_REQUESTED else None}


def _to_sparse(embedding: Any) -> SparseVector:
    return SparseVector(
        indices=embedding.indices.tolist(), values=embedding.values.tolist()
    )


def embed_documents_sparse(texts: list[str]) -> list[SparseVector | None]:
    """저장할 청크의 희소 벡터. 하이브리드가 꺼져 있으면 [None, ...]을 반환합니다."""
    model = _load_bm25()
    if model is None:
        return [None] * len(texts)
    return [_to_sparse(e) for e in model.embed(texts)]


def embed_query_sparse(text: str) -> SparseVector | None:
    """질의의 희소 벡터 (IDF는 Modifier.IDF로 서버 측에서 적용됩니다)."""
    model = _load_bm25()
    if model is None:
        return None
    return _to_sparse(next(iter(model.query_embed(text))))


def named_vectors(dense: list[float], sparse: SparseVector | None) -> dict[str, Any]:
    """청크 하나에 대한 PointStruct.vector 매핑을 만듭니다."""
    vectors: dict[str, Any] = {DENSE: dense}
    if sparse is not None:
        vectors[SPARSE] = sparse
    return vectors


def ensure_collection(
    client: QdrantClient, collection: str, dim: int, payload_indexes: tuple[str, ...] = ()
) -> None:
    """명명된 밀집 벡터(+ 하이브리드가 켜져 있으면 BM25 희소 벡터)와 키워드
    페이로드 인덱스로 컬렉션을 만듭니다. 이미 있으면 아무 일도 하지 않습니다."""
    if not client.collection_exists(collection):
        sparse_config = (
            {SPARSE: SparseVectorParams(modifier=Modifier.IDF)}
            if sparse_available()
            else None
        )
        client.create_collection(
            collection_name=collection,
            vectors_config={DENSE: VectorParams(size=dim, distance=Distance.COSINE)},
            sparse_vectors_config=sparse_config,
        )
        log.info(
            "created collection %s (dim=%d, cosine, hybrid=%s)",
            collection, dim, sparse_available(),
        )
    for field in payload_indexes:
        try:
            client.create_payload_index(
                collection, field_name=field, field_schema="keyword"
            )
        except Exception:  # noqa: BLE001 - 이미 있음 / 구버전 서버: 최선형
            pass


def query(
    client: QdrantClient,
    collection: str,
    dense_vector: list[float],
    query_text: str,
    *,
    query_filter: Any | None,
    limit: int,
    hybrid: bool = True,
):
    """`limit`개의 포인트를 가져옵니다.

    hybrid=True (기본값): 희소 벡터를 쓸 수 있으면 밀집 + BM25 희소를 RRF로 결합 —
    재현율이 가장 좋음; `point.score`는 (작은) RRF 결합 점수입니다.
    hybrid=False: 일반 밀집 검색 — `point.score`는 코사인 유사도입니다. 호출자가
    코사인 값으로 임계값을 판단할 때(예: 반복 장애 감지) 사용하세요. 결합 점수는
    척도가 다르기 때문입니다.
    희소 벡터를 쓸 수 없으면 항상 밀집 전용으로 대체됩니다."""
    sparse = embed_query_sparse(query_text) if (hybrid and query_text) else None

    if sparse is not None:
        result = client.query_points(
            collection_name=collection,
            prefetch=[
                Prefetch(query=dense_vector, using=DENSE, limit=limit, filter=query_filter),
                Prefetch(query=sparse, using=SPARSE, limit=limit, filter=query_filter),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
    else:
        result = client.query_points(
            collection_name=collection,
            query=dense_vector,
            using=DENSE,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
    return result.points
