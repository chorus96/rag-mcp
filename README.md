# rag-mcp

**AI 어시스턴트용 RAG 메모리 서버**입니다. 런북, 장애 보고서, RCA(근본 원인 분석), 위키 내보내기 같은
마크다운·PDF 문서를 색인해 두고, [Model Context Protocol](https://modelcontextprotocol.io)(MCP)로
검색 도구를 제공합니다. Claude Code, Claude Desktop 등 MCP를 지원하는 클라이언트라면
어디서든 "전에 이런 장애가 있었나?", "이 작업의 처리 절차는?" 같은 질문에 여러분의 문서를 근거로
답하게 할 수 있습니다.

## 특징

- **읽기 전용 LLM 인터페이스** — 모델은 *검색*만 할 수 있습니다. 문서 기록은 수집 명령(`rag-ingest`)이나
  토큰으로 보호되는 내부 API로만 이루어집니다.
- **하이브리드 검색** — 의미 기반 밀집(dense) 벡터와 BM25 키워드 희소(sparse) 벡터를 Reciprocal Rank
  Fusion(RRF)으로 결합합니다. `CrashLoopBackOff` 같은 에러 문자열이나 리소스 이름도 정확히 찾습니다.
- **선택적 리랭킹** — Cohere/Jina 호환 크로스 인코더로 결과 순서를 다시 매겨 정밀도를 높입니다.
- **OpenAI 호환 임베딩** — `/v1/embeddings`를 제공하는 엔드포인트라면 무엇이든 씁니다. 직접 띄운
  서버(TEI, vLLM 등)로 오프라인 운영하거나 호스팅 API(OpenAI 등)를 쓸 수 있습니다.
- **한국어 지원** — 기본 임베딩 모델은 다국어 모델 `bge-m3`, 기본 리랭커는 다국어 모델입니다.
- **마크다운 + PDF 수집** — YAML front matter, 헤딩 기준 청킹, 멱등(idempotent) 재실행을 지원합니다.
- **간단한 설치** — Linux + systemd 서버에 스크립트 하나로 설치합니다. root 권한이 필요 없습니다.
- **Claude Code 플러그인** — 이 저장소 자체가 플러그인 마켓플레이스입니다.

## 구성 요소

| 구성 요소 | 역할 |
|------|------|
| **rag-mcp 서버** (`tools/server.py`) | MCP 검색 도구를 `http://<서버>:8084/mcp`(streamable-http)로 제공 |
| **Qdrant** | 문서 청크의 벡터를 저장하는 벡터 DB (`127.0.0.1:6333`) |
| **rag-ingest** (`tools/ingest.py`) | 문서를 청크로 나누고 임베딩해 Qdrant에 기록하는 수집 명령 |
| **임베딩 엔드포인트** | 텍스트를 벡터로 바꾸는 OpenAI 호환 서버 — **별도로 준비** |
| **Claude Code 플러그인** (`plugins/rag-mcp`) | Claude Code에서 rag-mcp 서버에 연결 |

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
  │  · parses front matter    │───────────►│   any OpenAI-compatible     │
  │  · heading-aware chunks   │            │   /v1/embeddings endpoint   │
  │    (PDF pages = sections) │◄───────────│   (TEI, vLLM, OpenAI, ...)  │
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
  │  Claude Desktop ·    │  (agent / CI capturing
  │  your own agents     │  incidents + human feedback)
  └──────────────────────┘
```

- **쓰기 경로:** `rag-ingest`가 `knowledge/` 아래 문서를 읽어 청크로 나누고, 임베딩 엔드포인트로
  벡터를 만들어 Qdrant에 저장합니다. BM25 희소 벡터는 rag-mcp가 직접(FastEmbed) 계산합니다.
- **읽기 경로:** MCP 클라이언트가 검색 도구를 호출하면, rag-mcp가 **같은 임베딩 엔드포인트**로 질의를
  벡터로 바꾸고 Qdrant에서 하이브리드 검색한 뒤 (선택적으로 리랭킹해) 결과를 돌려줍니다.
- **내부 쓰기 API:** 신뢰할 수 있는 자동화(에이전트, CI)가 장애 기록과 피드백을 남기는 HTTP 경로입니다.
  LLM에는 보이지 않습니다.

## 빠른 시작

### 사전 요구 사항

- systemd를 쓰는 Linux 서버 (Ubuntu, Debian, RHEL 계열 등)
- Python 3.10 이상과 venv 모듈 (Debian/Ubuntu: `apt install python3-venv`)
- `curl`, `tar`
- **OpenAI 호환 임베딩 엔드포인트** — 예: 같은 서버에서
  [Hugging Face TEI](https://github.com/huggingface/text-embeddings-inference)로 `BAAI/bge-m3` 서빙,
  또는 OpenAI API ([임베딩 엔드포인트 설정](#임베딩-엔드포인트-설정) 참고)

### 1. 설치

서비스를 실행할 **일반 사용자**로 설치합니다. root(sudo)는 필요 없습니다.

```bash
git clone https://github.com/chorus96/rag-mcp.git && cd rag-mcp
./deploy/install.sh             # Qdrant + rag-mcp 설치, systemd 사용자 서비스로 시작
```

`rag-ingest` 명령은 `~/.local/bin`에 설치됩니다. 이 경로가 PATH에 없으면 설치 스크립트가 알려 줍니다.

### 2. 임베딩 엔드포인트 지정

설정 파일에서 임베딩 엔드포인트를 지정하고 서버를 재시작합니다.

```bash
vi ~/.config/rag-mcp/rag-mcp.env
#   EMBEDDINGS_BASE_URL=http://localhost:8080   (예: 같은 서버의 TEI)
#   EMBEDDINGS_API_KEY=
#   EMBEDDINGS_MODEL=bge-m3
systemctl --user restart rag-mcp
```

### 3. 문서 색인

```bash
rag-ingest                      # 샘플 문서 색인
```

요약 줄 `done: N file(s), M chunk(s)`가 나오면 성공입니다.

### 4. 확인

```bash
systemctl --user status qdrant rag-mcp
curl -s http://localhost:8084/mcp            # 핸드셰이크 없이 400 응답 = 정상 동작
curl -s http://localhost:6333/collections    # rag_kb 컬렉션 확인
journalctl --user -u rag-mcp -f              # 서버 로그
```

> 최초 시작 시 FastEmbed가 BM25 모델을 `huggingface.co`에서 한 번 내려받아 캐시에 저장합니다.
> 프록시가 필요하면 설정 파일에 `HTTPS_PROXY`를 넣으세요. 내려받지 못하면 키워드(BM25) 검색 없이
> 의미 검색만으로 동작합니다(로그의 `hybrid=False`로 확인할 수 있습니다).

### 5. 클라이언트 연결

Claude Code라면 [플러그인](#claude-code-플러그인-권장)으로 연결하는 것이 가장 간단합니다.

## 설치 구조와 운영

설치 스크립트는 현재 사용자 홈 아래에 설치하고, 서비스는 사용자 systemd(`systemctl --user`)로
실행합니다. 다시 실행하면 업그레이드로 동작합니다 — 코드와 의존성을 갱신하고 서비스를 재시작하며,
설정 파일과 데이터는 건드리지 않습니다.

| 경로 | 내용 |
|------|------|
| `~/.local/share/rag-mcp/app`, `~/.local/share/rag-mcp/venv` | 애플리케이션 코드와 Python 가상환경 |
| `~/.local/share/rag-mcp/qdrant/qdrant` | Qdrant 바이너리 (기본 `v1.12.4`, `127.0.0.1`에만 바인드) |
| `~/.config/rag-mcp/rag-mcp.env` | 설정 파일 ([.env.example](.env.example) 복사본, 권한 600) |
| `~/.local/share/rag-mcp/data/knowledge` | 색인할 문서 (샘플 문서가 복사됨) |
| `~/.local/share/rag-mcp/data/qdrant` | Qdrant 데이터 |
| `~/.config/systemd/user/{qdrant,rag-mcp}.service` | systemd 사용자 서비스 |
| `~/.local/bin/rag-ingest` | 문서 수집 명령 |

### 운영 명령

| 작업 | 명령 |
|------|------|
| 상태 | `systemctl --user status qdrant rag-mcp` |
| 로그 | `journalctl --user -u rag-mcp -f` |
| 설정 적용 | `systemctl --user restart rag-mcp` |
| 문서 수집 | `rag-ingest [--recreate]` |
| 업그레이드 | `./deploy/install.sh` |
| 제거 (설정·데이터 유지) | `./deploy/uninstall.sh` |
| 제거 (모두 삭제) | `./deploy/uninstall.sh --purge` |

### 주의 사항

- **로그아웃하면 서비스가 멈춥니다.** 사용자 서비스는 기본적으로 로그인해 있는 동안만 실행됩니다.
  로그아웃 후에도 계속 실행하고 부팅 시 자동으로 시작하려면 `loginctl enable-linger $USER`를 한 번
  실행하세요(배포판에 따라 관리자 권한이 필요할 수 있습니다).
- **ssh 등으로 직접 로그인한 세션에서 설치하세요.** `su`나 `sudo -u`로 전환한 셸에서는 사용자
  systemd에 연결되지 않습니다. 서비스 등록 없이 파일만 설치하려면 `SKIP_START=1 ./deploy/install.sh`를
  쓰세요.
- **Python venv 모듈**(Debian/Ubuntu의 `python3-venv`)이 없다면 그 패키지 설치만은 관리자에게 요청해야
  합니다.
- **같은 서버에서는 한 명만 실행할 수 있습니다.** 포트(8084, 6333, 6334)가 겹치기 때문입니다.

## MCP 클라이언트 연결

서버는 `http://<서버 주소>:8084/mcp`에서 **streamable-http**로 통신합니다. 다른 서버에서 접속한다면
방화벽에서 8084 포트를 열고, 설정 파일에 `RAG_INTERNAL_TOKEN`을 설정해 내부 쓰기 API를 보호하세요.

### Claude Code 플러그인 (권장)

이 저장소는 Claude Code 플러그인 마켓플레이스입니다. `rag-mcp` 플러그인을 설치하면 MCP 서버 연결과
함께, 언제 어떤 검색 도구를 쓸지 알려 주는 스킬(`rag-knowledge`)이 추가됩니다.

```text
/plugin marketplace add chorus96/rag-mcp
/plugin install rag-mcp@rag-mcp
/plugin configure rag-mcp@rag-mcp      # server_url: 예) http://localhost:8084/mcp
```

터미널에서는 한 번에 설치하고 설정할 수도 있습니다.

```bash
claude plugin marketplace add chorus96/rag-mcp
claude plugin install rag-mcp@rag-mcp --config server_url=http://<서버>:8084/mcp
```

설정한 뒤 Claude Code를 재시작하고 `/mcp`에서 `plugin:rag-mcp:rag`가 연결됐는지 확인하세요.

**환경 변수로 주소 지정:** 환경 변수 `RAG_MCP_URL`이 있으면 `server_url` 설정보다 우선합니다. 설정을
바꾸지 않고 셸이나 서버마다 다른 주소를 쓸 때 편합니다.

```bash
RAG_MCP_URL=http://10.0.0.5:8084/mcp claude
```

| 우선순위 | 주소를 정하는 곳 |
|------|------|
| 1 | 환경 변수 `RAG_MCP_URL` |
| 2 | 플러그인 설정 `server_url` (`/plugin configure`) |

플러그인은 서버에 연결만 하므로, rag-mcp 서버는 [빠른 시작](#빠른-시작)대로 따로 설치해 두어야 합니다.

### Claude Code 직접 설정

플러그인 없이 프로젝트의 `.mcp.json`에 직접 등록할 수도 있습니다. 플러그인과 마찬가지로 환경 변수
`RAG_MCP_URL`이 있으면 그 주소를, 없으면 `http://localhost:8084/mcp`를 씁니다.

```json
{
  "mcpServers": {
    "rag": {
      "type": "http",
      "url": "${RAG_MCP_URL:-http://localhost:8084/mcp}"
    }
  }
}
```

프로젝트 `.mcp.json`의 서버는 처음 쓸 때 Claude Code가 사용 승인을 묻습니다.

### 그 밖의 MCP 클라이언트

`http://<rag-mcp 서버 주소>:8084/mcp`를 HTTP(streamable-http) MCP 서버로 등록하면 됩니다.

## 검색 도구

| 도구 | 용도 |
|------|---------|
| `rag_search(query, doc_type?, cluster?, component?, limit?)` | 지식 베이스 전체에 대한 시맨틱 검색 |
| `search_incidents(query, cluster?, component?, limit?)` | "전에 이런 일이 있었나?" — 장애(`incident`)만 검색 |
| `search_runbooks(query, cluster?, component?, limit?)` | "처리 절차가 뭐지?" — 런북(`runbook`)만 검색 |
| `rag_collections()` | 컬렉션 목록과 포인트 수 (지식 베이스가 채워졌는지 확인) |
| `rag_health()` | Qdrant와 임베딩 엔드포인트 접근 가능 여부 |

- `cluster`는 **소프트 필터**입니다. 같은 클러스터 결과가 없으면 전체 범위로 다시 검색하고
  `cluster_narrowed: false`로 알려 줍니다.
- `doc_type`과 `component`는 **하드 필터**입니다. 결과가 없으면 그대로 비어 있습니다.

## 문서 추가하기

문서 디렉터리(기본값 `~/.local/share/rag-mcp/data/knowledge`, 설정 파일의 `RAG_KNOWLEDGE_DIR`)에 마크다운이나
PDF를 넣고 수집 명령을 실행합니다. 서버 재시작은 필요 없습니다.

```bash
cp my-runbook.md ~/.local/share/rag-mcp/data/knowledge/runbooks/
rag-ingest
```

하위 폴더 이름이 기본 문서 유형이 됩니다(`knowledge/incidents/*` → `incident`,
`knowledge/runbooks/*` → `runbook`). 마크다운은 선택적으로 YAML front matter를 쓸 수 있습니다.

```markdown
---
title: Longhorn 볼륨이 attaching 상태에서 멈춤
type: incident
tags: [longhorn, storage]
component: longhorn
cluster: prod-eu
---
# 본문...
```

`type`, `component`, `cluster`는 검색 필터가 되지만 형식이 정해지지 않은 레이블입니다. 환경, 고객,
제품, 팀 등 도메인에 맞게 쓰거나 생략해도 됩니다. PDF는 페이지 단위로 텍스트를 추출하며, 제목은
파일 이름에서 가져옵니다.

수집은 멱등적이라 다시 실행해도 중복이 생기지 않고, 내용이 줄어든 문서의 남은 청크는 자동으로
지워집니다. 정기적으로 실행하려면 cron이나 CI에 등록하세요.

> **알려진 제한:** 파일을 삭제하거나 이름을 바꿔도 기존 청크는 남습니다. 그런 경우에는
> `rag-ingest --recreate`로 전체를 다시 만드세요.

## 임베딩 엔드포인트 설정

임베딩은 OpenAI 호환 `/v1/embeddings` 엔드포인트로 처리합니다. 문서가 의도치 않게 외부로 전송되지
않도록 `EMBEDDINGS_BASE_URL`에는 기본값이 없으며 반드시 지정해야 합니다. `/v1`은 자동으로 붙습니다.

**직접 띄운 서버 (오프라인, 권장)** — 예: 같은 서버의 TEI로 `BAAI/bge-m3` 서빙. vLLM, LocalAI 등도
같은 방식입니다.

```bash
# ~/.config/rag-mcp/rag-mcp.env
EMBEDDINGS_BASE_URL=http://localhost:8080
EMBEDDINGS_API_KEY=
# 엔드포인트가 쓰는 모델 이름 (예: vLLM은 BAAI/bge-m3)
EMBEDDINGS_MODEL=bge-m3
```

**호스팅 API** — 예: OpenAI. 문서 내용이 외부로 전송된다는 점에 주의하세요.

```bash
# ~/.config/rag-mcp/rag-mcp.env
EMBEDDINGS_BASE_URL=https://api.openai.com
EMBEDDINGS_API_KEY=sk-...
EMBEDDINGS_MODEL=text-embedding-3-small
```

> ⚠️ **수집과 질의는 항상 같은 엔드포인트와 모델을 써야 합니다.** 엔드포인트나 모델을 바꾼 뒤에는
> 서버를 재시작하고 컬렉션을 다시 만드세요.
>
> ```bash
> systemctl --user restart rag-mcp
> rag-ingest --recreate
> ```

### 한국어 문서

- **임베딩:** 기본 모델 `bge-m3`는 한국어를 포함한 다국어 모델이라, 엔드포인트에서 bge-m3를 서빙하면
  한국어 문서와 질의를 바로 쓸 수 있습니다. OpenAI `text-embedding-3-small`/`-large`도 다국어를
  지원합니다. 영어 문서만 쓴다면 `nomic-embed-text` 같은 더 가벼운 모델로 바꿀 수 있습니다(nomic
  계열은 작업 접두사가 자동으로 붙습니다).
- **리랭커:** 리랭킹을 켜면(`RERANK_PROVIDER=cohere`) 기본 모델은 다국어 모델
  `rerank-multilingual-v3.0`입니다.
- **BM25 키워드 검색:** 영어 기준으로 단어를 나누므로 조사가 붙은 한국어("볼륨이", "볼륨을")에는
  약합니다. 의미 검색이 상당 부분 보완하며, 영어 토큰(에러 문자열, 리소스 이름)은 잘 찾습니다.

자세한 내용은 [docs/DESIGN.md의 "한국어 / 다국어 문서"](docs/DESIGN.md#한국어--다국어-문서)를
참고하세요.

## 설정

모든 설정은 설정 파일(`~/.config/rag-mcp/rag-mcp.env`)의 환경 변수로 합니다. 주석이 달린 전체
목록은 [.env.example](.env.example)에, 전체 환경 변수 표와 설계 설명은 [docs/DESIGN.md](docs/DESIGN.md)에
있습니다. 자주 쓰는 항목은 다음과 같습니다.

| 변수 | 기본값 | 설명 |
|------|------|------|
| `EMBEDDINGS_BASE_URL` | _(없음, 필수)_ | OpenAI 호환 임베딩 엔드포인트 |
| `EMBEDDINGS_API_KEY` | _(비어 있음)_ | 임베딩 엔드포인트 API 키 |
| `EMBEDDINGS_MODEL` | `bge-m3` | 임베딩 모델 이름 |
| `RERANK_PROVIDER` | `none` | 리랭킹 사용 여부 (`none` 또는 `cohere`) |
| `RAG_HYBRID` | `true` | 하이브리드 검색 (바꾸면 `--recreate` 필요) |
| `RAG_KNOWLEDGE_DIR` | `~/.local/share/rag-mcp/data/knowledge` | 수집할 문서 디렉터리 |
| `MCP_PORT` | `8084` | MCP 서버 포트 |
| `RAG_INTERNAL_TOKEN` | _(비어 있음)_ | 내부 쓰기 API 보호 토큰 |

> 설정 파일에서는 `KEY=value  # 주석`처럼 같은 줄 끝에 주석을 달지 마세요. systemd가 주석까지 값으로
> 읽습니다.

## 내부 쓰기 API (선택 사항)

읽기 전용 MCP 도구와 별개로, 신뢰할 수 있는 자동화 프로세스가 장애 기록을 남길 수 있는 HTTP
경로(`/internal/knowledge/capture|similar|feedback|stats`)를 제공합니다("지식 플라이휠"). 이 경로는
LLM에 **노출되지 않으며**, `RAG_INTERNAL_TOKEN`으로 보호합니다. 자세한 내용은
[docs/DESIGN.md](docs/DESIGN.md)를 참고하세요.

## 저장소 구조

| 경로 | 내용 |
|------|------|
| `tools/server.py` | MCP 서버 (검색 도구 + 내부 쓰기 API) |
| `tools/ingest.py` | 문서 수집 |
| `tools/embeddings.py`, `tools/vectorstore.py`, `tools/reranker.py`, `tools/capture.py` | 임베딩, Qdrant, 리랭킹, 장애 기록 |
| `requirements.txt` | Python 의존성 |
| `deploy/` | 설치·제거 스크립트, systemd 유닛, `rag-ingest` 명령 |
| `knowledge/` | 샘플 문서 |
| `.claude-plugin/marketplace.json` | Claude Code 플러그인 마켓플레이스 정의 |
| `plugins/rag-mcp/` | Claude Code 플러그인 (MCP 서버 설정, `rag-knowledge` 스킬) |
| `docs/DESIGN.md` | 설계 문서 (검색 파이프라인, 전체 환경 변수 표) |
| `.claude/skills/repo-check/` | 저장소 점검 스킬 (Claude Code에서 `/repo-check`, 직접 실행: `bash .claude/skills/repo-check/scripts/check.sh`) |
| `tests/` | 테스트 |

## 개발

```bash
python3 -m pip install -r requirements.txt pytest
python3 -m pytest tests/

# 설치 없이 직접 실행 (로컬 :6333 의 Qdrant 필요)
cp .env.example .env && set -a && . ./.env && set +a
python3 tools/server.py
python3 tools/ingest.py --path knowledge
```

## 라이선스

[MIT](LICENSE)
