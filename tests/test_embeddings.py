"""tools/embeddings.py 테스트 — 사내 임베딩 서버 호출.

확인하는 것
  - 주소 구성: 기본 /v1/embeddings, 주소가 /v1 로 끝나면 /embeddings 만
  - 응답 처리: index 순으로 정렬, 개수가 맞지 않으면 오류
  - 오류 메시지: 주소 미설정, HTTP 오류(상태 코드와 본문), OpenAI 형식이 아닌 응답

방법
  - httpx.post 를 가짜 함수로 바꿔 실제 서버를 호출하지 않습니다.
"""

import sys
from pathlib import Path

import httpx
import pytest

# 소스가 tools/ 에 있으므로 import 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import embeddings  # noqa: E402


class _Resp:
    def __init__(self, body, status=200, text=""):
        self._body, self.status_code, self.text = body, status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code, text=self.text))

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(embeddings.CONFIG, "base_url", "http://embedding.internal:8080")
    calls = []

    def respond(resp):
        monkeypatch.setattr(embeddings.httpx, "post", lambda url, **kw: calls.append(url) or resp)
        return calls

    return respond


@pytest.mark.parametrize("base,expected", [
    ("http://embedding.internal:8080", "http://embedding.internal:8080/v1/embeddings"),
    ("http://embedding.internal:8080/", "http://embedding.internal:8080/v1/embeddings"),
    ("https://gw.internal/embedding/v1", "https://gw.internal/embedding/v1/embeddings"),
])
def test_endpoint(monkeypatch, base, expected):
    monkeypatch.setattr(embeddings.CONFIG, "base_url", base)
    assert embeddings._openai_endpoint() == expected


def test_vectors_are_sorted_by_index(server):
    server(_Resp({"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}))
    assert embeddings.embed_documents(["a", "b"]) == [[1.0], [2.0]]


def test_missing_base_url(monkeypatch):
    monkeypatch.setattr(embeddings.CONFIG, "base_url", "")
    with pytest.raises(embeddings.EmbeddingError, match="EMBEDDINGS_BASE_URL is not set"):
        embeddings.embed("q", "query")


def test_http_error_keeps_status_and_body(server):
    server(_Resp(None, status=401, text='{"error":"invalid api key"}'))
    with pytest.raises(embeddings.EmbeddingError, match=r'HTTP 401 — \{"error":"invalid api key"\}'):
        embeddings.embed("q", "query")


@pytest.mark.parametrize("body", [
    ValueError("Expecting value"),               # JSON 이 아님
    {"data": [{"index": 0, "vector": [0.1]}]},   # embedding 키 없음
    [{"embedding": [0.1]}],                      # data 로 감싸지 않은 목록
])
def test_non_openai_response_is_clear_error(server, body):
    server(_Resp(body))
    with pytest.raises(embeddings.EmbeddingError, match="not in the OpenAI format"):
        embeddings.embed("q", "query")


def test_count_mismatch(server):
    server(_Resp({"data": []}))
    with pytest.raises(embeddings.EmbeddingError, match="returned 0 embeddings for 1 input"):
        embeddings.embed("q", "query")
