"""tools/reranker.py — 크로스 인코더 리랭킹 (선택 사항).

역할
  검색 후보의 순서를 사내 리랭커(크로스 인코더)로 다시 매겨 정밀도를 높입니다. 밀집 검색은 재현율은
  좋지만 순서가 약해서, 가장 좋은 청크가 상위 30개 안에는 있어도 상위 5개 안에는 없을 때가 많습니다.
  리랭커가 (질의, 청크) 쌍마다 점수를 매겨 이를 바로잡습니다. 질의할 때만 동작하므로 켜고 끌 때
  재수집이 필요 없습니다. rag-mcp는 리랭커 모델을 직접 실행하지 않습니다.

공개 함수
  - enabled():               리랭킹이 켜져 있는지 (RERANK_BASE_URL이 있으면 켜짐)
  - rerank(query, documents): (원래 인덱스, 점수) 목록을 좋은 순서대로 반환
  - describe():              현재 설정 요약 (비밀 값 제외)

동작
  - RERANK_BASE_URL(사내 리랭커 주소)에는 기본값이 없습니다. 비어 있으면 리랭킹은 꺼져 있고, 검색 후보가
    어디로도 전송되지 않습니다. 지정하면 켜지며 코드 변경은 필요 없습니다.
  - 최선형(best-effort): 잘못된 주소·키, 서버 중단, 잘못된 응답 등 어떤 실패든 RerankError를 내고,
    호출자(server.py)는 원래 검색 순서를 그대로 씁니다. 리랭킹 때문에 검색이 깨지면 안 됩니다.

사내 리랭커 API (Cohere 호환 rerank — vLLM, Infinity, Xinference 등이 제공하는 형식)
  POST {base}/v1/rerank  {model, query, documents} -> {results: [{index, relevance_score}]}
  주소는 RERANK_BASE_URL에서 만듭니다: 끝이 /rerank 면 그대로, /v1·/v2 로 끝나면 /rerank 를, 그 밖에는
  /v1/rerank 를 붙입니다. 인증이 있으면 Authorization: Bearer <RERANK_API_KEY>로 보냅니다.

환경 변수
  RERANK_BASE_URL    사내 리랭커 주소 (기본값 없음 — 비어 있으면 리랭킹 꺼짐)
  RERANK_MODEL       사내 리랭커의 모델 이름 (기본값 bge-reranker-v2-m3)
  RERANK_API_KEY     인증 토큰 (없으면 비워 둠)
  RERANK_CANDIDATES  리랭킹 전에 가져올 후보 수 (기본값 30)
  RERANK_TIMEOUT     HTTP 타임아웃 초 (기본값 30)
"""

from __future__ import annotations

import os

import httpx

# --- 설정 ---------------------------------------------------------------------
BASE_URL = os.environ.get("RERANK_BASE_URL", "").strip()
MODEL = os.environ.get("RERANK_MODEL") or "bge-reranker-v2-m3"
API_KEY = os.environ.get("RERANK_API_KEY", "")
CANDIDATES = int(os.environ.get("RERANK_CANDIDATES", "30"))
HTTP_TIMEOUT = float(os.environ.get("RERANK_TIMEOUT", "30"))


# --- 리랭킹 -------------------------------------------------------------------
class RerankError(RuntimeError):
    """리랭킹 결과를 만들 수 없을 때 발생합니다. 호출자는 원래 검색 순서로 되돌아갑니다."""


def enabled() -> bool:
    return bool(BASE_URL)


def describe() -> dict[str, object]:
    """현재 리랭크 설정의 요약, 비밀 값 제외 (상태 확인/로그용)."""
    return {"enabled": enabled(), "model": MODEL, "base_url": BASE_URL or None,
            "candidates": CANDIDATES}


def _endpoint() -> str:
    base = BASE_URL.rstrip("/")
    if base.endswith("/rerank"):
        return base
    if base.endswith(("/v1", "/v2")):
        return f"{base}/rerank"
    return f"{base}/v1/rerank"


def rerank(query: str, documents: list[str]) -> list[tuple[int, float]]:
    """(query, doc) 쌍에 점수를 매기고 (원래 인덱스, 점수)를 좋은 순서대로 반환합니다.

    어떤 실패든 RerankError를 발생시켜 호출자가 입력 순서로 되돌아갈 수 있게 합니다.
    최대 len(documents)개를 반환합니다."""
    if not enabled():
        raise RerankError("reranker disabled (RERANK_BASE_URL is not set)")
    if not documents:
        return []

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
            f"in-house reranker returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:200]}"
        ) from exc
    except httpx.RequestError as exc:
        raise RerankError(f"could not reach the in-house reranker at {_endpoint()} ({exc})") from exc
    except Exception as exc:  # noqa: BLE001 - 잘못된 형식의 JSON 등
        raise RerankError(f"rerank response could not be parsed ({exc})") from exc

    if not isinstance(results, list):
        raise RerankError("rerank response missing a 'results' list (expected the Cohere-compatible format)")

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
