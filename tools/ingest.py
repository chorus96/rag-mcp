"""tools/ingest.py — 문서 수집.

역할
  마크다운과 PDF 문서(런북, RCA, 참고 자료 등)를 읽어 청크로 나누고, OpenAI 호환 임베딩
  엔드포인트로 임베딩한 뒤 Qdrant에 업서트합니다. MCP 서버는 읽기 전용이므로, 이 명령이 지식
  베이스를 채우는 정해진 쓰기 경로입니다.

실행
  설치한 서버에서는 rag-ingest 명령(deploy/rag-ingest)으로 실행합니다. 개발용 직접 실행:
      python tools/ingest.py --path knowledge [--recreate]

처리 과정
  1. 파일 탐색 (.md, .pdf)
  2. 파싱: 마크다운은 YAML front matter 분리, PDF는 페이지마다 `# [Page N]` 섹션으로 추출
  3. 청킹: 헤딩 기준 섹션 → 크면 문단 단위로 나눔 (각 청크 앞에 헤딩을 붙임)
  4. 임베딩(EMBED_BATCH_SIZE 단위) + BM25 희소 벡터
  5. 업서트(QDRANT_UPSERT_BATCH 단위) → 줄어든 문서의 남은 청크 삭제

문서 형식 (front matter는 선택)
    ---
    title: Longhorn 볼륨이 attaching 상태에서 멈춤
    type: runbook           # runbook | rca | note | ...  (기본값: 폴더 이름, 없으면 "note")
    tags: [longhorn, storage, node-reboot]
    ---
    # 본문 마크다운...

  `type`을 생략하면 상위 폴더 이름에서 끝의 s를 뗀 값입니다 (runbooks/ → runbook).
  PDF에는 front matter가 없으므로 type은 폴더 이름, 제목은 파일 이름에서 가져옵니다.

멱등성
  청크 ID가 (source, chunk index)에서 정해지므로 다시 실행해도 중복이 생기지 않습니다. 문서를
  삭제하거나 이름을 바꾸면 이전 청크가 남으므로 --recreate 로 재구축하세요.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    Range,
)

import embeddings
import vectorstore

# --- 설정 ---------------------------------------------------------------------
# 사전 필터링을 위해 키워드 인덱스를 만들 페이로드 필드. 이미 있는 인덱스를 다시 만드는 것은
# vectorstore.ensure_collection에서 무시됩니다.
# `source`는 질의 필터가 아니라 오래된 청크 정리 필터를 위해 색인합니다.
_INDEXED_FIELDS = ("doc_type", "component", "cluster", "source")

# 수집기가 이해하는 파일 형식: 마크다운(front matter 인식, 섹션 청킹)과
# PDF(페이지 단위 텍스트 추출). 나머지는 모두 건너뜁니다.
_SUPPORTED_SUFFIXES = (".md", ".pdf")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("rag-ingest")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION = os.environ.get("QDRANT_COLLECTION", "rag_kb")

CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "100"))
HTTP_TIMEOUT = float(os.environ.get("RAG_TIMEOUT_SECONDS", "60"))

# 문서별 두 왕복 요청의 배치 크기. 둘 다 HTTP 요청당 작업량을 제한해, 큰 문서
# 하나(긴 런북, 100페이지 PDF)가 지나치게 크고 전부 아니면 전무인 단일 호출이 되지
# 않게 합니다.
#
# EMBED_BATCH_SIZE: 임베딩 요청당 청크 수. OpenAI 호환 `/v1/embeddings`는 입력 개수
#   (OpenAI 2048개)와 요청당 토큰 수에 상한이 있어, 큰 문서의 청크를 한 번에 보낼 수
#   없습니다.
# UPSERT_BATCH_SIZE: Qdrant 업서트당 포인트 수. 각 포인트는 밀집 벡터, BM25 희소
#   벡터, 청크 텍스트를 담으므로, 한 요청 본문에 청크 수백 개를 넣으면
#   RAG_TIMEOUT_SECONDS 안에 수 메가바이트를 보내야 합니다.
EMBED_BATCH_SIZE = max(1, int(os.environ.get("EMBED_BATCH_SIZE", "32")))
UPSERT_BATCH_SIZE = max(1, int(os.environ.get("QDRANT_UPSERT_BATCH", "64")))

# 같은 파일을 다시 수집하면 포인트를 덮어쓰도록 고정 네임스페이스를 씁니다.
_ID_NAMESPACE = uuid.UUID("6f3a9c1e-9b2d-5a44-8c11-a1b2c3d4e5f6")


# --- 문서 파싱과 청킹 ---------------------------------------------------------
def _parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """선택적 YAML front matter를 본문과 분리합니다. (meta, body)를 반환합니다."""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                return meta, parts[2].strip()
    return {}, raw.strip()


def _chunk(text: str, size: int, overlap: int) -> list[str]:
    """겹침이 있는 문단 인식 문자 단위 청킹. 크기에 맞으면 문단을 통째로 유지하고,
    너무 큰 문단은 강제로 나눕니다."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > size:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), size - overlap):
                chunks.append(para[i : i + size])
            continue
        if len(current) + len(para) + 2 > size:
            chunks.append(current)
            # 맥락이 이어지도록 이전 청크의 끝부분을 가져옴
            current = (current[-overlap:] + "\n\n" + para) if overlap else para
        else:
            current = f"{current}\n\n{para}" if current else para

    if current:
        chunks.append(current)
    return chunks or [text]


def _chunk_document(body: str) -> list[str]:
    """섹션 인식 청킹: 마크다운 헤딩 기준으로 나눠 런북 단계나 RCA 섹션이
    온전히 유지되게 하고, 각 청크 앞에 헤딩을 붙여 단독으로도 맥락을 갖게 한 뒤,
    너무 큰 섹션 안에서는 문단 청커로 대체합니다. 헤딩이 없는 본문은 이전과 똑같이
    동작합니다."""
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    buf: list[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            if heading or buf:
                sections.append((heading, buf))
            heading, buf = line.strip(), []
        else:
            buf.append(line)
    if heading or buf:
        sections.append((heading, buf))

    chunks: list[str] = []
    for head, body_lines in sections:
        text = "\n".join(body_lines).strip()
        if not text and not head:
            continue
        for c in _chunk(text, CHUNK_SIZE, CHUNK_OVERLAP) if text else [""]:
            chunks.append(f"{head}\n{c}".strip() if head else c)
    return [c for c in chunks if c] or _chunk(body, CHUNK_SIZE, CHUNK_OVERLAP)


def _infer_doc_type(meta: dict[str, Any], file: Path, root: Path) -> str:
    if meta.get("type"):
        return str(meta["type"])
    rel = file.relative_to(root)
    if len(rel.parts) > 1:
        folder = rel.parts[0].rstrip("s")  # runbooks -> runbook, rcas -> rca
        return folder
    return "note"


def _extract_pdf_text(path: Path) -> str:
    """PDF 텍스트를 비어 있지 않은 페이지마다 `# [Page N]` 섹션 하나로 추출합니다.

    페이지 표시가 마크다운 헤딩이므로 `_chunk_document`가 페이지 맥락을 유지합니다
    (레이아웃과 읽기 순서는 페이지 경계를 넘으면 거의 유지되지 않지만, 섹션은
    유지됩니다). 추출할 텍스트가 없는 스캔/이미지 전용 PDF는 ""를 반환합니다.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"# [Page {i}]\n{text}")
    return "\n\n".join(pages)


# --- 임베딩과 Qdrant 쓰기 -----------------------------------------------------
def _embed_batch(texts: list[str]) -> list[list[float]]:
    """모든 청크의 밀집 벡터를 요청당 EMBED_BATCH_SIZE개 청크씩 만듭니다.

    `embed()`를 반복 호출하지 않고 `embeddings.embed_documents`를 거치므로, 네이티브
    배치 입력을 지원하는 제공자는 청크마다가 아니라 배치마다 HTTP 왕복 한 번만
    씁니다. 문서는 "document" 작업 유형으로 임베딩되며, 제공자와 모델은
    embeddings.py에 설정된 것을 따릅니다. 수집과 질의는 반드시 그 설정을 공유해야
    하며(embeddings.py 참고), 그렇지 않으면 검색이 깨집니다.

    순서가 유지됩니다: `_embed_openai`가 응답을 `index`로 다시 정렬하고, 배치는
    잘라낸 순서대로 이어 붙이므로 vectors[i]는 texts[i]에 대응합니다.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        vectors.extend(embeddings.embed_documents(texts[start : start + EMBED_BATCH_SIZE]))
    return vectors


def _upsert_points(client: QdrantClient, points: list[PointStruct]) -> None:
    """UPSERT_BATCH_SIZE개 포인트씩 요청으로 업서트하며, 각 요청이 반영될 때까지 기다립니다.

    `wait=True` 덕분에 파일별 "ingested" 로그가 정확해집니다 — 청크가 대기열에만
    들어간 것이 아니라 실제로 검색 가능하다는 뜻입니다. 또한 큰 문서 처리 도중
    실패해도 거부된 요청 하나 때문에 파일 전체를 잃지 않고 앞선 배치는 커밋된 채로
    남습니다.
    """
    for start in range(0, len(points), UPSERT_BATCH_SIZE):
        client.upsert(
            collection_name=COLLECTION,
            points=points[start : start + UPSERT_BATCH_SIZE],
            wait=True,
        )


def _delete_orphan_chunks(client: QdrantClient, source: str, kept: int) -> None:
    """같은 source를 이전에 더 길게 수집했을 때 남은 포인트를 삭제합니다.

    포인트 ID는 uuid5(f"{source}#{index}")이므로, 재수집이 멱등적인 것은 문서의
    청크 수가 줄지 않을 때뿐입니다. 런북을 30개 청크에서 20개로 줄이거나 PDF를 더
    적은 페이지로 다시 내보내면, 20..29번 청크가 컬렉션에 영원히 남아 오래된 텍스트로
    계속 검색에 걸립니다. 현재 청크가 이미 들어간 뒤가 되도록 업서트 다음에
    실행합니다. 고아 청크는 오래된 검색 결과일 뿐 수집 전체를 중단할 이유는 아니므로
    최선형으로 동작합니다.
    """
    stale = Filter(
        must=[
            FieldCondition(key="source", match=MatchValue(value=source)),
            FieldCondition(key="chunk", range=Range(gte=kept)),
        ]
    )
    try:
        orphans = client.count(COLLECTION, count_filter=stale, exact=True).count
        if not orphans:
            return
        client.delete(collection_name=COLLECTION, points_selector=FilterSelector(filter=stale))
        log.info("removed %d stale chunk(s) from a previous ingest of %s", orphans, source)
    except Exception as exc:  # noqa: BLE001 - 정리는 최선형이며 치명적이지 않음
        log.warning("stale-chunk cleanup failed for %s: %s", source, exc)


def _ensure_collection(client: QdrantClient, dim: int, recreate: bool) -> None:
    if client.collection_exists(COLLECTION) and recreate:
        log.info("recreating collection %s", COLLECTION)
        client.delete_collection(COLLECTION)
    # 명명된 밀집 벡터(+ 하이브리드가 켜져 있으면 BM25 희소 벡터) 스키마 (vectorstore와 공유).
    vectorstore.ensure_collection(client, COLLECTION, dim, payload_indexes=_INDEXED_FIELDS)


# --- 수집 실행 ----------------------------------------------------------------
def _discover_files(path: Path) -> list[Path]:
    """`path` 아래의 지원되는 모든 (.md/.pdf) 파일. ID가 안정적이도록 정렬합니다."""
    return sorted(
        p for p in path.rglob("*")
        if p.is_file() and p.suffix.lower() in _SUPPORTED_SUFFIXES
    )


def ingest_file(client: QdrantClient, file: Path, root: Path, *, ensure: bool = True,
                recreate: bool = False) -> int:
    """파일 하나를 색인하고 저장한 청크 수를 반환합니다 (건너뛰면 0).

    `root`는 문서 디렉터리입니다. source(문서 경로)와 기본 doc_type을 여기서 정하므로, 같은 파일은
    누가 수집하든(rag-ingest, MCP 쓰기 도구) 같은 포인트 ID를 갖습니다. `ensure`가 참이면 컬렉션이
    없을 때 만들고, `recreate`가 참이면 기존 컬렉션을 지우고 새로 만듭니다.
    """
    if file.suffix.lower() == ".pdf":
        meta: dict[str, Any] = {}
        body = _extract_pdf_text(file)
        if not body:
            log.warning("skipping PDF with no extractable text %s", file)
            return 0
    else:
        raw = file.read_text(encoding="utf-8")
        meta, body = _parse_front_matter(raw)
        if not body:
            log.warning("skipping empty file %s", file)
            return 0

    doc_type = _infer_doc_type(meta, file, root)
    source = str(file.relative_to(root)).replace(os.sep, "/")
    title = meta.get("title") or file.stem
    tags = meta.get("tags") or []

    chunks = _chunk_document(body)
    dense = _embed_batch(chunks)
    sparse = vectorstore.embed_documents_sparse(chunks)

    if ensure:
        _ensure_collection(client, len(dense[0]), recreate)

    # front matter의 선택적 메타데이터를 필터용 페이로드로 그대로 넘깁니다.
    extra = {k: meta[k] for k in ("component", "severity", "cluster") if meta.get(k)}
    points = [
        PointStruct(
            id=str(uuid.uuid5(_ID_NAMESPACE, f"{source}#{i}")),
            vector=vectorstore.named_vectors(d, s),
            payload={
                "text": chunk,
                "doc_type": doc_type,
                "title": title,
                "source": source,
                "tags": tags,
                "chunk": i,
                **extra,
            },
        )
        for i, (chunk, d, s) in enumerate(zip(chunks, dense, sparse))
    ]
    _upsert_points(client, points)
    _delete_orphan_chunks(client, source, len(points))
    log.info("ingested %s (%s, %d chunk(s))", source, doc_type, len(points))
    return len(points)


def ingest(path: Path, recreate: bool) -> None:
    files = _discover_files(path)
    if not files:
        log.warning("no .md or .pdf files found under %s", path)
        return

    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=HTTP_TIMEOUT)
    collection_ready = False
    total_chunks = 0

    for file in files:
        # 컬렉션 준비(필요하면 재생성)는 실제로 저장할 첫 파일에서 한 번만 합니다.
        n = ingest_file(client, file, path, ensure=not collection_ready,
                        recreate=recreate and not collection_ready)
        if n:
            collection_ready = True
            total_chunks += n

    log.info("done: %d file(s), %d chunk(s) into '%s'", len(files), total_chunks, COLLECTION)


# --- 명령줄 -------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest markdown/PDF docs into the Qdrant knowledge base.")
    parser.add_argument(
        "--path",
        default="./knowledge",
        help="Directory tree of .md and .pdf documents to ingest (default: ./knowledge).",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and recreate the collection before ingesting (full rebuild).",
    )
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        parser.error(f"path not found or not a directory: {root}")

    emb = embeddings.describe()
    log.info(
        "ingesting from %s -> qdrant=%s collection=%s embed=%s:%s@%s",
        root, QDRANT_URL, COLLECTION, emb["provider"], emb["model"], emb["base_url"],
    )
    ingest(root, args.recreate)


if __name__ == "__main__":
    main()
