# rag-mcp

[Model Context Protocol](https://modelcontextprotocol.io)(MCP)로 노출되는 셀프 호스팅
**AI 어시스턴트용 RAG 메모리 서버**입니다. MCP를 지원하는 모든 LLM 클라이언트(Claude Desktop /
Claude Code, LibreChat Agents, 프록시를 통한 Open WebUI, 직접 만든 도구 등)에 여러분의 문서 —
런북, 장애 보고서, RCA(근본 원인 분석), 위키 내보내기 등 마크다운이나 PDF로 된 모든 것 — 를 대상으로 검색 가능한
장기 메모리를 제공합니다.

- **읽기 전용 LLM 인터페이스** — 모델은 *검색*만 할 수 있습니다. 쓰기는 배치 수집(ingestion) 작업이나
  토큰으로 보호되는 내부 API를 통해 별도 경로로 이루어집니다.
- **하이브리드 검색** — 밀집(dense) 시맨틱 벡터 **+ BM25 키워드 희소(sparse) 벡터**를
  Reciprocal Rank Fusion으로 결합합니다. 임베딩이 놓치더라도 정확한 토큰(`CrashLoopBackOff`,
  에러 코드, 리소스 이름)을 찾아냅니다.
- **선택적 크로스 인코더 리랭킹**(Cohere/Jina 호환)으로 정밀도를 높일 수 있습니다.
- **교체 가능한 임베딩** — 로컬 [Ollama](https://ollama.com)(오프라인 기본값) 또는
  OpenAI 호환 `/v1/embeddings` 엔드포인트. 환경 변수 하나로 전환합니다.
- **마크다운 + PDF 수집** — YAML front matter, 헤딩 인식 청킹, 멱등(idempotent) 재실행을 지원합니다.
- **벤더 중립 MCP** — streamable-http MCP를 지원하는 모든 클라이언트와 함께 동작합니다.

## 아키텍처

```
                       WRITE PATH — populate the KB
  ┌───────────────────────────┐
  │       knowledge/**        │   your markdown & PDF docs,
  │  runbooks · incidents ·   │   optional YAML front matter
  │  wikis · post-mortems     │
  └─────────────┬─────────────┘
                │
                ▼
  ┌───────────────────────────┐   embed    ┌─────────────────────────────┐
  │        rag-ingest         │  chunks    │     embedding provider      │
  │  · parses front matter    │───────────►│   ollama (offline default)  │
  │  · heading-aware chunks   │            │   or any OpenAI-compatible  │
  │    (PDF pages = sections) │◄───────────│   /v1/embeddings endpoint   │
  │  · idempotent upserts,    │  vectors   └──────────────▲──────────────┘
  │    stale-tail cleanup     │                           │
  └─────────────┬─────────────┘                           │  the SAME
                │ upsert                                  │  provider also
                ▼                                         │  embeds queries
  ┌───────────────────────────┐                           │  (see rag-mcp ↘)
  │      Qdrant  (v1.12)      │                           │
  │    collection:  rag_kb    │                           │
  │   dense (cosine · HNSW)   │                           │
  │   bm25  (sparse · IDF)    │                           │
  └─────────────▲─────────────┘                           │
                │                                         │
                │  1. hybrid search:                      │
                │     Prefetch(dense)+Prefetch(bm25)      │
                │     fused with Reciprocal Rank Fusion   │
                │                                         │
  ┌─────────────┴───────────────────────────────┐         │
  │            rag-mcp     :8084/mcp            │         │
  │   FastMCP server — READ-ONLY tool surface   │         │
  │                                             │  2. em- │
  │   rag_search         search_incidents       │  beds   │
  │   search_runbooks     rag_collections       │◄────────┘
  │   rag_health                                │  the
  │   (+ optional step 3: cross-encoder rerank  │  query
  │     via a Cohere/Jina-compatible /rerank)   │
  └──▲──────────────────────▲───────────────────┘
     │                      │
     │  MCP                 │  POST /internal/knowledge/*
     │  streamable-http     │  capture · similar · feedback · stats
     │  (search queries)    │  token-gated; NEVER visible to the LLM
     │                      │
  ┌──┴───────────────────┐  │
  │      MCP clients     │  ▼
  │  Claude Code ·       │  trusted automation
  │  LibreChat ·         │  (agent / CI capturing
  │  your own agents     │  incidents + human feedback)
  └──────────────────────┘
```

## 빠른 시작

사전 요구 사항: Docker(+ Compose v2), 그리고 기본 오프라인 임베딩 경로를 쓰려면 호스트에
[Ollama](https://ollama.com)가 필요합니다.

```bash
ollama pull nomic-embed-text     # 최초 1회

git clone https://github.com/mmelmesary/rag-mcp.git && cd rag-mcp
cp .env.example .env             # 기본값 그대로 바로 동작합니다

docker compose up -d --build     # Qdrant + rag-mcp 시작

docker compose run --rm rag-ingest   # ./knowledge 의 샘플 문서 색인
```

확인:

```bash
curl -s http://localhost:8084/mcp            # MCP 엔드포인트 (핸드셰이크 없이 400 응답 = 정상 동작)
curl -s http://localhost:6333/collections    # rag_kb 컬렉션이 포인트와 함께 존재
```

## MCP 클라이언트에 연결하기

서버는 `http://localhost:8084/mcp`에서 **streamable-http**로 통신합니다.

**Claude Code** (프로젝트의 `.mcp.json`):

```json
{
  "mcpServers": {
    "rag": {
      "type": "http",
      "url": "http://localhost:8084/mcp"
    }
  }
}
```

**LibreChat** (`librechat.yaml`):

```yaml
mcpServers:
  rag:
    type: streamable-http
    url: http://rag-mcp-server:8084/mcp   # compose 네트워크로 연결된 경우 컨테이너 이름 사용
```

그 밖의 MCP 클라이언트: 위 URL로 HTTP/streamable-http 서버를 등록하면 됩니다.

### 모델에 노출되는 도구

| 도구 | 용도 |
|------|---------|
| `rag_search(query, doc_type?, cluster?, component?, limit?)` | KB 전체에 대한 시맨틱 검색 |
| `search_incidents(query, cluster?, component?, limit?)` | "전에 이런 일이 있었나?" — `doc_type=incident` |
| `search_runbooks(query, cluster?, component?, limit?)` | "처리 절차가 뭐지?" — `doc_type=runbook` |
| `rag_collections()` | 컬렉션 목록과 포인트 수 |
| `rag_health()` | Qdrant 및 임베딩 제공자 접근 가능 여부 |

필터: `cluster`는 *소프트* 필터입니다(같은 클러스터 결과가 비어 있으면 전체 범위로 다시 검색).
`component`/`doc_type`은 하드 필터입니다.

## 내 문서 추가하기

`knowledge/` 아래에 마크다운이나 PDF를 넣으세요(하위 폴더 이름이 기본 `doc_type`이 됩니다.
예: `knowledge/incidents/*` → `incident`). 마크다운은 선택적으로 YAML front matter를 지원합니다.

```markdown
---
title: Longhorn volume stuck attaching
type: incident
tags: [longhorn, storage]
component: longhorn
cluster: prod-eu
---
# 본문...
```

front matter는 전적으로 자유롭게 정할 수 있습니다. `type`, `component`, `cluster`는 검색 필터
필드가 되지만 형식이 정해지지 않은 레이블일 뿐이므로, 도메인에 맞는 어떤 분류(환경, 고객, 제품,
팀 등)에든 사용하거나 아예 생략하고 순수 시맨틱 검색만 사용해도 됩니다.

그런 다음 다시 색인하세요 — 재빌드는 필요 없습니다(작업이 `./knowledge`를 바인드 마운트합니다).

```bash
docker compose run --rm rag-ingest
```

수집은 멱등적입니다(청크 ID는 `(source, chunk index)`에서 파생됩니다). 내용이 줄어든 문서는
더 이상 쓰이지 않는 뒷부분 청크가 삭제됩니다. 문서가 바뀔 때마다 CI/cron에서 실행하세요.
알려진 제한 사항: 파일을 삭제하거나 이름을 바꿔도 기존 청크는 제거되지 않습니다 — 삭제/이름 변경
후에는 `docker compose run --rm rag-ingest --recreate`를 사용하세요.

## 호스팅 임베딩 제공자로 전환하기

```bash
# .env
EMBEDDINGS_PROVIDER=openai
EMBEDDINGS_BASE_URL=https://api.openai.com    # 또는 LiteLLM 프록시 / Azure 게이트웨이 / TEI
EMBEDDINGS_API_KEY=sk-...
EMBEDDINGS_MODEL=text-embedding-3-small
```

그런 다음 컬렉션을 다시 만드세요(수집과 질의는 항상 같은 제공자+모델을 사용해야 합니다).

```bash
docker compose run --rm rag-ingest --recreate
```

## 설정

모든 설정은 환경 변수로 이루어집니다 — 주석이 달린 전체 목록은 [.env.example](.env.example)을,
설계 근거, 검색 파이프라인 상세 설명, 내부 쓰기 API, 전체 환경 변수 표는
[docs/DESIGN.md](docs/DESIGN.md)를 참고하세요.

## 내부 쓰기 API (선택 사항)

읽기 전용 MCP 도구 외에도, 서버는 *신뢰할 수 있는* 자동화 프로세스가 장애 기록을 쓸 수 있도록
일반 HTTP 라우트(`/internal/knowledge/capture|similar|feedback|stats`)를 제공합니다
("지식 플라이휠"). 이 라우트는 LLM에 **노출되지 않습니다**. `RAG_INTERNAL_TOKEN`으로 보호하세요.
[docs/DESIGN.md](docs/DESIGN.md)를 참고하세요.

## 개발

```bash
python3 -m pip install -r requirements.txt pytest
python3 -m pytest tests/
python3 server.py                       # 로컬 :6333 의 Qdrant에 연결해 실행
QDRANT_URL=http://localhost:6333 python3 ingest.py --path knowledge
```

## 라이선스

[MIT](LICENSE)
