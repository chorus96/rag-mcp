"""tools/documents.py, tools/server.py 테스트 — MCP 쓰기 도구 (문서 추가·삭제)와 초안 승격.

문서 디렉터리 (테스트에서는 pytest 임시 폴더가 KNOWLEDGE_DIR)
    <tmp>/
    ├── official/     정식 문서 — 쓰기 도구가 건드리면 안 되는 곳. 승격 테스트의 목적지
    └── draft/        초안 — 쓰기 도구가 다루는 유일한 곳
  source 는 모두 이 폴더 기준 상대 경로(draft/a.md, official/a.md)로 확인합니다.

확인하는 것
  - 파일 저장: draft/<제목>.md 에 front matter + 본문으로 저장. 문서 유형은 폴더가 아니라 front matter에만 씀
  - 저장한 파일을 ingest_file 로 색인하고, source 가 rag-ingest 와 같은 형식(상대 경로)임
  - 입력 검증: 빈 제목·본문, 너무 긴 본문, 잘못된 doc_type 은 저장하지 않음
  - 덮어쓰기: 같은 경로에 문서가 있으면 overwrite=True 일 때만 바꿈
  - 경로 안전: 제목에 경로 문자가 있어도 문서 디렉터리 밖에 쓰지 않음
  - 색인 실패: 파일은 남기고 오류를 돌려줌
  - 삭제: 파일과 청크를 함께 지우고, 파일 없이 남은 청크도 정리. draft/ 밖·문서가 아닌 파일은 거부
  - draft/ 제한: MCP로는 draft/ 밖(official/ 의 정식 문서 등)을 만들거나 지울 수 없음
  - 초안 승격(rag-promote): draft/<경로> → official/<경로> 로 옮겨 색인하고 초안을 정리, official/ 의 기존 문서는
    --overwrite 로만 바꿈, 색인 실패 시 official/ 을 되돌림, 목록에는 draft/ 문서만 나옴, MCP 도구로는 노출되지 않음
  - 도구 등록: RAG_MCP_WRITE 가 꺼져 있으면 쓰기 도구(추가·삭제)가 MCP 도구 목록에 없음
  - 문서 목록(rag_list_documents): official/ 또는 draft/ 의 .md/.pdf 만 source 순으로, 제목·유형·색인된 청크 수와 함께
    돌려줌. subdir·limit 처리, official/ 밖 거부, Qdrant 실패 시에도 목록은 돌려줌(chunks=None), 항상 등록됨.
    초안에는 promote_to, draft/ 가 없으면 빈 목록, official·draft 외 folder 는 거부

방법
  - KNOWLEDGE_DIR 을 pytest 임시 폴더로 바꾸고, ingest.ingest_file 은 가짜 함수로 바꿔
    Qdrant나 임베딩 없이 실행합니다.
"""

import asyncio
import sys
from pathlib import Path

import yaml

# 소스가 tools/ 에 있으므로 import 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import documents  # noqa: E402


# --- 테스트 도우미 ------------------------------------------------------------
class _IngestRecorder:
    """ingest.ingest_file 을 대신합니다: 호출을 기록하고 정해진 청크 수를 돌려줍니다."""

    def __init__(self, chunks=3, raises=None):
        self.chunks = chunks
        self.raises = raises
        self.calls = []

    def __call__(self, client, file, root, **kw):
        self.calls.append((file, root))
        if self.raises:
            raise self.raises
        return self.chunks


def _setup(monkeypatch, tmp_path, **kw):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    rec = _IngestRecorder(**kw)
    monkeypatch.setattr(documents.ingest, "ingest_file", rec)
    return rec


def _front_matter(path):
    text = path.read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    return yaml.safe_load(front), body.strip()


# --- 파일 저장과 색인 ------------------------------------------------------------
def test_add_document_writes_file_and_ingests(monkeypatch, tmp_path):
    rec = _setup(monkeypatch, tmp_path)
    out = documents.add_document(
        "Longhorn 볼륨 attaching 멈춤", "# 증상\n파드가 멈춤", "rca",
        tags=["longhorn"], component="longhorn", cluster="prod-01", client=object(),
    )
    assert out["status"] == "ok"
    assert out["source"] == "draft/longhorn-볼륨-attaching-멈춤.md"
    assert out["chunks"] == 3 and out["replaced"] is False

    path = tmp_path / out["source"]
    meta, body = _front_matter(path)
    assert meta == {"title": "Longhorn 볼륨 attaching 멈춤", "type": "rca",
                    "tags": ["longhorn"], "component": "longhorn", "cluster": "prod-01"}
    assert body == "# 증상\n파드가 멈춤"
    # rag-ingest 와 같은 기준(문서 디렉터리)으로 색인해야 포인트 ID가 같아집니다.
    assert rec.calls == [(path, tmp_path)]


def test_default_doc_type_is_note(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = documents.add_document("메모", "내용", client=object())
    assert out["source"] == "draft/메모.md"


def test_doc_type_does_not_change_folder(monkeypatch, tmp_path):
    # 문서 유형은 front matter에만 쓰고, 파일은 유형과 관계없이 draft/ 바로 아래에 둡니다.
    _setup(monkeypatch, tmp_path)
    out = documents.add_document("a", "b", "runbook", client=object())
    assert out["source"] == "draft/a.md"
    assert _front_matter(tmp_path / out["source"])[0]["type"] == "runbook"


# --- 입력 검증 ------------------------------------------------------------------
def test_rejects_empty_title_and_content(monkeypatch, tmp_path):
    rec = _setup(monkeypatch, tmp_path)
    assert documents.add_document(" ", "x")["status"] == "error"
    assert documents.add_document("t", "  ")["status"] == "error"
    assert rec.calls == [] and list(tmp_path.iterdir()) == []


def test_rejects_too_long_content(monkeypatch, tmp_path):
    rec = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(documents, "MAX_DOC_CHARS", 10)
    out = documents.add_document("t", "x" * 11)
    assert out["status"] == "error" and "too long" in out["error"]
    assert rec.calls == []


def test_rejects_unsafe_doc_type(monkeypatch, tmp_path):
    rec = _setup(monkeypatch, tmp_path)
    for bad in ("../etc", "Runbook!", "a/b", ""):
        # 빈 값은 기본값 note 로 바뀌므로 통과해야 하고, 나머지는 거부해야 합니다.
        out = documents.add_document("t", "x", bad, client=object())
        assert (out["status"] == "ok") == (bad == "")
    assert all(str(f).startswith(str(tmp_path)) for f, _ in rec.calls)


# --- 덮어쓰기와 경로 안전 ----------------------------------------------------------
def test_existing_document_requires_overwrite(monkeypatch, tmp_path):
    rec = _setup(monkeypatch, tmp_path)
    first = documents.add_document("same", "v1", client=object())
    second = documents.add_document("same", "v2", client=object())
    assert second["status"] == "error" and "overwrite" in second["error"]
    assert _front_matter(tmp_path / first["source"])[1] == "v1"

    third = documents.add_document("same", "v2", overwrite=True, client=object())
    assert third["status"] == "ok" and third["replaced"] is True
    assert _front_matter(tmp_path / first["source"])[1] == "v2"
    assert len(rec.calls) == 2


def test_title_with_path_characters_stays_inside_knowledge_dir(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = documents.add_document("../../etc/passwd", "x", client=object())
    assert out["status"] == "ok"
    written = (tmp_path / out["source"]).resolve()
    assert tmp_path.resolve() in written.parents
    assert "/" not in Path(out["source"]).name


# --- 색인 실패 ------------------------------------------------------------------
def test_indexing_failure_keeps_file(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, raises=RuntimeError("qdrant down"))
    out = documents.add_document("t", "x", client=object())
    assert out["status"] == "error" and out["saved"] is True
    assert "rag-ingest" in out["error"]
    assert (tmp_path / out["source"]).exists()


# --- 도구 등록 ------------------------------------------------------------------
def _tool_names():
    import server
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_write_tool_not_registered_by_default():
    # 기본 환경에서는 RAG_MCP_WRITE 가 설정되지 않음 -> 꺼짐.
    assert documents.WRITE_ENABLED is False
    names = _tool_names()
    assert "rag_add_document" not in names and "rag_delete_document" not in names


def test_write_tool_registered_when_enabled():
    # server 모듈은 import 시점에 도구를 등록하므로, 새 프로세스에서 환경 변수를 켜고 확인합니다.
    import os
    import subprocess
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    code = ("import asyncio, server; "
            "print(sorted(t.name for t in asyncio.run(server.mcp.list_tools())))")
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=tools_dir, capture_output=True, text=True, check=True,
        env={**os.environ, "RAG_MCP_WRITE": "true", "RAG_HYBRID": "false"},
    ).stdout
    assert "rag_add_document" in out and "rag_delete_document" in out


# --- 문서 삭제 ------------------------------------------------------------------
class _FakeQdrant:
    """count/delete 호출을 기록합니다. `points`는 count()가 돌려줄 청크 수입니다."""

    def __init__(self, points=0, delete_raises=None):
        self.points = points
        self.delete_raises = delete_raises
        self.deleted = []

    def count(self, collection, count_filter=None, exact=True):
        self.count_filter = count_filter
        return type("C", (), {"count": self.points})()

    def delete(self, collection_name, points_selector):
        if self.delete_raises:
            raise self.delete_raises
        self.deleted.append(points_selector)


def _make_doc(tmp_path, rel="draft/a.md"):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: a\n---\nbody\n", encoding="utf-8")
    return path


def test_delete_removes_file_and_chunks(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    path = _make_doc(tmp_path)
    q = _FakeQdrant(points=4)
    out = documents.delete_document("draft/a.md", client=q)
    assert out == {"status": "ok", "source": "draft/a.md", "file_deleted": True,
                   "chunks_deleted": 4}
    assert not path.exists()
    cond = q.deleted[0].filter.must[0]
    assert cond.key == "source" and cond.match.value == "draft/a.md"


def test_delete_cleans_orphan_chunks_without_file(monkeypatch, tmp_path):
    # 파일을 지우거나 이름을 바꾼 뒤 남은 청크도 정리할 수 있어야 합니다.
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    q = _FakeQdrant(points=2)
    out = documents.delete_document("draft/gone.md", client=q)
    assert out["status"] == "ok" and out["file_deleted"] is False and out["chunks_deleted"] == 2
    assert len(q.deleted) == 1


def test_delete_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    q = _FakeQdrant(points=0)
    out = documents.delete_document("draft/none.md", client=q)
    assert out["status"] == "error" and "no document" in out["error"]
    assert q.deleted == []


def test_delete_refuses_paths_outside_knowledge_dir(monkeypatch, tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", kb)
    outside = tmp_path / "secret.md"
    outside.write_text("x", encoding="utf-8")
    q = _FakeQdrant(points=1)
    for bad in ("../secret.md", "official/../../secret.md", str(outside)):
        out = documents.delete_document(bad, client=q)
        assert out["status"] == "error", bad
    assert outside.exists() and q.deleted == []


def test_delete_refuses_non_document_files(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    other = tmp_path / "draft" / "notes.txt"
    other.parent.mkdir(parents=True)
    other.write_text("x", encoding="utf-8")
    out = documents.delete_document("draft/notes.txt", client=_FakeQdrant(points=1))
    assert out["status"] == "error" and other.exists()


def test_delete_keeps_file_when_chunk_delete_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    path = _make_doc(tmp_path)
    out = documents.delete_document("draft/a.md",
                                    client=_FakeQdrant(points=3, delete_raises=RuntimeError("down")))
    assert out["status"] == "error" and path.exists()


def test_add_then_delete_roundtrip(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    added = documents.add_document("복구 절차", "내용", "runbook", client=object())
    out = documents.delete_document(added["source"], client=_FakeQdrant(points=3))
    assert out["status"] == "ok" and not (tmp_path / added["source"]).exists()


# --- draft/ 제한 ------------------------------------------------------------------
def test_add_never_writes_outside_draft(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    for title, doc_type in (("../../official/x", "runbook"), ("x", "draft"), ("y", "rca")):
        out = documents.add_document(title, "body", doc_type, client=object())
        assert out["source"].startswith("draft/"), out
        assert (tmp_path / "draft").resolve() in (tmp_path / out["source"]).resolve().parents


def test_delete_refuses_documents_outside_draft(monkeypatch, tmp_path):
    # 사람이 관리하는 정식 문서는 파일이 있어도, 청크가 있어도 MCP로 지울 수 없어야 합니다.
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    official = _make_doc(tmp_path, "official/official.md")
    q = _FakeQdrant(points=5)
    for bad in ("official/official.md", "draft/../official/official.md", "draft", "draft/"):
        out = documents.delete_document(bad, client=q)
        assert out["status"] == "error", bad
    assert official.exists() and q.deleted == []



# --- 초안 승격 (rag-promote) -------------------------------------------------------
class _PromoteQdrant(_FakeQdrant):
    """승격 테스트용: count는 정해진 수, delete는 기록."""


def _draft(tmp_path, rel="draft/a.md", title="초안 A"):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\ntype: runbook\n---\nbody\n", encoding="utf-8")
    return path


def test_list_drafts(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    _draft(tmp_path)
    _draft(tmp_path, "official/official.md", "정식")  # draft/ 밖은 목록에 없어야 함
    assert documents.list_drafts() == [
        {"source": "draft/a.md", "title": "초안 A", "promote_to": "official/a.md"}
    ]


def test_promote_moves_ingests_and_cleans_draft(monkeypatch, tmp_path):
    rec = _setup(monkeypatch, tmp_path, chunks=4)
    draft = _draft(tmp_path)
    q = _PromoteQdrant(points=2)
    out = documents.promote_document("draft/a.md", client=q)
    assert out == {"status": "ok", "source": "draft/a.md", "target": "official/a.md",
                   "chunks": 4, "draft_chunks_deleted": 2, "replaced": False}
    official = tmp_path / "official/a.md"
    assert official.exists() and not draft.exists()
    # 정식 위치를 rag-ingest 와 같은 기준(문서 디렉터리)으로 색인하고, 초안 청크를 지웁니다.
    assert rec.calls == [(official, tmp_path)]
    assert q.deleted[0].filter.must[0].match.value == "draft/a.md"


def test_promote_refuses_existing_official_without_overwrite(monkeypatch, tmp_path):
    rec = _setup(monkeypatch, tmp_path)
    draft = _draft(tmp_path)
    official = _draft(tmp_path, "official/a.md", "기존 정식")
    out = documents.promote_document("draft/a.md", client=_PromoteQdrant())
    assert out["status"] == "error" and "--overwrite" in out["error"]
    assert draft.exists() and "기존 정식" in official.read_text(encoding="utf-8") and rec.calls == []

    out = documents.promote_document("draft/a.md", overwrite=True, client=_PromoteQdrant())
    assert out["status"] == "ok" and out["replaced"] is True
    assert "초안 A" in official.read_text(encoding="utf-8") and not draft.exists()


def test_promote_rolls_back_when_indexing_fails(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, raises=RuntimeError("embeddings down"))
    draft = _draft(tmp_path)
    official = _draft(tmp_path, "official/a.md", "기존 정식")
    out = documents.promote_document("draft/a.md", overwrite=True, client=_PromoteQdrant())
    assert out["status"] == "error" and "nothing was changed" in out["error"]
    # 초안은 그대로, 정식 문서는 원래 내용으로 되돌아가야 합니다.
    assert draft.exists() and "기존 정식" in official.read_text(encoding="utf-8")


def test_promote_rejects_non_draft_sources(monkeypatch, tmp_path):
    rec = _setup(monkeypatch, tmp_path)
    _draft(tmp_path, "official/official.md")
    for bad in ("official/official.md", "draft/../official/official.md", "draft/missing.md", ""):
        out = documents.promote_document(bad, client=_PromoteQdrant())
        assert out["status"] == "error", bad
    assert rec.calls == []


def test_promote_is_not_an_mcp_tool():
    # 승격은 사람이 쓰는 명령 전용입니다. 쓰기 도구를 켜도 MCP 도구로는 노출되지 않아야 합니다.
    import os
    import subprocess
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    code = ("import asyncio, server; "
            "print(sorted(t.name for t in asyncio.run(server.mcp.list_tools())))")
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=tools_dir, capture_output=True, text=True, check=True,
        env={**os.environ, "RAG_MCP_WRITE": "true", "RAG_HYBRID": "false"},
    ).stdout
    assert "promote" not in out


# --- 정식 문서 목록 (rag_list_documents) -------------------------------------------
class _FacetQdrant:
    """facet 호출을 기록하고 source별 청크 수를 돌려줍니다. raises가 있으면 실패합니다."""

    def __init__(self, counts=None, raises=None):
        self.counts = counts or {}
        self.raises = raises
        self.calls = []

    def facet(self, collection, key, limit, exact, facet_filter):
        self.calls.append((key, limit, facet_filter))
        if self.raises:
            raise self.raises
        hits = [type("H", (), {"value": v, "count": n})() for v, n in self.counts.items()]
        return type("R", (), {"hits": hits})()


def _official(tmp_path):
    docs = {
        "official/a.md": "---\ntitle: 문서 A\ntype: runbook\n---\nbody\n",
        "official/rcas/b.md": "---\ntitle: 문서 B\n---\nbody\n",
        "official/c.pdf": "%PDF-1.4",
        "official/notes.txt": "x",
        "draft/d.md": "---\ntitle: 초안 D\n---\nbody\n",
    }
    for rel, text in docs.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")


def test_list_documents_lists_documents_with_index_state(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    _official(tmp_path)
    q = _FacetQdrant(counts={"official/a.md": 4})
    out = documents.list_documents(client=q)
    assert out["status"] == "ok" and out["folder"] == "official/"
    assert out["total"] == 3 and out["truncated"] is False
    # .md/.pdf 만, source 순. draft/ 와 다른 확장자는 없음.
    rows = [(d["source"], d["title"], d["doc_type"], d["chunks"]) for d in out["documents"]]
    assert rows == [
        ("official/a.md", "문서 A", "runbook", 4),
        ("official/c.pdf", "c", "note", 0),        # 색인 전 → 0
        ("official/rcas/b.md", "문서 B", "rca", 0),  # type 없음 → 하위 폴더 이름
    ]
    assert all(d["size_bytes"] > 0 and d["modified"].endswith("+00:00") for d in out["documents"])
    # 청크 수는 목록에 나온 source 만 대상으로 한 번에 셉니다.
    key, limit, flt = q.calls[0]
    assert key == "source" and limit == 3 and len(q.calls) == 1
    assert sorted(flt.must[0].match.any) == ["official/a.md", "official/c.pdf", "official/rcas/b.md"]


def test_list_documents_subdir_and_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    _official(tmp_path)
    out = documents.list_documents(subdir="rcas", client=_FacetQdrant())
    assert out["folder"] == "official/rcas/" and [d["source"] for d in out["documents"]] == ["official/rcas/b.md"]

    out = documents.list_documents(limit=1, client=_FacetQdrant())
    assert out["total"] == 3 and out["returned"] == 1 and out["truncated"] is True


def test_list_documents_rejects_paths_outside_official(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    _official(tmp_path)
    for bad in ("../draft", "rcas/../../draft", "missing"):
        out = documents.list_documents(subdir=bad, client=_FacetQdrant())
        assert out["status"] == "error", bad


def test_list_documents_without_qdrant_still_lists(monkeypatch, tmp_path):
    # Qdrant에 묻지 못해도 목록은 돌려주고, 색인 상태는 알 수 없음(None)으로 표시합니다.
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    _official(tmp_path)
    out = documents.list_documents(client=_FacetQdrant(raises=RuntimeError("qdrant down")))
    assert out["status"] == "ok" and out["total"] == 3
    assert all(d["chunks"] is None for d in out["documents"])


def test_list_documents_tool_registered_by_default():
    # 읽기 전용 도구이므로 쓰기 도구를 켜지 않아도 등록됩니다.
    assert "rag_list_documents" in _tool_names()


def test_list_documents_draft_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    _official(tmp_path)
    out = documents.list_documents("draft", client=_FacetQdrant(counts={"draft/d.md": 2}))
    assert out["folder"] == "draft/" and out["total"] == 1
    assert out["documents"][0] | {"size_bytes": 0, "modified": ""} == {
        "source": "draft/d.md", "title": "초안 D", "doc_type": "note", "size_bytes": 0, "modified": "",
        "chunks": 2, "promote_to": "official/d.md",
    }
    # 정식 문서 목록에는 promote_to 가 없습니다.
    assert all("promote_to" not in d for d in documents.list_documents(client=_FacetQdrant())["documents"])


def test_list_documents_empty_when_no_draft_folder(monkeypatch, tmp_path):
    # draft/ 는 첫 초안을 저장할 때 만들어지므로, 없으면 오류가 아니라 빈 목록입니다.
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    out = documents.list_documents("draft", client=_FacetQdrant())
    assert out == {"status": "ok", "folder": "draft/", "total": 0, "returned": 0, "truncated": False,
                   "documents": []}
    assert documents.list_documents("official", client=_FacetQdrant())["status"] == "error"


def test_list_documents_rejects_unknown_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "KNOWLEDGE_DIR", tmp_path)
    _official(tmp_path)
    for bad in ("runbooks", "..", "/etc"):
        assert documents.list_documents(bad, client=_FacetQdrant())["status"] == "error", bad
