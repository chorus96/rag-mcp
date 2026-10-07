"""tools/documents.py — 초안(draft/) 문서의 추가·삭제·승격 로직.

역할
  LLM이 MCP 도구로 지식 베이스의 문서를 추가하거나 삭제할 때의 로직입니다.
  - add_document (rag_add_document):    문서를 문서 디렉터리의 draft/ 아래 마크다운 파일로 저장한 뒤 rag-ingest와
                                         같은 방식(ingest.ingest_file)으로 바로 색인
  - delete_document (rag_delete_document): draft/ 아래 문서 파일과 그 문서의 청크(포인트)를 함께 삭제
  - list_documents (rag_list_documents): 정식 문서(official/) 또는 초안(draft/) 파일 목록. 읽기 전용이라
                                           항상 등록됩니다.
  - list_drafts / promote_document:         초안 목록과, 검토한 초안을 정식 폴더(official/)로 옮기는 승격.
                                             사람이 쓰는 rag-promote 명령(promote.py)만 호출하며 MCP 도구가
                                             아닙니다 (모델은 정식 폴더에 쓸 수 없음)

문서 디렉터리 (RAG_KNOWLEDGE_DIR → KNOWLEDGE_DIR)
    KNOWLEDGE_DIR/
    ├── official/     OFFICIAL_SUBDIR — 사람이 관리하는 정식 문서. 이 모듈은 승격할 때만 씁니다.
    └── draft/        DRAFT_SUBDIR    — 모델이 만든 초안. MCP 쓰기 도구는 여기만 다룹니다.
  - 추가: draft/<제목>.md 로 저장합니다. 문서 유형은 폴더가 아니라 front matter의 type 에 씁니다.
  - 삭제: source 를 정규화해 draft/ 안일 때만 지웁니다 (_resolve_source).
  - 승격: draft/<경로> → official/<경로>. 하위 경로는 그대로 유지합니다 (draft/a/b.md → official/a/b.md).
  - source 는 KNOWLEDGE_DIR 기준 상대 경로이고, 색인도 rag-ingest와 같이 KNOWLEDGE_DIR 을 root 로 합니다.
    그래서 초안은 저장 즉시 검색되고, 나중에 rag-ingest 가 같은 파일을 다시 색인해도 ID가 같습니다.

왜 파일로도 저장하나
  지식 베이스의 원본은 문서 디렉터리입니다. 파일로 남겨 두면 rag-ingest --recreate 로 재구축해도
  추가한 문서가 사라지지 않고, 사람이 직접 고치거나 지울 수도 있습니다. 같은 파일을 rag-ingest가 다시
  수집해도 포인트 ID가 같아 중복이 생기지 않습니다.

안전장치
  - 기본으로 꺼져 있습니다. 설정 파일에서 RAG_MCP_WRITE=true 일 때만 server.py가 도구를 등록합니다.
  - 파일 경로는 서버가 정합니다(draft/ + 제목에서 만든 파일 이름). 호출자가 경로를 지정할 수
    없으므로 문서 디렉터리 밖에 쓸 수 없습니다.
  - 같은 이름의 파일이 있으면 overwrite=True 일 때만 덮어씁니다.
  - 삭제는 draft/ 안의 .md / .pdf 문서만 대상으로 합니다. 경로를 정규화해 draft/ 밖을
    가리키면 거부합니다.
  - 본문 크기는 RAG_MAX_DOC_CHARS(기본 200000자)로 제한합니다.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchAny, MatchValue

import ingest

log = logging.getLogger("rag-documents")

# --- 설정 ---------------------------------------------------------------------
WRITE_ENABLED = os.environ.get("RAG_MCP_WRITE", "false").strip().lower() in ("1", "true", "yes", "on")
# 문서 디렉터리. 비어 있으면 기본 경로, ~ 는 홈으로 풂 (install.sh·rag-ingest·rag-promote와 같은 규칙).
KNOWLEDGE_DIR = Path(os.path.expanduser(
    os.environ.get("RAG_KNOWLEDGE_DIR") or "~/.local/share/rag-mcp/data/knowledge"
))
MAX_DOC_CHARS = int(os.environ.get("RAG_MAX_DOC_CHARS", "200000"))

# 문서 디렉터리의 두 하위 디렉터리: MCP 쓰기 도구가 다룰 수 있는 초안, 사람이 관리하는 정식 문서.
DRAFT_SUBDIR = "draft"
OFFICIAL_SUBDIR = "official"

# 문서 유형은 front matter와 검색 필터 값이 되므로 안전한 문자만 허용합니다.
_DOC_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


# --- 내부 도우미 --------------------------------------------------------------
def _slug(title: str) -> str:
    """제목으로 파일 이름을 만듭니다. 한글 등 글자는 그대로 두고, 나머지 기호는 '-'로 바꿉니다."""
    s = unicodedata.normalize("NFC", title).strip().lower()
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE).strip("-_")
    return s[:80].rstrip("-_") or f"doc-{uuid.uuid4().hex[:8]}"


def _draft_dir() -> Path:
    """MCP 쓰기 도구가 다룰 수 있는 디렉터리 (문서 디렉터리/draft)."""
    return KNOWLEDGE_DIR / DRAFT_SUBDIR


def _render(title: str, content: str, doc_type: str, tags: list[str],
            component: str | None, cluster: str | None) -> str:
    """front matter + 본문으로 마크다운 파일 내용을 만듭니다."""
    meta: dict[str, Any] = {"title": title, "type": doc_type}
    if tags:
        meta["tags"] = tags
    if component:
        meta["component"] = component
    if cluster:
        meta["cluster"] = cluster
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{content.strip()}\n"


# --- 문서 추가 -----------------------------------------------------------------
def add_document(title: str, content: str, doc_type: str = "note", tags: list[str] | None = None,
                 component: str | None = None, cluster: str | None = None,
                 overwrite: bool = False, client: QdrantClient | None = None) -> dict[str, Any]:
    """문서를 문서 디렉터리에 저장하고 색인합니다. 결과를 dict로 돌려줍니다 (오류도 status로)."""
    title = (title or "").strip()
    content = content or ""
    doc_type = (doc_type or "note").strip().lower()
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]

    if not title:
        return {"status": "error", "error": "title must be a non-empty string"}
    if not content.strip():
        return {"status": "error", "error": "content must be a non-empty string"}
    if len(content) > MAX_DOC_CHARS:
        return {"status": "error",
                "error": f"content is too long ({len(content)} chars; limit {MAX_DOC_CHARS})"}
    if not _DOC_TYPE_RE.match(doc_type):
        return {"status": "error",
                "error": "doc_type must be lowercase letters, digits, '-' or '_' (e.g. runbook, rca)"}

    # 저장은 draft/ 아래로만 합니다. source와 색인 기준은 rag-ingest와 같게 문서 디렉터리로 둡니다.
    path = _draft_dir() / f"{_slug(title)}.md"
    source = str(path.relative_to(KNOWLEDGE_DIR)).replace(os.sep, "/")
    existed = path.exists()
    if existed and not overwrite:
        return {"status": "error", "source": source,
                "error": f"a document already exists at {source}; pass overwrite=true to replace it"}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(title, content, doc_type, tags, component, cluster), encoding="utf-8")

    try:
        if client is None:
            client = QdrantClient(url=ingest.QDRANT_URL, api_key=ingest.QDRANT_API_KEY,
                                  timeout=ingest.HTTP_TIMEOUT)
        chunks = ingest.ingest_file(client, path, KNOWLEDGE_DIR)
    except Exception as exc:  # noqa: BLE001 - 파일은 남겨 두고 rag-ingest로 다시 색인할 수 있게 함
        log.exception("indexing %s failed", source)
        return {"status": "error", "source": source, "saved": True,
                "error": f"saved the file but indexing failed: {exc} (run rag-ingest to retry)"}

    log.info("added document %s (%s, %d chunk(s), overwrite=%s)", source, doc_type, chunks, existed)
    return {"status": "ok", "source": source, "doc_type": doc_type, "title": title,
            "chunks": chunks, "replaced": existed}


# --- Qdrant 도우미 ---------------------------------------------------------------
def _client() -> QdrantClient:
    return QdrantClient(url=ingest.QDRANT_URL, api_key=ingest.QDRANT_API_KEY, timeout=ingest.HTTP_TIMEOUT)


def _by_source(source: str) -> Filter:
    return Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))])


def _count_chunks(client: QdrantClient, source: str) -> int:
    """source의 청크 수. 컬렉션이 없는 등으로 셀 수 없으면 0."""
    try:
        return client.count(ingest.COLLECTION, count_filter=_by_source(source), exact=True).count
    except Exception as exc:  # noqa: BLE001 - 컬렉션이 없을 수도 있음
        log.warning("counting chunks for %s failed: %s", source, exc)
        return 0


def _delete_chunks(client: QdrantClient, source: str) -> None:
    client.delete(collection_name=ingest.COLLECTION, points_selector=FilterSelector(filter=_by_source(source)))


# --- 문서 삭제 -----------------------------------------------------------------
def _resolve_source(source: str) -> tuple[Path, str] | None:
    """source(문서 디렉터리 기준 상대 경로)를 실제 경로로 바꿉니다.

    절대 경로이거나, 정규화했을 때 draft/ 밖을 가리키면 None입니다.
    """
    if Path(source).is_absolute():
        return None
    root = KNOWLEDGE_DIR.resolve()
    path = (root / source).resolve()
    if _draft_dir().resolve() not in path.parents:
        return None
    return path, str(path.relative_to(root)).replace(os.sep, "/")


def delete_document(source: str, client: QdrantClient | None = None) -> dict[str, Any]:
    """문서 파일과 그 청크를 삭제합니다. 결과를 dict로 돌려줍니다 (오류도 status로).

    파일이 이미 없어도 청크가 남아 있으면 청크만 지웁니다 (파일을 지우거나 이름을 바꾼 뒤 남은 청크 정리).
    """
    source = (source or "").strip()
    if not source:
        return {"status": "error",
                "error": f"source must be a non-empty path (e.g. {DRAFT_SUBDIR}/foo.md)"}
    resolved = _resolve_source(source)
    if resolved is None:
        return {"status": "error",
                "error": f"only documents under {DRAFT_SUBDIR}/ can be deleted via MCP: {source}"}
    path, source = resolved
    if path.suffix.lower() not in (".md", ".pdf"):
        return {"status": "error", "error": "only .md or .pdf documents can be deleted"}

    client = client or _client()
    points = _count_chunks(client, source)

    file_exists = path.is_file()
    if not file_exists and not points:
        return {"status": "error", "source": source, "error": f"no document found at {source}"}

    try:
        if points:
            _delete_chunks(client, source)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "source": source,
                "error": f"deleting chunks failed: {exc} (the file was not deleted)"}
    if file_exists:
        path.unlink()

    log.info("deleted document %s (file=%s, %d chunk(s))", source, file_exists, points)
    return {"status": "ok", "source": source, "file_deleted": file_exists, "chunks_deleted": points}


# --- 문서 목록 (MCP 읽기 도구 rag_list_documents) -----------------------------------------
MAX_LIST = 500


def _indexed_chunks(client: QdrantClient, sources: list[str]) -> dict[str, int] | None:
    """source별 색인된 청크 수 (Qdrant facet, 요청 한 번). 셀 수 없으면 None."""
    if not sources:
        return {}
    try:
        res = client.facet(ingest.COLLECTION, key="source", limit=len(sources), exact=True,
                           facet_filter=Filter(must=[FieldCondition(key="source", match=MatchAny(any=sources))]))
        return {str(hit.value): hit.count for hit in res.hits}
    except Exception as exc:  # noqa: BLE001 - 목록은 색인 상태 없이도 돌려줌
        log.warning("counting indexed chunks failed: %s", exc)
        return None


def list_documents(folder: str = OFFICIAL_SUBDIR, subdir: str | None = None, limit: int = 100,
                   client: QdrantClient | None = None) -> dict[str, Any]:
    """official/ 또는 draft/ 아래 문서(.md/.pdf) 목록을 source 순으로 돌려줍니다 (오류도 status로).

    subdir를 주면 <folder>/<subdir>/ 아래만 봅니다. 항목마다 source, title, doc_type, 크기, 수정 시각과
    색인된 청크 수(chunks; 0이면 아직 색인 전)를 넣습니다. 색인 상태를 알 수 없으면 chunks 는 None입니다.
    초안(draft/)에는 승격하면 옮겨질 위치(promote_to)도 넣습니다. draft/ 는 첫 초안을 저장할 때 만들어지므로,
    아직 없으면 빈 목록입니다.
    """
    folder = (folder or OFFICIAL_SUBDIR).strip().strip("/").lower()
    if folder not in (OFFICIAL_SUBDIR, DRAFT_SUBDIR):
        return {"status": "error",
                "error": f"folder must be '{OFFICIAL_SUBDIR}' or '{DRAFT_SUBDIR}': {folder}"}
    root = (KNOWLEDGE_DIR / folder).resolve()
    base = root
    subdir = (subdir or "").strip().strip("/")
    if subdir:
        base = (root / subdir).resolve()
        if Path(subdir).is_absolute() or (base != root and root not in base.parents):
            return {"status": "error", "error": f"subdir must be a folder under {folder}/: {subdir}"}
    label = f"{folder}/{subdir}/" if subdir else f"{folder}/"
    if not base.is_dir():
        if folder == DRAFT_SUBDIR and not subdir:
            return {"status": "ok", "folder": label, "total": 0, "returned": 0, "truncated": False,
                    "documents": []}
        return {"status": "error", "error": f"folder not found: {label}"}
    limit = max(1, min(int(limit or 100), MAX_LIST))

    paths = sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".pdf"))
    kb = KNOWLEDGE_DIR.resolve()
    docs = []
    for path in paths[:limit]:
        meta: dict[str, Any] = {}
        if path.suffix.lower() == ".md":
            try:
                meta, _ = ingest._parse_front_matter(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - 읽지 못한 파일도 목록에는 넣음
                log.warning("reading %s failed: %s", path, exc)
        stat = path.stat()
        docs.append({
            "source": str(path.relative_to(kb)).replace(os.sep, "/"),
            "title": str(meta.get("title") or path.stem),
            "doc_type": ingest._infer_doc_type(meta, path, kb),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        })

    counts = _indexed_chunks(client or _client(), [d["source"] for d in docs])
    for d in docs:
        d["chunks"] = None if counts is None else counts.get(d["source"], 0)
        if folder == DRAFT_SUBDIR:
            d["promote_to"] = f"{OFFICIAL_SUBDIR}/{d['source'][len(DRAFT_SUBDIR) + 1:]}"

    return {"status": "ok", "folder": label, "total": len(paths), "returned": len(docs),
            "truncated": len(paths) > len(docs), "documents": docs}


# --- 초안 목록과 승격 (사람이 쓰는 rag-promote 명령 전용) --------------------------------
def list_drafts() -> list[dict[str, str]]:
    """draft/ 아래 문서 목록: [{"source", "title", "promote_to"}] (source 순)."""
    drafts = []
    root = _draft_dir()
    if not root.is_dir():
        return drafts
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".pdf")):
        source = str(path.relative_to(KNOWLEDGE_DIR)).replace(os.sep, "/")
        title = path.stem
        if path.suffix.lower() == ".md":
            meta, _ = ingest._parse_front_matter(path.read_text(encoding="utf-8"))
            title = str(meta.get("title") or title)
        drafts.append({"source": source, "title": title,
                       "promote_to": f"{OFFICIAL_SUBDIR}/{source[len(DRAFT_SUBDIR) + 1:]}"})
    return drafts


def promote_document(source: str, overwrite: bool = False,
                     client: QdrantClient | None = None) -> dict[str, Any]:
    """검토한 초안을 정식 폴더로 옮깁니다: draft/<경로> → official/<경로>.

    순서: 정식 위치에 복사 → 색인 → 성공하면 초안의 청크와 파일을 삭제. 색인에 실패하면 정식 위치를
    원래대로 되돌리고 초안은 그대로 둡니다. 정식 위치에 이미 문서가 있으면 overwrite=True 일 때만 바꿉니다.
    """
    source = (source or "").strip()
    resolved = _resolve_source(source) if source else None
    if resolved is None:
        return {"status": "error", "source": source,
                "error": f"source must be a document under {DRAFT_SUBDIR}/ (e.g. {DRAFT_SUBDIR}/foo.md)"}
    path, source = resolved
    if path.suffix.lower() not in (".md", ".pdf") or not path.is_file():
        return {"status": "error", "source": source, "error": f"no draft document found at {source}"}

    target_rel = Path(OFFICIAL_SUBDIR) / path.relative_to(_draft_dir().resolve())
    dest = KNOWLEDGE_DIR / target_rel
    target = str(target_rel).replace(os.sep, "/")
    existed = dest.exists()
    if existed and not overwrite:
        return {"status": "error", "source": source, "target": target,
                "error": f"a document already exists at {target}; use --overwrite to replace it"}

    backup = dest.read_bytes() if existed else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    client = client or _client()
    try:
        chunks = ingest.ingest_file(client, dest, KNOWLEDGE_DIR)
    except Exception as exc:  # noqa: BLE001 - 정식 위치를 원래대로 되돌림
        if backup is None:
            dest.unlink()
        else:
            dest.write_bytes(backup)
        return {"status": "error", "source": source, "target": target,
                "error": f"indexing the promoted document failed: {exc} (nothing was changed)"}

    draft_chunks = _count_chunks(client, source)
    try:
        if draft_chunks:
            _delete_chunks(client, source)
    except Exception as exc:  # noqa: BLE001 - 승격은 끝났고 초안 정리만 실패
        return {"status": "error", "source": source, "target": target, "promoted": True,
                "error": f"promoted, but removing the draft chunks failed: {exc} "
                         f"(the draft file was kept; run rag-promote --overwrite again, or delete the draft via MCP)"}
    path.unlink()

    log.info("promoted %s -> %s (%d chunk(s), replaced=%s)", source, target, chunks, existed)
    return {"status": "ok", "source": source, "target": target, "chunks": chunks,
            "draft_chunks_deleted": draft_chunks, "replaced": existed}

