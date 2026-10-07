"""RAG 메모리를 위한 제공자 무관 크로스 인코더 리랭킹 (검색 2단계).

밀집 벡터 검색은 재현율은 좋지만 순서가 약합니다: 가장 좋은 청크가 상위 30개 안에는
자주 있지만 상위 5개 안에는 없을 때가 많습니다. 리랭커는 크로스 인코더로 각
(query, chunk) 쌍에 점수를 매겨 순서를 다시 정하며, 가장 적은 노력으로 가장 큰 정밀도
향상을 얻는 방법입니다 — 질의 시점에만 동작하므로 다시 수집할 필요가 없습니다.

원칙
----
- 기본으로 꺼져 있습니다(`RERANK_PROVIDER=none`). 운영자가 켜기 전까지 스택은 이전과
  똑같이 동작합니다 — .env에서 설정하면 되고 코드 변경은 필요 없습니다.
- 최선형(BEST-EFFORT): 어떤 실패든(잘못된 키, 엔드포인트 중단, 잘못된 형식의 응답)
  RerankError를 발생시키고 호출자는 원래의 밀집 검색 순서로 되돌아갑니다. 리랭킹
  때문에 검색이 깨져서는 절대 안 됩니다.

제공자
------
  none    — 비활성 (기본값).
  cohere  — Cohere/Jina 호환 rerank API: POST {base}/rerank 에
            {model, query, documents} -> {results:[{index, relevance_score}]}.
            Cohere(기본 base)와 Jina(RERANK_BASE_URL + RERANK_MODEL 설정)를
            지원합니다. openai 임베딩 경로처럼 표준 형식 하나입니다.

환경 변수
---------
  RERANK_PROVIDER    none | cohere            (기본값 none)
  RERANK_MODEL       rerank-english-v3.0      (Cohere) / jina-reranker-v2-... (Jina)
  RERANK_BASE_URL    https://api.cohere.com   (또는 https://api.jina.ai/v1)
  RERANK_API_KEY     제공자 API 키
  RERANK_CANDIDATES  리랭킹 전에 가져올 밀집 검색 결과 수 (기본값 30)
  RERANK_TIMEOUT     HTTP 타임아웃 초 (기본값 30)
"""

from __future__ import annotations

import os

import httpx

PROVIDER = os.environ.get("RERANK_PROVIDER", "none").strip().lower()
MODEL = os.environ.get("RERANK_MODEL", "rerank-english-v3.0")
BASE_URL = os.environ.get("RERANK_BASE_URL") or "https://api.cohere.com"
API_KEY = os.environ.get("RERANK_API_KEY", "")
CANDIDATES = int(os.environ.get("RERANK_CANDIDATES", "30"))
HTTP_TIMEOUT = float(os.environ.get("RERANK_TIMEOUT", "30"))


class RerankError(RuntimeError):
    """리랭킹 결과를 만들 수 없을 때 발생합니다. 호출자는 밀집 검색 순서로 되돌아갑니다."""


def enabled() -> bool:
    return PROVIDER not in ("", "none", "off", "false")


def describe() -> dict[str, object]:
    """현재 리랭크 설정의 요약, 비밀 값 제외 (상태 확인/로그용)."""
    return {"provider": PROVIDER, "model": MODEL, "candidates": CANDIDATES,
            "enabled": enabled()}


def _endpoint() -> str:
    base = BASE_URL.rstrip("/")
    if base.endswith("/rerank"):
        return base
    if base.endswith(("/v1", "/v2")):
        return f"{base}/rerank"
    return f"{base}/v2/rerank"  # Cohere 기본값


def rerank(query: str, documents: list[str]) -> list[tuple[int, float]]:
    """(query, doc) 쌍에 점수를 매기고 (원래 인덱스, 점수)를 좋은 순서대로 반환합니다.

    어떤 실패든 RerankError를 발생시켜 호출자가 입력 순서로 되돌아갈 수 있게 합니다.
    최대 len(documents)개를 반환합니다."""
    if not enabled():
        raise RerankError("reranker disabled")
    if not documents:
        return []

    if PROVIDER != "cohere":
        raise RerankError(f"unknown RERANK_PROVIDER={PROVIDER!r}; use 'none' or 'cohere'")

    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    try:
        resp = httpx.post(
            _endpoint(),
            headers=headers,
            json={"model": MODEL, "query": query, "documents": documents},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results")
    except httpx.HTTPStatusError as exc:
        raise RerankError(
            f"rerank endpoint returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}"
        ) from exc
    except httpx.RequestError as exc:
        raise RerankError(f"could not reach the rerank endpoint ({exc})") from exc
    except Exception as exc:  # noqa: BLE001 - 잘못된 형식의 JSON 등
        raise RerankError(f"rerank response could not be parsed ({exc})") from exc

    if not isinstance(results, list):
        raise RerankError("rerank response missing a 'results' list")

    ranked: list[tuple[int, float]] = []
    for item in results:
        idx = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if isinstance(idx, int) and 0 <= idx < len(documents) and score is not None:
            ranked.append((idx, float(score)))
    if not ranked:
        raise RerankError("rerank response contained no usable results")

    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked
