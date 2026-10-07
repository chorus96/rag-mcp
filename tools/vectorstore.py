"""tools/vectorstore.py — Qdrant 컬렉션과 하이브리드 검색.

역할
  Qdrant 컬렉션 스키마, BM25 희소 벡터 계산, 하이브리드 질의를 한곳에서 다룹니다. ingest.py,
  server.py, documents.py가 모두 이 모듈을 거치므로, 쓰기 경로와 읽기 경로 사이에서 컬렉션 구조와
  벡터 이름이 어긋나지 않습니다.

벡터 구성 (Qdrant 명명된 벡터)
  - dense: 임베딩 엔드포인트가 만든 의미 벡터 (코사인 거리)
  - bm25:  FastEmbed `Qdrant/bm25`로 로컬에서 만든 키워드 벡터. IDF는 컬렉션의 Modifier.IDF로
           서버 측에서 적용하므로, 질의 쪽은 단어 존재 여부만 보내면 됩니다.
  밀집 벡터는 의미를 잡지만 에러 문자열(`CrashLoopBackOff`), 리소스 ID(`c-xxxxx`), 컴포넌트
  이름(`Longhorn`) 같은 정확한 토큰을 놓치기 쉽습니다. BM25가 이를 보완하고, 질의할 때 두 결과를
  Reciprocal Rank Fusion(RRF)으로 결합합니다.

공개 함수
  - ensure_collection: 컬렉션과 페이로드 인덱스 생성 (이미 있으면 그대로)
  - named_vectors:     포인트 하나의 벡터 묶음 구성
  - embed_documents_sparse / embed_query_sparse: BM25 희소 벡터
  - query:             하이브리드(기본) 또는 밀집 전용 검색
  - sparse_available / describe: 하이브리드 동작 여부와 설정

대체 동작
  FastEmbed를 불러올 수 없거나 RAG_HYBRID=false 이면 밀집 검색만 합니다. 검색은 계속 동작하고
  키워드 신호만 잃습니다.

주의
  RAG_HYBRID를 바꾸면 컬렉션 구조가 달라지므로 rag-ingest --recreate 로 재구축해야 합니다.
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

# --- 설정 ---------------------------------------------------------------------
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


# --- BM25 희소 벡터 -----------------------------------------------------------
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


# --- 컬렉션과 포인트 ----------------------------------------------------------
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


# --- 검색 ---------------------------------------------------------------------
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
    코사인 값으로 임계값을 판단할 때 사용하세요. 결합 점수는
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
