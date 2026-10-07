"""수집: 지원 파일 탐색, PDF 텍스트 추출, 쓰기 배치.

PDF 추출은 가짜 pypdf로 검증하므로 실제 PDF가 필요 없습니다.
페이지가 `# [Page N]` 섹션이 되는지(섹션 인식 청킹이 페이지 맥락을 유지하도록),
빈 페이지/이미지 전용 페이지를 건너뛰는지, 탐색이 .md와 .pdf 파일만 찾고
나머지는 무시하는지 확인합니다.

배치 테스트는 가짜 Qdrant 클라이언트로 문서별 쓰기 경로를 검증합니다:
임베딩은 청크 순서를 유지한 채 EMBED_BATCH_SIZE 크기의 요청으로 나가고,
업서트는 UPSERT_BATCH_SIZE 포인트로 제한되며, 실행 사이에 줄어든 문서는
고아가 된 뒷부분 청크가 삭제됩니다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import ingest  # noqa: E402


def test_discover_files_picks_md_and_pdf_only(tmp_path):
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "runbooks" / "b.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "runbooks" / "c.PDF").write_bytes(b"%PDF-1.4")
    (tmp_path / "runbooks" / "d.txt").write_text("x", encoding="utf-8")
    (tmp_path / "runbooks" / "e.yaml").write_text("x", encoding="utf-8")

    found = [p.name for p in ingest._discover_files(tmp_path)]
    assert found == ["a.md", "b.pdf", "c.PDF"]
    assert "d.txt" not in found and "e.yaml" not in found


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdfReader:
    def __init__(self, pages):
        self.pages = pages


def _patch_pdf(monkeypatch, pages):
    import types

    fake = types.SimpleNamespace(PdfReader=lambda _path: _FakePdfReader(pages))
    monkeypatch.setitem(sys.modules, "pypdf", fake)


def test_extract_pdf_text_builds_page_sections(monkeypatch):
    _patch_pdf(monkeypatch, [_FakePage("Page one body"), _FakePage("Page two body")])
    text = ingest._extract_pdf_text(Path("unused.pdf"))
    assert text == "# [Page 1]\nPage one body\n\n# [Page 2]\nPage two body"


def test_extract_pdf_text_skips_empty_pages(monkeypatch):
    _patch_pdf(monkeypatch, [_FakePage("   "), _FakePage("Real text"), _FakePage(None)])
    text = ingest._extract_pdf_text(Path("unused.pdf"))
    assert text == "# [Page 2]\nReal text"


def test_extract_pdf_text_empty_for_image_only_pdf(monkeypatch):
    _patch_pdf(monkeypatch, [_FakePage(""), _FakePage(" \n ")])
    assert ingest._extract_pdf_text(Path("unused.pdf")) == ""


# ---------------------------------------------------------------------------
# 임베딩 배치
# ---------------------------------------------------------------------------

def test_embed_batch_splits_requests_and_keeps_order(monkeypatch):
    """청크마다 요청 하나가 아니라 EMBED_BATCH_SIZE개 청크마다 요청 하나를 보내고,
    반환된 벡터가 입력 청크와 같은 순서를 유지하는지 확인합니다."""
    sizes = []

    def fake_embed_documents(texts):
        sizes.append(len(texts))
        # 자기 텍스트를 담은 벡터를 만들어, 순서가 뒤섞이면 알아챌 수 있게 합니다.
        return [[float(len(t)), float(ord(t[0]))] for t in texts]

    monkeypatch.setattr(ingest.embeddings, "embed_documents", fake_embed_documents)
    monkeypatch.setattr(ingest, "EMBED_BATCH_SIZE", 3)

    texts = ["a", "bb", "ccc", "dddd", "eeeee", "ffffff", "g"]
    vectors = ingest._embed_batch(texts)

    assert sizes == [3, 3, 1]  # 텍스트 하나씩 7번 호출이 아니라 배치로 처리
    assert vectors == [[float(len(t)), float(ord(t[0]))] for t in texts]


def test_embed_batch_empty_input_makes_no_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ingest.embeddings, "embed_documents", lambda texts: calls.append(texts) or []
    )
    assert ingest._embed_batch([]) == []
    assert calls == []


# ---------------------------------------------------------------------------
# 업서트 배치 + 오래된 청크 정리
# ---------------------------------------------------------------------------

class _FakeCount:
    def __init__(self, count):
        self.count = count


class _FakeClient:
    """upsert/count/delete 호출을 기록합니다. `orphans`는 count()가 반환하는 값입니다."""

    def __init__(self, orphans=0, count_raises=None):
        self.upserts = []          # 요청마다 하나씩, 포인트 ID 목록의 목록
        self.deletes = []          # 기록된 points_selector 값
        self.orphans = orphans
        self.count_raises = count_raises
        self.count_filters = []

    def upsert(self, collection_name, points, wait=None):
        self.upserts.append([p.id for p in points])

    def count(self, collection_name, count_filter=None, exact=True):
        self.count_filters.append(count_filter)
        if self.count_raises:
            raise self.count_raises
        return _FakeCount(self.orphans)

    def delete(self, collection_name, points_selector):
        self.deletes.append(points_selector)


def _points(n):
    from qdrant_client.models import PointStruct

    return [
        PointStruct(id=i + 1, vector={"dense": [0.0, 1.0]}, payload={"chunk": i})
        for i in range(n)
    ]


def test_upsert_points_caps_points_per_request(monkeypatch):
    monkeypatch.setattr(ingest, "UPSERT_BATCH_SIZE", 4)
    client = _FakeClient()

    ingest._upsert_points(client, _points(10))

    assert [len(batch) for batch in client.upserts] == [4, 4, 2]
    # 모든 포인트가 순서대로 정확히 한 번씩 기록됨.
    assert [pid for batch in client.upserts for pid in batch] == list(range(1, 11))


def test_upsert_points_single_request_when_under_batch_size(monkeypatch):
    monkeypatch.setattr(ingest, "UPSERT_BATCH_SIZE", 64)
    client = _FakeClient()

    ingest._upsert_points(client, _points(5))

    assert len(client.upserts) == 1


def test_delete_orphan_chunks_targets_only_the_tail():
    client = _FakeClient(orphans=7)

    ingest._delete_orphan_chunks(client, "runbooks/longhorn.md", kept=20)

    assert len(client.deletes) == 1
    conditions = client.deletes[0].filter.must
    assert conditions[0].key == "source"
    assert conditions[0].match.value == "runbooks/longhorn.md"
    # 현재 개수 이상의 청크만 대상 — 남아 있는 0..19번은 건드리지 않음.
    assert conditions[1].key == "chunk"
    assert conditions[1].range.gte == 20


def test_delete_orphan_chunks_noop_when_nothing_stale():
    client = _FakeClient(orphans=0)

    ingest._delete_orphan_chunks(client, "runbooks/longhorn.md", kept=20)

    assert client.deletes == []  # 삭제 요청 자체가 없음


def test_delete_orphan_chunks_survives_qdrant_error(caplog):
    """정리는 최선형(best-effort)입니다: 오래된 검색 결과 때문에 수집을 중단할 가치는 없습니다."""
    client = _FakeClient(count_raises=RuntimeError("qdrant down"))

    ingest._delete_orphan_chunks(client, "runbooks/longhorn.md", kept=3)

    assert client.deletes == []
    assert "stale-chunk cleanup failed" in caplog.text
