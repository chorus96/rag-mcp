"""tools/kiwi_bm25.py — 한국어 형태소 기반 BM25 희소 벡터 (기본 희소 모델 `kiwi-bm25`).

역할
  FastEmbed `Qdrant/bm25`와 같은 방식(단어 → 해시 → BM25 가중치, IDF는 Qdrant의 Modifier.IDF)으로
  희소 벡터를 만들되, 단어를 나눌 때 한국어 형태소 분석기 Kiwi(kiwipiepy)를 씁니다.
  `Qdrant/bm25`는 영어 기준으로 공백에서 단어를 나눠 "볼륨이"와 "볼륨을"을 다른 단어로 보지만,
  이 모듈은 둘 다 "볼륨"으로 맞춥니다.

토큰
  - 한글: Kiwi로 형태소를 나눈 뒤 내용어만 남깁니다 — 일반·고유 명사(NNG, NNP), 수사(NR),
    동사·형용사 어간(VV, VA), 어근(XR), 한자(SH). 조사·어미·접사는 버립니다.
      "볼륨이 멈췄어요" → ["볼륨", "멈추"]
  - 영문·숫자: 형태소 분석과 별개로, 원문에서 [A-Za-z0-9_] 연속 구간을 소문자로 그대로 씁니다.
    에러 문자열(CrashLoopBackOff), 리소스 이름(c-1a2b3c → c, 1a2b3c)이 지금처럼 정확히 맞습니다.
  - 40자를 넘는 토큰은 버립니다.

가중치
  - 문서: BM25 TF 부분  tf * (k + 1) / (tf + k * (1 - b + b * |d| / avg_len))   (k=1.2, b=0.75, avg_len=256)
  - 질의: 토큰마다 1.0 (중복 제거)
  - 토큰 ID: abs(mmh3.hash(token)) — FastEmbed BM25와 같은 해시
  IDF는 컬렉션의 Modifier.IDF로 Qdrant가 계산합니다.

주의
  토큰 방식이 다른 모델(`Qdrant/bm25`)로 만든 컬렉션과 섞어 쓸 수 없습니다. 모델을 바꾸면
  rag-ingest --recreate 로 재구축하세요.
"""

from __future__ import annotations

import re
import threading
from collections import Counter

import mmh3
from qdrant_client.models import SparseVector

MODEL_NAME = "kiwi-bm25"

# 남길 Kiwi 품사 (내용어). 영문(SL)·숫자(SN)는 원문에서 따로 뽑으므로 여기서는 빼 둡니다.
_KEEP_TAGS = frozenset({"NNG", "NNP", "NR", "VV", "VA", "XR", "SH"})
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_MAX_TOKEN_LEN = 40

# BM25 매개변수 (FastEmbed Bm25 기본값과 같음)
K = 1.2
B = 0.75
AVG_LEN = 256.0


class KiwiBM25:
    """Kiwi 형태소 분석으로 토큰을 만드는 BM25 희소 임베더."""

    def __init__(self) -> None:
        from kiwipiepy import Kiwi  # 지연 import: 설치되지 않았으면 호출자가 밀집 전용으로 대체

        self._kiwi = Kiwi()
        self._lock = threading.Lock()  # 서버가 여러 요청을 동시에 처리해도 안전하게

    # --- 토큰 ---------------------------------------------------------------
    def tokenize(self, text: str) -> list[str]:
        tokens = [w.lower() for w in _ASCII_WORD.findall(text)]
        if _HANGUL.search(text):
            with self._lock:
                morphs = self._kiwi.tokenize(text)
            tokens += [m.form for m in morphs if m.tag in _KEEP_TAGS]
        return [t for t in tokens if len(t) <= _MAX_TOKEN_LEN]

    @staticmethod
    def token_id(token: str) -> int:
        return abs(mmh3.hash(token))

    # --- 희소 벡터 ------------------------------------------------------------
    def embed_document(self, text: str) -> SparseVector:
        tokens = self.tokenize(text)
        counts = Counter(self.token_id(t) for t in tokens)
        norm = K * (1 - B + B * len(tokens) / AVG_LEN)
        ids = sorted(counts)
        return SparseVector(indices=ids, values=[counts[i] * (K + 1) / (counts[i] + norm) for i in ids])

    def embed_query(self, text: str) -> SparseVector:
        ids = sorted({self.token_id(t) for t in self.tokenize(text)})
        return SparseVector(indices=ids, values=[1.0] * len(ids))
