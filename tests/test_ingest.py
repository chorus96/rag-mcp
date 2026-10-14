"""tools/ingest.py 테스트 — 문서 수집.

확인하는 것
  - 파일 탐색: official/ 아래에서 .md 만 찾고 나머지 확장자(.pdf 등)는 무시. official/ 밖의 문서는
    수집하지 않고 건너뛴 개수를 경고로 알림
  - 페이로드: 파일 전체가 본문(front matter 해석 없음), 제목은 파일 이름, 페이로드는 text/title/source/chunk 뿐
  - 임베딩 배치: EMBED_BATCH_SIZE 단위로 요청하고, 벡터 순서가 청크 순서와 같음
  - 업서트 배치: 요청당 UPSERT_BATCH_SIZE 포인트를 넘지 않음
  - 오래된 청크 정리: 문서가 줄어들면 남은 뒷부분 청크만 지우고, 실패해도 수집을 멈추지 않음

방법
  - 임베딩과 Qdrant 클라이언트를 가짜 객체로 바꿔 서버 없이 실행합니다.
  - 파일이 필요한 테스트는 pytest 임시 폴더를 문서 디렉터리로 쓰고, source 예시는 official/... 경로를 씁니다.
"""

import sys
from pathlib import Path

# 소스가 tools/ 에 있으므로 import 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import ingest  # noqa: E402


# --- 파일 탐색 ----------------------------------------------------------------

def test_discover_files_picks_md_only(tmp_path):
    (tmp_path / "official").mkdir()
    (tmp_path / "official" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "official" / "b.MD").write_text("x", encoding="utf-8")
    (tmp_path / "official" / "c.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "official" / "d.txt").write_text("x", encoding="utf-8")
    (tmp_path / "official" / "e.yaml").write_text("x", encoding="utf-8")

    found = [p.name for p in ingest._discover_files(tmp_path)]
    assert found == ["a.md", "b.MD"]


def test_discover_files_only_official(tmp_path, caplog):
    for rel in ("official/a.md", "official/team-a/b.md", "draft/c.md",
                "notes/old.md", "top.md", "other/x.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")

    found = [str(p.relative_to(tmp_path)) for p in ingest._discover_files(tmp_path)]
    # official/ 밖의 문서(draft/ 포함)는 수집하지 않고, 건너뛴 개수를 경고로 알립니다.
    assert found == ["official/a.md", "official/team-a/b.md"]
    assert "ignored 4 document(s)" in caplog.text


# --- 임베딩 배치 --------------------------------------------------------------

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


# --- 업서트 배치 + 오래된 청크 정리 -------------------------------------------

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

    ingest._delete_orphan_chunks(client, "official/longhorn.md", kept=20)

    assert len(client.deletes) == 1
    conditions = client.deletes[0].filter.must
    assert conditions[0].key == "source"
    assert conditions[0].match.value == "official/longhorn.md"
    # 현재 개수 이상의 청크만 대상 — 남아 있는 0..19번은 건드리지 않음.
    assert conditions[1].key == "chunk"
    assert conditions[1].range.gte == 20


def test_delete_orphan_chunks_noop_when_nothing_stale():
    client = _FakeClient(orphans=0)

    ingest._delete_orphan_chunks(client, "official/longhorn.md", kept=20)

    assert client.deletes == []  # 삭제 요청 자체가 없음


def test_delete_orphan_chunks_survives_qdrant_error(caplog):
    """정리는 최선형(best-effort)입니다: 오래된 검색 결과 때문에 수집을 중단할 가치는 없습니다."""
    client = _FakeClient(count_raises=RuntimeError("qdrant down"))

    ingest._delete_orphan_chunks(client, "official/longhorn.md", kept=3)

    assert client.deletes == []
    assert "stale-chunk cleanup failed" in caplog.text


# --- 파일 하나 색인 (페이로드) --------------------------------------------------

def test_ingest_file_payload_has_no_metadata(monkeypatch, tmp_path):
    doc = tmp_path / "official" / "longhorn-volume-attach.md"
    doc.parent.mkdir()
    # front matter처럼 생긴 줄도 해석하지 않고 본문으로 둡니다.
    doc.write_text("---\ncomponent: longhorn\n---\n# 증상\n볼륨이 멈춤\n", encoding="utf-8")
    monkeypatch.setattr(ingest, "_embed_batch", lambda texts: [[0.1, 0.2] for _ in texts])
    monkeypatch.setattr(ingest.vectorstore, "embed_documents_sparse", lambda texts: [None] * len(texts))
    saved = []
    monkeypatch.setattr(ingest, "_upsert_points", lambda client, points: saved.extend(points))
    monkeypatch.setattr(ingest, "_delete_orphan_chunks", lambda *a: None)

    n = ingest.ingest_file(object(), doc, tmp_path, ensure=False)

    assert n == len(saved) >= 1
    payload = saved[0].payload
    assert set(payload) == {"text", "title", "source", "chunk"}
    assert payload["title"] == "longhorn-volume-attach"
    assert payload["source"] == "official/longhorn-volume-attach.md"
    assert "component: longhorn" in "\n".join(p.payload["text"] for p in saved)


# --- 임베딩 서버 장애 ----------------------------------------------------------

def test_ingest_stops_cleanly_on_embedding_error(monkeypatch, tmp_path, caplog):
    for name in ("a", "b", "c"):
        (tmp_path / "official").mkdir(exist_ok=True)
        (tmp_path / "official" / f"{name}.md").write_text(f"# {name}\n본문", encoding="utf-8")
    saved = []

    def fake_ingest_file(client, file, root, **kw):
        if file.name == "b.md":
            raise ingest.embeddings.EmbeddingError("embeddings server HTTP 503")
        saved.append(file.name)
        return 1

    monkeypatch.setattr(ingest, "ingest_file", fake_ingest_file)
    monkeypatch.setattr(ingest, "QdrantClient", lambda **kw: object())
    import pytest
    with pytest.raises(SystemExit) as exc:
        ingest.ingest(tmp_path, recreate=False)
    assert exc.value.code == 1
    assert saved == ["a.md"]                       # 실패한 파일 이후는 처리하지 않음
    assert "embedding failed for official/b.md" in caplog.text
    assert "stopped after 1 of 3 file(s)" in caplog.text


def test_ingest_stops_cleanly_on_qdrant_error(monkeypatch, tmp_path, caplog):
    (tmp_path / "official").mkdir()
    (tmp_path / "official" / "a.md").write_text("# a", encoding="utf-8")

    from qdrant_client.http.exceptions import ResponseHandlingException

    def fake_ingest_file(client, file, root, **kw):
        raise ResponseHandlingException(ConnectionRefusedError("[Errno 111] Connection refused"))

    monkeypatch.setattr(ingest, "ingest_file", fake_ingest_file)
    monkeypatch.setattr(ingest, "QdrantClient", lambda **kw: object())
    import pytest
    with pytest.raises(SystemExit):
        ingest.ingest(tmp_path, recreate=True)
    assert "qdrant failed for official/a.md" in caplog.text
    assert "stopped after 0 of 1 file(s); fix Qdrant and run rag-ingest again (with --recreate)" in caplog.text


def test_unreadable_file_is_skipped(monkeypatch, tmp_path, caplog):
    doc = tmp_path / "official" / "bad.md"
    doc.parent.mkdir()
    doc.write_bytes(b"\xff\xfe# bad")                 # UTF-8 이 아님
    assert ingest.ingest_file(object(), doc, tmp_path, ensure=False) == 0
    assert "skipping unreadable file" in caplog.text


def test_other_errors_are_not_labelled_qdrant(monkeypatch, tmp_path, caplog):
    (tmp_path / "official").mkdir()
    (tmp_path / "official" / "a.md").write_text("# a", encoding="utf-8")

    def boom(*a, **kw):
        raise ValueError("unexpected")

    monkeypatch.setattr(ingest, "ingest_file", boom)
    monkeypatch.setattr(ingest, "QdrantClient", lambda **kw: object())
    import pytest
    with pytest.raises(SystemExit):
        ingest.ingest(tmp_path, recreate=False)
    assert "ingest failed for official/a.md: unexpected" in caplog.text
    assert "fix the problem and run rag-ingest again" in caplog.text
