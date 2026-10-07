"""tools/reranker.py — 크로스 인코더 리랭킹 (선택 사항).

역할
  검색 후보의 순서를 크로스 인코더로 다시 매겨 정밀도를 높입니다. 밀집 검색은 재현율은 좋지만
  순서가 약해서, 가장 좋은 청크가 상위 30개 안에는 있어도 상위 5개 안에는 없을 때가 많습니다.
  리랭커가 (질의, 청크) 쌍마다 점수를 매겨 이를 바로잡습니다. 질의할 때만 동작하므로 켜고 끌 때
  재수집이 필요 없습니다.

공개 함수
  - enabled():               리랭킹이 켜져 있는지
  - rerank(query, documents): (원래 인덱스, 점수) 목록을 좋은 순서대로 반환
  - describe():              현재 설정 요약 (비밀 값 제외)

동작
  - 기본값은 꺼짐(RERANK_PROVIDER=none)입니다. 설정 파일에서 켜면 되고 코드 변경은 필요 없습니다.
  - 최선형(best-effort): 잘못된 키, 엔드포인트 중단, 잘못된 응답 등 어떤 실패든 RerankError를 내고,
    호출자(server.py)는 원래 검색 순서를 그대로 씁니다. 리랭킹 때문에 검색이 깨지면 안 됩니다.

제공자
  none    비활성 (기본값)
  cohere  Cohere/Jina 호환 rerank API
          POST {base}/rerank  {model, query, documents} -> {results: [{index, relevance_score}]}
          Cohere(기본 주소)와 Jina(RERANK_BASE_URL, RERANK_MODEL 지정)를 지원합니다.

환경 변수
  RERANK_PROVIDER    none | cohere              (기본값 none)
  RERANK_MODEL       rerank-multilingual-v3.0   (Cohere 다국어) / jina-reranker-v2-base-multilingual (Jina)
  RERANK_BASE_URL    https://api.cohere.com     (Jina: https://api.jina.ai/v1)
  RERANK_API_KEY     제공자 API 키
  RERANK_CANDIDATES  리랭킹 전에 가져올 후보 수 (기본값 30)
  RERANK_TIMEOUT     HTTP 타임아웃 초 (기본값 30)
"""

from __future__ import annotations

import os

import httpx

# --- 설정 ---------------------------------------------------------------------
PROVIDER = os.environ.get("RERANK_PROVIDER", "none").strip().lower()
MODEL = os.environ.get("RERANK_MODEL", "rerank-multilingual-v3.0")
BASE_URL = os.environ.get("RERANK_BASE_URL") or "https://api.cohere.com"
API_KEY = os.environ.get("RERANK_API_KEY", "")
CANDIDATES = int(os.environ.get("RERANK_CANDIDATES", "30"))
HTTP_TIMEOUT = float(os.environ.get("RERANK_TIMEOUT", "30"))


# --- 리랭킹 -------------------------------------------------------------------
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
