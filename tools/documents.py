"""tools/documents.py — MCP 쓰기 도구의 로직 (문서 추가).

역할
  LLM이 MCP 도구로 지식 베이스의 문서를 추가하거나 삭제할 때의 로직입니다.
  - add_document (rag_add_document):    문서를 문서 디렉터리의 draft/ 아래 마크다운 파일로 저장한 뒤 rag-ingest와
                                         같은 방식(ingest.ingest_file)으로 바로 색인
  - delete_document (rag_delete_document): draft/ 아래 문서 파일과 그 문서의 청크(포인트)를 함께 삭제

  MCP로는 문서 디렉터리의 draft/ 하위만 추가·삭제할 수 있습니다. 사람이 관리하는 문서(runbooks/,
  incidents/ 등)는 모델이 바꾸거나 지울 수 없고, 모델이 만든 문서는 draft/에 모여 사람이 검토한 뒤
  정식 폴더로 옮길 수 있습니다. draft/ 문서도 저장 즉시 검색됩니다.

왜 파일로도 저장하나
  지식 베이스의 원본은 문서 디렉터리입니다. 파일로 남겨 두면 rag-ingest --recreate 로 재구축해도
  추가한 문서가 사라지지 않고, 사람이 직접 고치거나 지울 수도 있습니다. 같은 파일을 rag-ingest가 다시
  수집해도 포인트 ID가 같아 중복이 생기지 않습니다.

안전장치
  - 기본으로 꺼져 있습니다. 설정 파일에서 RAG_MCP_WRITE=true 일 때만 server.py가 도구를 등록합니다.
  - 파일 경로는 서버가 정합니다(draft/ + 문서 유형 폴더 + 제목에서 만든 파일 이름). 호출자가 경로를 지정할 수
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
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import yaml
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

import ingest

log = logging.getLogger("rag-documents")

# --- 설정 ---------------------------------------------------------------------
WRITE_ENABLED = os.environ.get("RAG_MCP_WRITE", "false").strip().lower() in ("1", "true", "yes", "on")
KNOWLEDGE_DIR = Path(os.path.expanduser(
    os.environ.get("RAG_KNOWLEDGE_DIR") or "~/.local/share/rag-mcp/data/knowledge"
))
MAX_DOC_CHARS = int(os.environ.get("RAG_MAX_DOC_CHARS", "200000"))

# MCP 쓰기 도구가 다룰 수 있는 하위 디렉터리 (문서 디렉터리 기준).
DRAFT_SUBDIR = "draft"

# 문서 유형은 폴더 이름이 되므로 안전한 문자만 허용합니다.
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


def _folder_for(doc_type: str) -> str:
    """문서 유형의 폴더 이름. 기존 관례(incidents/, runbooks/)에 맞춰 복수형으로 둡니다."""
    return doc_type if doc_type.endswith("s") else f"{doc_type}s"


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
                "error": "doc_type must be lowercase letters, digits, '-' or '_' (e.g. incident, runbook)"}

    # 저장은 draft/ 아래로만 합니다. source와 색인 기준은 rag-ingest와 같게 문서 디렉터리로 둡니다.
    path = _draft_dir() / _folder_for(doc_type) / f"{_slug(title)}.md"
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
                "error": f"source must be a non-empty path (e.g. {DRAFT_SUBDIR}/runbooks/foo.md)"}
    resolved = _resolve_source(source)
    if resolved is None:
        return {"status": "error",
                "error": f"only documents under {DRAFT_SUBDIR}/ can be deleted via MCP: {source}"}
    path, source = resolved
    if path.suffix.lower() not in (".md", ".pdf"):
        return {"status": "error", "error": "only .md or .pdf documents can be deleted"}

    by_source = Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))])
    try:
        if client is None:
            client = QdrantClient(url=ingest.QDRANT_URL, api_key=ingest.QDRANT_API_KEY,
                                  timeout=ingest.HTTP_TIMEOUT)
        points = client.count(ingest.COLLECTION, count_filter=by_source, exact=True).count
    except Exception as exc:  # noqa: BLE001 - 컬렉션이 없을 수도 있음
        log.warning("counting chunks for %s failed: %s", source, exc)
        points = 0

    file_exists = path.is_file()
    if not file_exists and not points:
        return {"status": "error", "source": source, "error": f"no document found at {source}"}

    try:
        if points:
            client.delete(collection_name=ingest.COLLECTION,
                          points_selector=FilterSelector(filter=by_source))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "source": source,
                "error": f"deleting chunks failed: {exc} (the file was not deleted)"}
    if file_exists:
        path.unlink()

    log.info("deleted document %s (file=%s, %d chunk(s))", source, file_exists, points)
    return {"status": "ok", "source": source, "file_deleted": file_exists, "chunks_deleted": points}
