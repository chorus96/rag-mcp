"""tools/kiwi_bm25.py 테스트 — 한국어 형태소 기반 BM25 희소 벡터.

확인하는 것
  - 토큰: 조사·어미를 떼어 내용어만 남김 ("볼륨이"/"볼륨을" → "볼륨"), 영문·숫자는 원문 그대로 소문자로 남김
  - 질의 벡터: 토큰마다 1.0, 중복 제거
  - 문서 벡터: BM25 TF 가중치 (같은 단어가 많을수록 커지되 포화됨)
  - 조사만 다른 문장끼리 같은 토큰 ID를 가짐 (키워드 검색에서 서로 맞음)
  - vectorstore 가 Kiwi BM25로 희소 벡터를 만듦

방법
  - 실제 Kiwi(kiwipiepy)를 씁니다. 설치되어 있지 않으면 건너뜁니다.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

pytest.importorskip("kiwipiepy")

import kiwi_bm25  # noqa: E402
import vectorstore  # noqa: E402


@pytest.fixture(scope="module")
def bm25():
    return kiwi_bm25.KiwiBM25()


# --- 토큰 ---------------------------------------------------------------------
def test_particles_and_endings_are_removed(bm25):
    assert bm25.tokenize("볼륨이 멈췄어요") == ["볼륨", "멈추"]
    assert set(bm25.tokenize("볼륨을 분리합니다")) == {"볼륨", "분리"}


def test_ascii_tokens_are_kept_verbatim(bm25):
    toks = bm25.tokenize("파드가 CrashLoopBackOff 상태, c-1a2b3c 확인")
    assert "crashloopbackoff" in toks and "c" in toks and "1a2b3c" in toks
    assert "파드" in toks and "상태" in toks and "확인" in toks
    # 영문만 있는 문장은 형태소 분석 없이 처리합니다.
    assert bm25.tokenize("FailedAttachVolume event") == ["failedattachvolume", "event"]


def test_overlong_tokens_are_dropped(bm25):
    assert bm25.tokenize("a" * 41 + " ok") == ["ok"]


# --- 희소 벡터 -----------------------------------------------------------------
def test_query_vector_is_unique_tokens_with_weight_one(bm25):
    sv = bm25.embed_query("볼륨 볼륨이 볼륨을")
    assert sv.indices == [bm25.token_id("볼륨")] and sv.values == [1.0]


def test_document_vector_uses_bm25_tf(bm25):
    one = bm25.embed_document("볼륨")
    three = bm25.embed_document("볼륨 볼륨 볼륨")
    assert one.indices == three.indices == [bm25.token_id("볼륨")]
    assert 0 < one.values[0] < three.values[0] < kiwi_bm25.K + 1  # 많을수록 크지만 포화


def test_particle_variants_share_token_ids(bm25):
    q = set(bm25.embed_query("볼륨이 멈췄어요").indices)
    d = set(bm25.embed_document("노드 재부팅 뒤 볼륨을 다시 붙였더니 멈추지 않았다").indices)
    assert q <= d


# --- vectorstore 연결 ------------------------------------------------------------
def test_vectorstore_uses_kiwi_bm25(monkeypatch):
    monkeypatch.setattr(vectorstore, "_bm25", None)
    monkeypatch.setattr(vectorstore, "_bm25_loaded", False)
    monkeypatch.setattr(vectorstore, "_HYBRID_REQUESTED", True)
    assert isinstance(vectorstore._load_bm25(), kiwi_bm25.KiwiBM25)
    assert vectorstore.describe() == {"hybrid": True, "sparse_model": "kiwi-bm25"}
    docs = vectorstore.embed_documents_sparse(["볼륨이 멈춤", "인증서 갱신"])
    assert len(docs) == 2 and all(d.indices for d in docs)
    assert vectorstore.embed_query_sparse("볼륨").indices == [kiwi_bm25.KiwiBM25.token_id("볼륨")]
