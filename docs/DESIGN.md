# rag-mcp 설계 문서

이 문서는 rag-mcp가 **어떻게 동작하고 왜 그렇게 설계했는지** 설명합니다. 설치와 사용법은 최상위
[README](../README.md)를 먼저 보세요.

## 1. 개요

rag-mcp는 런북, RCA(근본 원인 분석) 같은 운영 문서를 **Qdrant**에 색인하고, 그 지식 베이스에 대한
시맨틱 검색을 MCP 서버로 제공합니다. AI 에이전트는 운영 중인 시스템을 디버깅하면서 필요할 때마다
과거 맥락을 직접 찾아 씁니다(에이전틱 검색, agentic retrieval).

### 설계 원칙

| 원칙 | 내용 |
|------|------|
| **기본은 읽기 전용** | 기본 설정에서 MCP 도구는 검색과 정식 문서 목록 조회만 합니다. 지식 베이스 기록은 수집 명령(`rag-ingest`)으로 이루어집니다. 모델이 문서를 추가·삭제하는 쓰기 도구는 운영자가 `RAG_MCP_WRITE=true`로 켤 때만 등록됩니다. |
| **벤더 중립** | 채팅 LLM은 연결하는 MCP 클라이언트가 정합니다. 임베딩은 OpenAI 호환 `/v1/embeddings`라면 무엇이든 씁니다. |
| **수집과 질의의 일관성** | 수집과 질의가 같은 임베딩 코드와 설정을 공유하도록 만들어, 벡터가 어긋날 여지를 없앴습니다. |
| **실패해도 검색은 유지** | 리랭킹, BM25, 오래된 청크 정리 같은 부가 기능은 실패하면 조용히 건너뛰고, 기본 검색은 계속 동작합니다(최선형, best-effort). |

## 2. 구성 요소

| 모듈 | 역할 |
|------|------|
| [`server.py`](../tools/server.py) | FastMCP 서버. 읽기 도구 5개(검색 4개 + 정식 문서 목록)와 선택적 쓰기 도구 2개를 제공 |
| [`ingest.py`](../tools/ingest.py) | 마크다운/PDF 문서를 읽어 청크로 나누고 임베딩해 Qdrant에 업서트 |
| [`embeddings.py`](../tools/embeddings.py) | OpenAI 호환 임베딩 호출, 비대칭 모델 접두사 처리 |
| [`vectorstore.py`](../tools/vectorstore.py) | Qdrant 컬렉션 스키마, BM25 희소 벡터(FastEmbed), 하이브리드 질의 |
| [`reranker.py`](../tools/reranker.py) | Cohere/Jina 호환 크로스 인코더 리랭킹 (선택 사항) |
| [`documents.py`](../tools/documents.py) | 초안(`draft/`) 문서 로직: MCP 쓰기 도구의 추가·삭제, `rag-promote`의 목록·승격 |
| [`promote.py`](../tools/promote.py) | 초안 승격 명령 `rag-promote` (사람 전용, MCP 도구 아님) |
| [`plugins/rag-mcp`](../plugins/rag-mcp) | Claude Code 플러그인: MCP 서버 연결 설정과 스킬(`rag-knowledge`: 검색 도구 사용 안내, `rag-list-documents`: 정식 문서 목록) (서버 코드는 포함하지 않음) |

`ingest.py`, `documents.py`, `server.py`는 모두 `vectorstore.py`와 `embeddings.py`를 거칩니다. 그래서
쓰기 경로와 읽기 경로 사이에서 컬렉션 스키마, 벡터 이름, 임베딩 설정이 어긋나지 않습니다.

## 3. 데이터 모델

Qdrant 컬렉션 하나(기본 이름 `rag_kb`)에 모든 문서를 저장합니다.

### 벡터

| 이름 | 종류 | 내용 |
|------|------|------|
| `dense` | 밀집 벡터, 코사인 거리, HNSW 인덱스 | 임베딩 모델이 만든 의미 벡터 (bge-m3는 1024차원) |
| `bm25` | 희소 벡터, IDF 적용 | FastEmbed `Qdrant/bm25`로 만든 키워드 벡터 (하이브리드가 켜져 있을 때만) |

- 벡터 차원은 수집할 때 실제 임베딩 길이로 정해지므로, 모델을 바꿔도 코드를 고칠 필요가 없습니다.
- IDF는 컬렉션의 `Modifier.IDF`로 서버 측에서 계산하므로, 질의 쪽은 단어 존재 여부만 보내면 됩니다.

### 포인트 (청크)

| 출처 | 포인트 ID | 주요 페이로드 |
|------|------|------|
| 문서 수집 (`ingest.py`) | `uuid5(source#chunk)` | `text`, `doc_type`, `title`, `source`, `tags`, `chunk`, 그리고 front matter의 `component`/`severity`/`cluster` |

ID가 문서 경로와 청크 번호에서 결정되므로, 같은 문서를 다시 수집하면 중복이
생기지 않고 기존 포인트를 덮어씁니다.

### 페이로드 인덱스

필터링을 빠르게 하려고 다음 필드에 키워드 인덱스를 만듭니다.

- `doc_type`, `component`, `cluster`, `source` (`source`는 오래된 청크 정리용)

## 4. 검색 파이프라인

`server.py`의 검색 도구는 모두 같은 경로(`_search`)를 거칩니다.

1. **질의 임베딩** — 질의 텍스트를 임베딩 엔드포인트로 밀집 벡터로 바꿉니다.
2. **하이브리드 검색 (재현율)** — Qdrant Query API에서 밀집 검색과 BM25 검색을 동시에 실행하고
   (`Prefetch` 두 개), Reciprocal Rank Fusion(RRF)으로 결합합니다. BM25가 없으면 밀집 검색만 합니다.
3. **리랭킹 (정밀도, 선택 사항)** — 켜져 있으면 후보를 `RERANK_CANDIDATES`개(기본 30)까지 넉넉히
   가져와 크로스 인코더로 순서를 다시 매기고, 상위 `limit`개만 남깁니다.
4. **응답 구성** — 각 결과의 텍스트를 `RAG_SNIPPET_CHARS`(기본 1200자)까지 잘라 돌려줍니다. 잘렸으면
   `truncated: true`로 표시합니다.

### 쉬운 설명: 질의 하나가 처리되는 과정

`rag_search("Longhorn 볼륨이 attaching 상태에서 멈춤")`을 호출하면 질의가 **두 가지 표현으로 동시에**
바뀝니다. 두 검색 방식이 텍스트를 서로 다르게 이해하기 때문입니다.

- **밀집 벡터** — 질의의 *의미*가 숫자 목록(bge-m3는 1024개)이 됩니다. Qdrant는 HNSW 그래프 인덱스에서
  코사인 유사도로 가까운 청크를 찾습니다. HNSW는 *근사* 최근접 이웃 검색이라 매우 빠르고 수백만 개까지
  확장되지만, 결과가 수학적으로 정확한 top-k라는 보장은 없습니다(실제로는 거의 같습니다).
- **BM25 희소 벡터** — 질의의 *정확한 단어*가 `{단어 ID: 가중치}` 묶음이 됩니다. 역색인에서 단어를
  찾아, 밀집 벡터가 놓치기 쉬운 `CrashLoopBackOff`, `c-xxxxx`, `Longhorn` 같은 토큰을 잡아냅니다.

두 검색은 각자 순위 목록을 만들고, **RRF**가 이를 *점수가 아니라 순위*로 합칩니다. 어느 쪽 목록에서든
상위권에 오른 청크가 높은 결합 점수(`score ≈ Σ 1/(60 + rank)`)를 받습니다. 그래서 하이브리드 검색의
`score`는 코사인 유사도가 아니라 작은 결합 수치입니다.

리랭커가 켜져 있으면, 결합된 후보를 크로스 인코더(Cohere/Jina)에 보내 `(질의, 청크)` 쌍마다 점수를
다시 매깁니다. 밀집 검색은 *재현율*은 좋지만 *순서*가 약한데, 리랭커가 그 순서를 바로잡습니다.
리랭킹이 실패하면 결합된 순서를 그대로 씁니다.

### 개념 정리

| 용어 | 쉬운 의미 | 이 프로젝트에서 |
|------|------|------|
| 밀집 벡터 (dense vector) | 의미 / 시맨틱 | 임베딩 모델(bge-m3) → 1024개의 float |
| 희소 벡터 (sparse vector) | 정확한 단어 / 키워드 | FastEmbed BM25 → `{term_id: weight}` |
| HNSW | 근사 최근접 이웃 그래프 인덱스 | 빠른 코사인 검색, 거의 정확한 top-k |
| RRF | 두 순위 목록을 위치 기준으로 합치는 방법 | 밀집 + 희소 결과를 결합 |
| 리랭커 (reranker) | `(질의, 청크)` 쌍의 순서를 다시 매기는 크로스 인코더 | Cohere/Jina (선택 사항) |
| 비대칭 접두사 | 질의와 문서에 서로 다른 작업 태그를 붙이는 방식 | nomic 계열은 필요, bge-m3·OpenAI는 불필요 |

> ℹ️ **임베딩 모델은 여러분의 데이터로 학습되지 않습니다.** 사전 학습된 모델이 일반적인 의미 지식으로
> 텍스트를 숫자로 바꿀 뿐입니다. 조직이나 업계 고유의 맥락은 Qdrant에 수집한 **문서**와 (선택적으로)
> 리랭커에서 나옵니다.

## 5. MCP 도구와 필터

| 도구 | 용도 |
|------|------|
| `rag_search(query, doc_type?, cluster?, component?, limit?)` | 지식 베이스 전체에 대한 시맨틱 검색 |
| `search_runbooks(query, cluster?, component?, limit?)` | "처리 절차가 뭐지?" — `doc_type=runbook`으로 고정 |
| `rag_collections()` | 컬렉션 목록과 포인트 수 (지식 베이스가 채워졌는지 확인) |
| `rag_health()` | Qdrant와 임베딩 엔드포인트 접근 가능 여부, 리랭커·하이브리드 설정 |
| `rag_list_documents(subdir?, limit?)` | 정식 문서(`official/`) 파일 목록. 항목마다 `source`, `title`, `doc_type`, `size_bytes`, `modified`, `chunks`(색인된 청크 수, Qdrant facet 한 번으로 셈; 0이면 색인 전, 셀 수 없으면 `null`). 기본 100개, 최대 500개, 넘으면 `truncated: true`. `subdir`는 `official/` 밖을 가리키면 거부 |
| `rag_add_document(title, content, doc_type?, tags?, component?, cluster?, overwrite?)` | (선택) `draft/`에 문서 추가 — `RAG_MCP_WRITE=true`일 때만 등록. [7장](#mcp-쓰기-도구-rag_add_document-rag_delete_document) 참고 |
| `rag_delete_document(source)` | (선택) `draft/` 문서 삭제 — 파일과 청크를 함께 삭제. `RAG_MCP_WRITE=true`일 때만 등록 |

`limit`은 1부터 `RAG_MAX_LIMIT`(기본 20) 사이로 제한되며, 생략하면 `RAG_DEFAULT_LIMIT`(기본 5)입니다.

### 필터의 의미

| 필터 | 종류 | 결과가 비었을 때 |
|------|------|------|
| `doc_type` | 하드 | 그대로 빈 결과 |
| `component` | 하드 | 그대로 빈 결과 |
| `cluster` | **소프트** | 클러스터 조건만 빼고 전체 범위로 다시 검색 |

`cluster`를 소프트 필터로 둔 이유는, 새 클러스터에서 증상이 처음 나타났을 때 다른 클러스터의 선례가
가려지지 않게 하기 위해서입니다. 응답의 `cluster_narrowed`로 어떤 경우인지 알 수 있습니다.

| `cluster_narrowed` | 의미 |
|------|------|
| `None` | 클러스터를 지정하지 않음 |
| `true` | 지정한 클러스터 안에서 결과를 찾음 |
| `false` | 같은 클러스터에는 없어서 전체 범위 결과를 돌려줌 (응답에 `note` 포함) |

## 6. 문서 디렉터리와 수집

### 문서 디렉터리

지식 베이스의 **원본은 문서 디렉터리**(`RAG_KNOWLEDGE_DIR`, 기본 `~/.local/share/rag-mcp/data/knowledge`)입니다.
Qdrant의 포인트는 이 디렉터리의 파일에서 만든 사본이므로, `rag-ingest --recreate`로 언제든 다시 만들 수 있습니다.
MCP 쓰기 도구로 추가한 문서도 먼저 파일로 저장한 뒤 색인합니다.

```text
knowledge/
├── official/                  정식 문서 — 사람이 관리
│   ├── longhorn-volume-attach.md      → source: official/longhorn-volume-attach.md
│   └── runbooks/...                   (선택) 하위 폴더
└── draft/                     초안 — 모델이 MCP 쓰기 도구로 만듦
    └── longhorn-볼륨-복구.md          → source: draft/longhorn-볼륨-복구.md
```

| 폴더 | 쓰는 주체 | 들어오는 경로 | 나가는 경로 |
|------|------|------|------|
| `official/` | 사람 | 파일 복사 + `rag-ingest`, 또는 `rag-promote` 승격 | 사람이 파일 삭제 후 `--recreate` (또는 `source`로 청크 삭제) |
| `draft/` | 모델 (`RAG_MCP_WRITE=true`일 때만) | `rag_add_document` | `rag_delete_document`, 또는 `rag-promote`로 `official/`에 승격 |

규칙:

- **폴더는 문서의 단계(정식 / 초안)를 나타냅니다.** 문서 유형(`runbook`, `rca` 등)은 front matter의 `type`이
  나타냅니다. 단계와 유형을 분리했기 때문에 승격은 `draft/<경로>` → `official/<경로>`로 맨 앞 폴더만 바꾸는 단순한
  이동이 되고, 모델이 쓰는 위치도 `draft/` 하나로 고정됩니다.
- **`source`는 문서 디렉터리 기준 상대 경로입니다.** 포인트 ID(`uuid5(source#chunk)`)와 오래된 청크 정리의 기준이
  되고, 검색 결과에도 그대로 나옵니다. `source`가 `draft/`로 시작하면 초안입니다.
- **권한 경계는 디렉터리 하나로 판단합니다.** MCP 쓰기 도구는 경로를 정규화한 뒤 `draft/` 안인지만 확인하므로
  (`documents._resolve_source`), `official/`을 비롯한 그 밖의 경로는 모델이 만들거나 지울 수 없습니다.
- **두 폴더 모두 검색 대상입니다.** 초안은 저장 즉시 검색되고, 검토 여부는 `source`로 구분합니다.
- **서버가 문서 디렉터리를 읽는 것은 `rag_list_documents`뿐입니다.** 검색은 Qdrant만 씁니다. 목록 도구는
  `official/`의 파일을 직접 훑으므로, 아직 색인하지 않은 문서도 `chunks: 0`으로 보여 줍니다.
- **수집 대상은 `official/`과 `draft/` 아래뿐입니다.** 두 폴더 밖에 둔 문서는 `rag-ingest`가 건너뛰고 개수를 경고로
  알립니다(`ingest._discover_files`).

### 문서 형식

- **마크다운** — 선택적인 YAML front matter를 지원합니다.
- **PDF** — 페이지 단위로 텍스트를 추출하고, 각 페이지를 `# [Page N]` 섹션으로 만듭니다. front matter가
  없으므로 유형은 하위 폴더 이름(없으면 `note`)에서, 제목은 파일 이름에서 가져옵니다. 텍스트를 추출할 수 없는 스캔 PDF는
  건너뜁니다.

```markdown
---
title: Longhorn 볼륨이 attaching 상태에서 멈춤
type: rca             # runbook | rca | note | ...  (기본값: 하위 폴더 이름, 없으면 note)
tags: [longhorn, storage]
component: longhorn   # 선택: 필터용
cluster: prod-eu      # 선택: 필터용
---
# 본문...
```

문서 유형(`doc_type`)은 다음 순서로 정합니다(`ingest._infer_doc_type`).

| 순서 | 조건 | 결과 (예) |
|------|------|------|
| 1 | front matter에 `type`이 있음 | 그 값 |
| 2 | `official/`·`draft/` 아래 하위 폴더 안에 있음 | 첫 하위 폴더 이름에서 끝의 `s`를 뗀 값 (`official/runbooks/a.md` → `runbook`) |
| 3 | 그 밖 | `note` (`official/a.md`, `draft/a.md`) |

`official/`·`draft/` 바로 아래 둔 문서는 3번에 해당하므로 front matter에 `type`을 쓰는 것을 권장합니다.
MCP 쓰기 도구는 항상 `type`을 front matter에 기록합니다. `component`, `cluster`, `severity`는 형식이 정해지지
않은 레이블이라 도메인에 맞게 자유롭게 써도 됩니다.

### 청킹

1. 마크다운 헤딩 기준으로 섹션을 나눕니다. 런북의 단계나 RCA 문서의 한 섹션이 한 덩어리로 남습니다.
2. 각 청크 앞에 섹션 헤딩을 붙여, 청크 하나만 검색돼도 맥락을 알 수 있게 합니다.
3. 섹션이 `CHUNK_SIZE`(기본 1500자)보다 크면 문단 단위로 나누고, 청크 사이에 `CHUNK_OVERLAP`(기본
   100자)만큼 겹치게 합니다. 너무 큰 문단은 강제로 자릅니다.

### 멱등성과 오래된 청크

- **다시 실행해도 안전합니다.** 포인트 ID가 `(문서 경로, 청크 번호)`에서 정해지므로 기존 포인트를
  덮어씁니다.
- **문서가 줄어든 경우:** 예를 들어 30개였던 청크가 20개로 줄면, 그대로 두었을 때 20~29번 청크가 오래된
  검색 결과로 남습니다. 그래서 업서트 직후 현재 개수 이상의 청크를 삭제합니다.
- **문서를 삭제하거나 이름을 바꾼 경우:** 처리하지 않습니다. 수집할 파일이 없으니 그 청크가 고아가
  된 것을 알 수 없기 때문입니다. `source` 필드로 직접 지우거나 `--recreate`로 전체를 다시 만드세요.

### 배치 처리

큰 문서(예: 100페이지 PDF)는 청크 수백 개가 되고, 각 청크가 밀집 벡터·희소 벡터·텍스트를 담고 있어
한 번에 보내면 수 메가바이트가 됩니다. 그래서 요청을 나눕니다.

| 단계 | 요청당 크기 | 설정 |
|------|------|------|
| 임베딩 | 청크 32개 | `EMBED_BATCH_SIZE` |
| Qdrant 업서트 | 포인트 64개 | `QDRANT_UPSERT_BATCH` |

나눠 보내면 큰 파일을 처리하다 실패해도 앞선 배치는 이미 반영되어 있습니다. 엔드포인트가 413/400
오류를 돌려주면 `EMBED_BATCH_SIZE`를 줄이세요(엔드포인트마다 요청당 입력 개수와 토큰 상한이 다릅니다).

### 실행과 확인

```bash
rag-ingest                      # 문서 색인
rag-ingest --recreate           # 컬렉션을 지우고 전체 재구축
```

- `rag-ingest`는 서버와 같은 설정 파일을 읽으므로, 서버와 정확히 같은 임베딩 설정을 사용합니다.
- 요약 줄 `done: N file(s), M chunk(s)`가 예상과 맞는지 확인하세요. 문서가 빠졌다면 그 위의 로그에
  이유(지원하지 않는 확장자, 빈 파일, 텍스트 없는 PDF)가 나옵니다.
- 정기적으로 수집하려면 cron이나 systemd 타이머에 등록하세요.

```cron
# 매시 정각 (설치한 사용자의 crontab: crontab -e)
0 * * * * $HOME/.local/bin/rag-ingest >> $HOME/.local/share/rag-mcp/rag-ingest.log 2>&1
```

## 7. 쓰기 도구와 초안 승격

기본 설정에서 지식 베이스에 문서를 넣는 길은 [문서 디렉터리](#문서-디렉터리)의 `official/`과 `rag-ingest`뿐입니다. 여기에 더해, 운영자가 켜면
모델이 대화 중에 초안을 추가·삭제할 수 있고, 사람이 그 초안을 검토해 정식 문서로 올릴 수 있습니다.

### MCP 쓰기 도구 (`rag_add_document`, `rag_delete_document`)

**LLM이 직접 호출하는** 쓰기 도구입니다. 사용자가 대화 중에 "이 내용을 지식 베이스에
추가해 줘", "그 문서 지워 줘"라고 하면 모델이 문서를 저장하거나 삭제할 수 있습니다. 편리한 만큼 지식 베이스가 잘못된 내용으로 오염될
수 있으므로 **기본으로 꺼져 있고**, 운영자가 설정 파일에 `RAG_MCP_WRITE=true`를 넣어야 켜집니다.

| 항목 | 동작 |
|------|------|
| 등록 | `RAG_MCP_WRITE=true`일 때만 서버 시작 시 도구를 등록. 꺼져 있으면 도구 목록에도 없음 |
| 쓰기 범위 | 문서 디렉터리의 **`draft/` 아래만** 추가·삭제. 사람이 관리하는 정식 문서(`official/`)는 만들거나 지울 수 없음 |
| 저장 위치 | `<문서 디렉터리>/draft/<제목>.md` (예: `draft/longhorn-볼륨-멈춤.md`). 문서 유형은 폴더가 아니라 front matter의 `type`에 기록 |
| 파일 내용 | 인자로 받은 `title`, `type`, `tags`, `component`, `cluster`를 front matter로 쓰고 그 아래 본문 |
| 색인 | 저장 직후 `ingest.ingest_file`로 색인 — `rag-ingest`와 같은 코드라 청크 ID·페이로드가 같음 |
| 덮어쓰기 | 같은 경로에 파일이 있으면 `overwrite=true`일 때만 바꿈 (청크 수가 줄면 남은 청크도 정리) |
| 크기 제한 | 본문 `RAG_MAX_DOC_CHARS`자(기본 200000)까지 |
| 삭제 | `rag_delete_document(source)` — `draft/` 아래 문서 파일과 그 `source`의 청크를 함께 삭제 |

설계상 선택과 그 이유:

- **파일로 먼저 저장합니다.** 지식 베이스의 원본은 문서 디렉터리입니다. 파일로 남겨야 `rag-ingest --recreate`로
  재구축해도 추가한 문서가 사라지지 않고, 사람이 직접 확인·수정·삭제할 수 있습니다.
- **경로는 서버가 정합니다.** 호출자는 제목과 문서 유형만 넘기고, 파일 이름은 제목에서 만든 안전한 이름(글자·숫자·`-`)
  입니다. 경로에는 `draft/`와 이 파일 이름만 쓰이므로 `draft/` 밖에 쓸 수 없습니다. `doc_type`은 front matter와 검색
  필터 값이 되므로 소문자·숫자·`-`·`_`만 허용합니다.
- **모델은 `draft/`에만 씁니다.** 모델이 만든 문서는 초안으로 `draft/`에 모이고 저장 즉시 검색됩니다. 사람이
  검토한 뒤 `rag-promote`로 정식 폴더(`official/`)에 올리고(아래 "초안 승격"), 사람이 관리하는 정식 문서는 모델이 바꾸거나
  지울 수 없습니다. 검색 결과의 `source`가
  `draft/`로 시작하면 초안입니다.
- **색인에 실패해도 파일은 남깁니다.** 응답에 `saved: true`와 오류를 함께 돌려주므로, 원인을 고친 뒤
  `rag-ingest`로 다시 색인하면 됩니다.
- **삭제는 파일과 청크를 함께 지웁니다** (`rag_delete_document(source)`). 검색 결과나 추가 응답의 `source`로
  문서를 지정합니다. 파일이 이미 없고 청크만 남아 있어도 청크를 정리하므로, 파일을 지우거나 이름을 바꾼 뒤
  남은 청크를 없앨 때도 쓸 수 있습니다.
- **삭제 범위를 제한합니다.** `source`는 문서 디렉터리 기준 상대 경로여야 하고, 정규화했을 때 `draft/` 안이어야
  합니다(절대 경로, `draft/` 밖, `..`로 빠져나가는 경로는 거부). `.md`/`.pdf` 문서만 지울 수 있습니다.
- **청크 삭제에 실패하면 파일은 지우지 않습니다.** 파일만 사라지고 청크가 검색에 남는 상태를 피하기 위해서입니다.

### 초안 승격 (`rag-promote`)

모델이 만든 초안을 사람이 검토한 뒤 정식 문서로 올리는 **사람 전용 명령**입니다. MCP 도구가 아니므로 쓰기 도구를
켜도 모델은 승격할 수 없습니다. 그래서 "모델은 `draft/`에만, 정식 문서는 사람이 검토해서"라는 원칙이 유지됩니다.

```bash
rag-promote                                    # 초안 목록과 옮겨질 위치
rag-promote draft/foo.md [...]                 # draft/foo.md → official/foo.md
rag-promote --overwrite draft/foo.md           # 정식 위치에 같은 이름의 문서가 있으면 바꾸기
```

| 단계 | 동작 |
|------|------|
| 1. 확인 | `source`가 `draft/` 안의 `.md`/`.pdf` 파일인지 확인. 정식 위치에 문서가 있으면 `--overwrite` 없이는 중단 |
| 2. 복사 | 초안을 정식 위치(`draft/`를 `official/`로 바꾼 같은 경로)에 복사. 덮어쓸 때는 기존 내용을 메모리에 백업 |
| 3. 색인 | 정식 위치를 `ingest_file`로 색인 (`rag-ingest`와 같은 청크 ID) |
| 4. 정리 | 초안의 청크와 파일을 삭제 |

- **색인에 실패하면 되돌립니다.** 정식 위치를 원래대로(없던 파일은 삭제, 덮어쓴 파일은 백업 내용으로) 돌리고
  초안은 그대로 두므로, 실패해도 아무것도 바뀌지 않습니다.
- **초안 청크 정리에 실패하면** 승격은 끝난 상태로 초안 파일을 남기고 알려 줍니다. `rag-promote --overwrite`로
  다시 실행하거나 `rag_delete_document`로 초안을 지우면 됩니다.

## 8. 임베딩

### OpenAI 호환 엔드포인트

`EMBEDDINGS_BASE_URL`의 `/v1/embeddings`(URL이 `/v1`로 끝나면 `/embeddings`)에 배치 `input` 배열로
요청합니다. 응답은 `index`로 다시 정렬하므로, 엔드포인트가 순서를 바꿔 돌려줘도 청크와 벡터가 어긋나지
않습니다.

- **`EMBEDDINGS_BASE_URL`에는 기본값이 없습니다.** 설정을 빠뜨렸을 때 문서가 의도치 않게 외부 API로
  전송되지 않도록, 운영자가 엔드포인트를 명시해야 합니다. 비어 있으면 검색과 수집이 오류로 알려 줍니다.
- `EMBEDDINGS_API_KEY`가 있으면 `Authorization: Bearer` 헤더로 보내고, 비어 있으면 보내지 않습니다.
- HTTP 오류는 상태 코드와 응답 본문을 그대로 보여 줘, 잘못된 URL(404)이나 키 문제(401)를 바로 알 수
  있습니다.

### 비대칭 모델 접두사

일부 모델은 질의와 문서에 서로 다른 작업 태그를 붙여야 성능이 납니다.

| 모델 | 질의 접두사 | 문서 접두사 | 설정 |
|------|------|------|------|
| nomic 계열 | `search_query: ` | `search_document: ` | 이름에 `nomic`이 있으면 자동 |
| bge-m3, OpenAI `text-embedding-*` | 없음 | 없음 | 자동 (기본값) |
| e5, 구형 bge 등 | `query: ` | `passage: ` | `EMBED_QUERY_PREFIX` / `EMBED_DOC_PREFIX`로 직접 지정 |

### 일관성 규칙

> ⚠️ **수집과 질의는 반드시 같은 엔드포인트와 모델을 써야 합니다.** 서로 다른 모델의 벡터는 차원과
> 의미가 달라 비교할 수 없고, 섞어 쓰면 아무 경고 없이 검색이 망가집니다. 모델을 바꾸면 다시 수집하세요.
>
> ⚠️ **하이브리드를 켜고 끄면 컬렉션 스키마가 바뀝니다.** `RAG_HYBRID`를 바꾼 뒤에도 `--recreate`로
> 한 번 재구축해야 합니다.

## 한국어 / 다국어 문서

기본 임베딩 모델 이름은 한국어를 포함한 다국어 모델 **`bge-m3`**(1024차원, 최대 입력 8192 토큰)입니다.
영어 위주로 학습된 `nomic-embed-text`(768차원)보다 한국어 질의·문서의 의미 검색이 정확합니다. 호스팅
API라면 OpenAI `text-embedding-3-small`/`-large`도 다국어를 지원합니다. 영어 문서만 쓰고 더 가벼운
모델을 원하면 엔드포인트에서 `nomic-embed-text`를 서빙하고 `EMBEDDINGS_MODEL`만 바꾸면 됩니다.

### TEI로 bge-m3 서빙하기 (오프라인, 권장)

[Hugging Face TEI](https://github.com/huggingface/text-embeddings-inference)(Text Embeddings Inference)는
`BAAI/bge-m3`를 OpenAI 호환 `/v1/embeddings`로 서빙합니다. 설치 방법은 TEI 문서를 따르세요(CPU/GPU 빌드
제공). 같은 서버의 8080 포트에서 띄웠다면:

```bash
# ~/.config/rag-mcp/rag-mcp.env
EMBEDDINGS_BASE_URL=http://localhost:8080
# 인증이 없으면 비워 둠
EMBEDDINGS_API_KEY=
EMBEDDINGS_MODEL=bge-m3
```

vLLM, LocalAI 등 다른 OpenAI 호환 서버도 같은 방식입니다. 모델 이름은 그 서버가 쓰는 이름에 맞추세요
(예: vLLM은 기본으로 `BAAI/bge-m3`).

### 적용 및 확인

```bash
systemctl --user restart rag-mcp            # 새 모델로 질의하도록 재시작
rag-ingest --recreate                       # 컬렉션 재생성 + 재수집

# 컬렉션 차원 확인 → "size":1024 이면 성공
curl -s http://localhost:6333/collections/rag_kb | grep -o '"size":[0-9]*'
```

그다음 `rag_health()`에서 모델이 `bge-m3`로 표시되는지 확인하고, 한국어 질의로 `rag_search`를 실행해
보세요.

### 참고 사항

- **BM25는 한국어에 약합니다.** `Qdrant/bm25`는 영어 기준으로 단어를 나누므로 조사가 붙은 형태("볼륨이",
  "볼륨을")를 서로 다른 단어로 봅니다. RRF 결합에서 의미 검색이 상당 부분 보완하며, 영어 토큰(에러 문자열,
  리소스 이름)은 잘 찾습니다. 결과가 이상하면 `RAG_HYBRID=false`(밀집 전용)와 비교해 보세요.
- **리랭커도 다국어 모델이 기본값입니다.** 리랭킹을 켜면(`RERANK_PROVIDER=cohere`) 기본 모델은 Cohere
  `rerank-multilingual-v3.0`입니다. Jina라면 `jina-reranker-v2-base-multilingual`을 지정하세요. 영어 전용
  `rerank-english-v3.0`은 한국어 문서에 쓰지 마세요.
- **속도.** bge-m3(약 1.2GB)는 CPU로 서빙하면 수집이 느릴 수 있습니다. 문서가 많다면 GPU에서 서빙하거나
  `EMBED_BATCH_SIZE`를 엔드포인트가 허용하는 범위에서 늘리세요.
- **청크 크기.** 기본 `CHUNK_SIZE=1500`자는 bge-m3의 최대 입력 길이보다 훨씬 작아 그대로 써도 됩니다.

## 9. 배포 구조

설치 절차와 운영 명령은 [README](../README.md#설치-구조와-운영)에 있습니다. 여기서는 구조와 그 이유만
정리합니다.

| 항목 | 위치 |
|------|------|
| 실행 사용자 | 설치한 사용자 (root 불필요) |
| 앱 / venv / Qdrant | `~/.local/share/rag-mcp/{app,venv,qdrant}` |
| 설정 파일 | `~/.config/rag-mcp/rag-mcp.env` (권한 `600`) |
| 데이터 | `~/.local/share/rag-mcp/data/{knowledge,qdrant,fastembed_cache}` (`knowledge/` 아래 `official/`, `draft/`) |
| systemd 유닛 | [`deploy/systemd/`](../deploy/systemd) → `~/.config/systemd/user` (경로는 `%h`) |
| 명령 | `~/.local/bin/rag-ingest` (문서 수집), `~/.local/bin/rag-promote` (초안 승격) |

- **사용자 권한으로만 설치합니다.** 전용 시스템 사용자나 `/opt`, `/etc` 같은 시스템 경로를 쓰지 않으므로
  관리자 권한 없이 설치·업그레이드·제거할 수 있습니다. 설치 스크립트는 root로 실행하면 멈춥니다.
- **Qdrant는 `127.0.0.1`에만 바인드합니다.** rag-mcp만 접속하면 되므로 외부에 열 이유가 없습니다.
- **Qdrant API 키는 값이 있을 때만 넘깁니다.** Qdrant는 빈 키도 "키가 설정됨"으로 보고 모든 요청을
  거부하기 때문입니다.
- **systemd 샌드박스 옵션(`ProtectSystem` 등)은 쓰지 않습니다.** 사용자 systemd에서는 배포판에 따라 이
  옵션들이 동작하지 않거나 서비스 시작을 막을 수 있습니다. 대신 데이터와 설정 디렉터리를 본인만 접근할 수
  있게(`700`/`600`) 만듭니다.
- **`rag-ingest`는 깨끗한 환경에서 실행합니다.** 호출한 셸의 환경 변수를 넘기지 않고 설정 파일만으로
  환경을 만들어, 서비스와 같은 조건으로 수집합니다.
- **설치 스크립트는 업그레이드도 겸합니다.** 다시 실행하면 코드와 의존성만 갱신하고, 설정 파일과 데이터는
  건드리지 않습니다.
- **linger가 필요합니다.** 사용자 서비스는 기본적으로 로그인해 있는 동안만 실행됩니다. 로그아웃 후에도,
  부팅 직후에도 실행되게 하려면 `loginctl enable-linger $USER`를 켜세요. `XDG_CONFIG_HOME`을 기본값이 아닌
  곳으로 바꾼 환경은 지원하지 않습니다.

> **설정 파일 형식:** systemd의 `EnvironmentFile`은 `KEY=value  # 주석`처럼 같은 줄 끝의 주석까지 값으로
> 읽습니다. 주석은 항상 별도 줄에 쓰세요.

> **최초 시작과 네트워크:** FastEmbed가 BM25 모델을 `huggingface.co`에서 한 번 내려받아
> `fastembed_cache`에 저장합니다. 프록시가 필요하면 설정 파일에 `HTTPS_PROXY`를 넣으세요. 내려받지
> 못하면 밀집 검색만으로 동작합니다(로그에 `FastEmbed unavailable` 경고, 시작 로그에 `hybrid=False`).

### 클라이언트 쪽 서버 주소 (`RAG_MCP_URL`)

서버가 열리는 포트(`MCP_PORT`)는 서버 설정 파일에서 정하지만, 클라이언트가 **어느 주소로 접속할지**는
클라이언트 쪽에서 정합니다. Claude Code 플러그인과 README의 프로젝트 `.mcp.json` 예시는 모두 환경 변수
`RAG_MCP_URL`을 먼저 보고, 없을 때 기본 주소를 씁니다.

| 연결 방식 | 주소 결정 | 기본 주소 |
|------|------|------|
| 플러그인 (`plugins/rag-mcp/.mcp.json`) | `${RAG_MCP_URL:-${user_config.server_url}}` | 플러그인 설정 `server_url` (`/plugin configure`) |
| 프로젝트 `.mcp.json` | `${RAG_MCP_URL:-http://localhost:8084/mcp}` | `http://localhost:8084/mcp` |

- **왜 환경 변수인가:** 설정 파일이나 플러그인 설정을 고치지 않고도 셸·서버·CI마다 다른 rag-mcp 서버에
  접속할 수 있습니다 (예: `RAG_MCP_URL=http://10.0.0.5:8084/mcp claude`).
- **언제 읽히나:** Claude Code가 시작할 때 MCP 서버 설정을 만들면서 읽습니다. 값을 바꿨다면 Claude Code를
  다시 시작하세요.
- **서버는 이 변수를 읽지 않습니다.** 서버의 바인드 주소와 포트는 `MCP_HOST` / `MCP_PORT`(아래 표)로
  정하며, 둘을 바꾸면 클라이언트의 `RAG_MCP_URL`이나 `server_url`도 맞춰야 합니다.

## 10. 환경 변수

| 변수 | 기본값 | 설명 |
|-----|---------|-------|
| **연결** | | |
| `MCP_HOST` | `0.0.0.0` | 서버 바인드 주소 |
| `MCP_PORT` | `8084` | 서버 포트 |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant REST 엔드포인트 |
| `QDRANT_COLLECTION` | `rag_kb` | 컬렉션 이름 |
| `QDRANT_API_KEY` | _(미설정)_ | Qdrant 인증 키 (설정하면 Qdrant와 rag-mcp가 함께 사용) |
| `RAG_TIMEOUT_SECONDS` | `30` (서버의 Qdrant 연결) / `60` (임베딩 요청, 수집) | HTTP 타임아웃 (초). 설정하면 모두 이 값을 사용 |
| **임베딩** | | |
| `EMBEDDINGS_BASE_URL` | _(없음, 필수)_ | OpenAI 호환 임베딩 엔드포인트 (`/v1`은 자동으로 붙음). 예: `http://localhost:8080`(TEI), `https://api.openai.com` |
| `EMBEDDINGS_API_KEY` | _(미설정)_ | 엔드포인트 API 키 (비워 두면 인증 헤더를 보내지 않음) |
| `EMBEDDINGS_MODEL` | `bge-m3` | 임베딩 모델 이름 (엔드포인트가 쓰는 이름에 맞춤) |
| `EMBED_QUERY_PREFIX` / `EMBED_DOC_PREFIX` | 자동 (nomic → `search_query: `/`search_document: `, 그 외 → 빈 값) | 자동 감지가 놓치는 비대칭 모델에만 지정 (e5 등 → `query: `/`passage: `) |
| **수집** | | |
| `RAG_KNOWLEDGE_DIR` | `~/.local/share/rag-mcp/data/knowledge` | 문서 디렉터리 (`official/`, `draft/`). `rag-ingest`가 수집하고 쓰기 도구·`rag-promote`가 씀 (설치 스크립트가 실제 경로로 바꿔 넣음) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1500` / `100` | 청크 크기와 겹침 (글자 수) |
| `EMBED_BATCH_SIZE` | `32` | 임베딩 요청당 청크 수 |
| `QDRANT_UPSERT_BATCH` | `64` | Qdrant 업서트 요청당 포인트 수 |
| **검색** | | |
| `RAG_HYBRID` | `true` | 하이브리드 검색(밀집 + BM25). 바꾸면 `--recreate` 필요 |
| `RAG_SPARSE_MODEL` | `Qdrant/bm25` | FastEmbed 희소(BM25) 모델 |
| `RAG_DEFAULT_LIMIT` / `RAG_MAX_LIMIT` | `5` / `20` | 검색 결과 기본 개수 / 최대 개수 |
| `RAG_SNIPPET_CHARS` | `1200` | 결과마다 반환하는 텍스트의 최대 글자 수 (넘으면 `truncated: true`) |
| **리랭킹** | | |
| `RERANK_PROVIDER` | `none` | `none`(비활성) 또는 `cohere`(Cohere/Jina 호환 `/rerank`) |
| `RERANK_MODEL` | `rerank-multilingual-v3.0` | 리랭커 모델 (Cohere 다국어). Jina는 `jina-reranker-v2-base-multilingual` |
| `RERANK_BASE_URL` | `https://api.cohere.com` | 리랭커 API 기본 URL (Jina는 `https://api.jina.ai/v1`) |
| `RERANK_API_KEY` | _(미설정)_ | 리랭커 API 키 |
| `RERANK_CANDIDATES` | `30` | 리랭킹 전에 가져오는 후보 수 |
| `RERANK_TIMEOUT` | `30` | 리랭커 HTTP 타임아웃 (초) |
| **MCP 쓰기 도구** | | |
| `RAG_MCP_WRITE` | `false` | `true`면 MCP 쓰기 도구 `rag_add_document`, `rag_delete_document`를 등록 (LLM이 문서를 추가·삭제할 수 있음) |
| `RAG_MAX_DOC_CHARS` | `200000` | `rag_add_document`로 추가할 수 있는 본문의 최대 글자 수 |

주석이 달린 예시는 [`.env.example`](../.env.example)에 있습니다. 위 표는 모두 **서버**가 읽는 변수입니다.
클라이언트(Claude Code)가 읽는 `RAG_MCP_URL`은
[클라이언트 쪽 서버 주소](#클라이언트-쪽-서버-주소-rag_mcp_url)를 참고하세요.

## 11. 알려진 제한

- 문서를 삭제하거나 이름을 바꾸면 이전 청크가 남습니다 → `--recreate` 또는 `source`로 직접 삭제.
- BM25 키워드 검색은 한국어 형태소를 고려하지 않습니다.
- 스캔(이미지) PDF는 OCR을 하지 않으므로 수집되지 않습니다.
- 하이브리드 검색의 `score`는 RRF 결합 점수라서, 코사인 유사도처럼 임계값을 정하는 데 쓸 수 없습니다.
- 같은 서버에서는 rag-mcp를 하나만 실행할 수 있습니다. Qdrant 포트(6333, 6334)가 systemd 유닛에 고정되어
  있기 때문입니다.
