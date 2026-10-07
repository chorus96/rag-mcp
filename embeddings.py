"""
RAG 메모리를 위한 제공자 무관 임베딩.

지원:
- Ollama            (/api/embeddings, 단일 프롬프트)
- OpenAI 호환       (/v1/embeddings, 네이티브 배치 입력)

기능:
- 대칭/비대칭 모델을 코드에서 처리: 비대칭 모델(예: nomic)에는 질의/문서용 작업
  접두사가 자동으로 붙고, 대칭 모델(예: OpenAI text-embedding-*)에는 붙지 않습니다.
  EMBED_QUERY_PREFIX / EMBED_DOC_PREFIX 환경 변수를 명시하면 항상 자동 기본값보다
  우선합니다 (자동 감지가 모르는 모델 계열을 위한 탈출구, 예: "query:"/"passage:"를
  쓰는 e5/bge).
- rag-mcp 전체에서 쓰는 단일 공개 진입점 `embed(text, kind)`, 그리고 편의 래퍼
  `embed_query` / `embed_document` / `embed_documents`.
- HTTP 오류에 상태 코드 + 응답 본문을 담아, 엔드포인트/모델 문제(잘못된 모델, 404,
  인증)를 디버깅할 수 있게 합니다.

중요: 수집과 질의는 반드시 같은 제공자 + 모델을 사용해야 합니다. Qdrant 벡터는 모델에
종속되므로, 모델을 바꾸면 다시 수집해야 합니다. rag-mcp/README.md를 참고하세요.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


# =========================================================
# 설정
# =========================================================

@dataclass
class EmbeddingConfig:
    provider: str
    model: str
    base_url: str
    api_key: str | None
    query_prefix: str = ""
    doc_prefix: str = ""
    is_asymmetric: bool = False


def _build_config() -> EmbeddingConfig:
    provider = os.environ.get("EMBEDDINGS_PROVIDER", "ollama").strip().lower()
    model = os.environ.get("EMBEDDINGS_MODEL", "bge-m3")

    ollama_url = os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"
    openai_url = os.environ.get("EMBEDDINGS_BASE_URL") or "https://api.openai.com"
    api_key = os.environ.get("EMBEDDINGS_API_KEY")

    # 비대칭 모델 자동 감지 (질의/문서에 서로 다른 작업 접두사가 필요). nomic은 흔한
    # 오프라인 기본값입니다. 다른 계열(e5, bge, gte)도 접두사가 필요하지만 문자열이
    # 다르므로, 그런 경우 EMBED_*_PREFIX를 설정하세요.
    is_asymmetric = "nomic" in model.lower()

    query_prefix = os.environ.get(
        "EMBED_QUERY_PREFIX",
        "search_query: " if is_asymmetric else "",
    )
    doc_prefix = os.environ.get(
        "EMBED_DOC_PREFIX",
        "search_document: " if is_asymmetric else "",
    )

    base_url = ollama_url if provider == "ollama" else openai_url

    return EmbeddingConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        query_prefix=query_prefix,
        doc_prefix=doc_prefix,
        is_asymmetric=is_asymmetric,
    )


CONFIG = _build_config()
HTTP_TIMEOUT = float(os.environ.get("RAG_TIMEOUT_SECONDS", "60"))


# =========================================================
# 오류
# =========================================================

class EmbeddingError(RuntimeError):
    pass


def _http_error(provider: str, exc: httpx.HTTPStatusError) -> EmbeddingError:
    """상태 코드 + 응답 본문을 보존해 잘못된 모델/엔드포인트를 진단할 수 있게
    합니다 (예: Ollama 404 = 모델을 내려받지 않음)."""
    body = (exc.response.text or "").strip()
    detail = f" — {body}" if body else ""
    return EmbeddingError(f"{provider} embeddings HTTP {exc.response.status_code}{detail}")


# =========================================================
# 헬퍼
# =========================================================

def describe() -> dict[str, str]:
    return {
        "provider": CONFIG.provider,
        "model": CONFIG.model,
        "base_url": CONFIG.base_url,
        "asymmetric": str(CONFIG.is_asymmetric),
    }


def _apply_prefix(text: str, kind: str) -> str:
    # 설정된 접두사를 그대로 붙입니다 — 대칭 모델의 ""는 아무 효과가 없습니다.
    # `is_asymmetric`은 _build_config에서 기본 접두사를 고를 때만 쓰입니다. 여기서
    # 이 값으로 막으면 자동 감지가 모르는 계열(e5/bge/gte)에 명시한 EMBED_*_PREFIX가
    # 조용히 무시되므로, 여기서는 막지 않습니다.
    if kind not in ("query", "document"):
        raise ValueError("kind must be 'query' or 'document'")
    prefix = CONFIG.query_prefix if kind == "query" else CONFIG.doc_prefix
    return f"{prefix}{text}"


def _openai_endpoint() -> str:
    base = CONFIG.base_url.rstrip("/")
    return f"{base}/embeddings" if base.endswith("/v1") else f"{base}/v1/embeddings"


# =========================================================
# 제공자
# =========================================================

def _embed_ollama_one(text: str) -> list[float]:
    """Ollama의 레거시 /api/embeddings는 단일 프롬프트입니다: {"prompt": str} ->
    {"embedding": [...]}. 목록을 받지 않으므로 배치는 반복문으로 처리합니다."""
    try:
        resp = httpx.post(
            f"{CONFIG.base_url.rstrip('/')}/api/embeddings",
            json={"model": CONFIG.model, "prompt": text},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _http_error("ollama", exc) from exc
    except httpx.RequestError as exc:
        raise EmbeddingError(f"ollama connection failed: {exc}") from exc

    vector = resp.json().get("embedding")
    if not vector:
        raise EmbeddingError(f"ollama returned no embedding for model {CONFIG.model!r}")
    return vector


def _embed_openai(texts: list[str]) -> list[list[float]]:
    """OpenAI 호환 /v1/embeddings는 배치 `input` 배열을 기본으로 받습니다."""
    headers = {"Authorization": f"Bearer {CONFIG.api_key}"} if CONFIG.api_key else {}
    try:
        resp = httpx.post(
            _openai_endpoint(),
            headers=headers,
            json={"model": CONFIG.model, "input": texts},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _http_error("openai", exc) from exc
    except httpx.RequestError as exc:
        raise EmbeddingError(f"openai connection failed: {exc}") from exc

    # `index`로 다시 정렬 — API가 요청 순서와 다르게 항목을 반환할 수 있습니다.
    data = sorted(resp.json().get("data") or [], key=lambda d: d.get("index", 0))
    vectors = [item["embedding"] for item in data]
    if len(vectors) != len(texts):
        raise EmbeddingError(
            f"openai returned {len(vectors)} embeddings for {len(texts)} input(s)"
        )
    return vectors


def _embed_batch(texts: list[str]) -> list[list[float]]:
    if CONFIG.provider == "ollama":
        return [_embed_ollama_one(t) for t in texts]
    if CONFIG.provider == "openai":
        return _embed_openai(texts)
    raise EmbeddingError(f"unknown embeddings provider: {CONFIG.provider!r}")


# =========================================================
# 공개 API — rag-mcp의 다른 부분이 호출하는 것
# =========================================================

def embed(text: str, kind: str) -> list[float]:
    """텍스트 하나를 임베딩합니다. `kind`는 'query' 또는 'document'이며 비대칭 작업
    접두사를 고릅니다. server.py / ingest.py / capture.py가 쓰는 진입점입니다."""
    return _embed_batch([_apply_prefix(text, kind)])[0]


def embed_query(text: str) -> list[float]:
    return embed(text, "query")


def embed_document(text: str) -> list[float]:
    return embed(text, "document")


def embed_documents(texts: list[str]) -> list[list[float]]:
    """문서 배치 임베딩 (OpenAI는 HTTP 호출 한 번, Ollama는 반복 호출)."""
    return _embed_batch([_apply_prefix(t, "document") for t in texts])
