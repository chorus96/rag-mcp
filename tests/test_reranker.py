"""tools/reranker.py 테스트 — 리랭킹.

확인하는 것
  - 기본값은 꺼짐(RERANK_PROVIDER=none)이며, 꺼져 있으면 외부 호출을 하지 않음
  - 엔드포인트 주소 구성: Cohere(/v2/rerank), Jina(/v1/rerank), 이미 완성된 주소
  - 응답 처리: 관련도 순으로 정렬, 범위를 벗어난 인덱스는 버림, 빈 입력은 호출 없이 빈 결과
  - 최선형(best-effort) 계약: 네트워크 오류나 잘못된 응답은 RerankError 를 내어, 호출자가
    원래 검색 순서로 되돌아갈 수 있게 함 (리랭킹 때문에 검색이 깨지면 안 됨)

방법
  - httpx.post 를 가짜 함수로 바꿔 실제 API를 호출하지 않습니다.
"""

import sys
from pathlib import Path

import pytest

# 소스가 tools/ 에 있으므로 import 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import reranker  # noqa: E402


# --- 활성화 여부 --------------------------------------------------------------

def test_disabled_by_default():
    # 기본 환경에서는 RERANK_PROVIDER가 설정되지 않음 -> "none".
    assert reranker.enabled() is False


def test_rerank_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(reranker, "PROVIDER", "none")
    monkeypatch.setattr(reranker.httpx, "post", _boom)  # 외부 호출조차 하면 안 됨
    with pytest.raises(reranker.RerankError):
        reranker.rerank("q", ["a", "b"])


# --- 엔드포인트 주소 ----------------------------------------------------------

@pytest.mark.parametrize("base,expected", [
    ("https://api.cohere.com", "https://api.cohere.com/v2/rerank"),
    ("https://api.jina.ai/v1", "https://api.jina.ai/v1/rerank"),
    ("https://x/v2", "https://x/v2/rerank"),
    ("https://x/rerank", "https://x/rerank"),
    ("https://api.cohere.com/", "https://api.cohere.com/v2/rerank"),
])
def test_endpoint_building(monkeypatch, base, expected):
    monkeypatch.setattr(reranker, "BASE_URL", base)
    assert reranker._endpoint() == expected


# --- 응답 처리 ----------------------------------------------------------------

def test_rerank_orders_by_relevance_and_maps_indices(monkeypatch):
    monkeypatch.setattr(reranker, "PROVIDER", "cohere")
    # 순서가 뒤섞인 결과: 문서 인덱스 2가 가장 관련성이 높고, 그다음 0, 1 순.
    payload = {"results": [
        {"index": 0, "relevance_score": 0.4},
        {"index": 2, "relevance_score": 0.9},
        {"index": 1, "relevance_score": 0.1},
    ]}
    monkeypatch.setattr(reranker.httpx, "post", _fake_post(payload))
    order = reranker.rerank("q", ["a", "b", "c"])
    assert [i for i, _ in order] == [2, 0, 1]        # 가장 좋은 것부터
    assert order[0][1] == 0.9


def test_rerank_ignores_out_of_range_indices(monkeypatch):
    monkeypatch.setattr(reranker, "PROVIDER", "cohere")
    payload = {"results": [{"index": 5, "relevance_score": 0.9},
                           {"index": 0, "relevance_score": 0.3}]}
    monkeypatch.setattr(reranker.httpx, "post", _fake_post(payload))
    order = reranker.rerank("q", ["a", "b"])
    assert order == [(0, 0.3)]                        # 인덱스 5는 버려짐


def test_rerank_empty_docs_returns_empty(monkeypatch):
    monkeypatch.setattr(reranker, "PROVIDER", "cohere")
    monkeypatch.setattr(reranker.httpx, "post", _boom)
    assert reranker.rerank("q", []) == []             # 호출도 예외도 없음


# --- 오류 처리 (최선형 계약) --------------------------------------------------

def test_rerank_network_error_raises(monkeypatch):
    monkeypatch.setattr(reranker, "PROVIDER", "cohere")
    monkeypatch.setattr(reranker.httpx, "post", _boom_request)
    with pytest.raises(reranker.RerankError):
        reranker.rerank("q", ["a"])


def test_rerank_malformed_body_raises(monkeypatch):
    monkeypatch.setattr(reranker, "PROVIDER", "cohere")
    monkeypatch.setattr(reranker.httpx, "post", _fake_post({"nope": True}))
    with pytest.raises(reranker.RerankError):
        reranker.rerank("q", ["a"])


# --- 테스트 도우미 ------------------------------------------------------------

def _boom(*_a, **_k):
    raise AssertionError("httpx.post should not have been called")


def _boom_request(*_a, **_k):
    raise reranker.httpx.RequestError("connection refused")


def _fake_post(payload):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def _post(*_a, **_k):
        return _Resp()

    return _post
