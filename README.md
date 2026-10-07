# rag-mcp

**AI 어시스턴트용 RAG 메모리 서버**입니다. 마크다운·PDF 문서를 색인해 두고, [Model Context Protocol](https://modelcontextprotocol.io)(MCP)로
검색 도구를 제공합니다. Claude Code, Claude Desktop 등 MCP를 지원하는 클라이언트라면
어디서든 "이 작업은 어떻게 하지?", "관련 문서에는 뭐라고 되어 있지?" 같은 질문에 여러분의 문서를 근거로
답하게 할 수 있습니다. AI 에이전트는 작업하면서 필요할 때마다 관련 문서를 직접 찾아 씁니다(에이전틱 검색,
agentic retrieval).

## 특징

- **기본은 읽기 전용** — 기본 설정에서 모델은 *검색*과 *문서 목록 조회*만 할 수 있습니다. 문서 기록은 수집
  명령(`rag-ingest`)으로 이루어집니다. 원하면 모델이 초안(`draft/`)을 추가·삭제하는 쓰기 도구를 켤 수 있습니다.
- **하이브리드 검색** — 의미 기반 밀집(dense) 벡터와 BM25 키워드 희소(sparse) 벡터를 Reciprocal Rank
  Fusion(RRF)으로 결합합니다. `CrashLoopBackOff` 같은 에러 문자열이나 리소스 이름도 정확히 찾습니다.
- **선택적 리랭킹** — Cohere/Jina 호환 크로스 인코더로 결과 순서를 다시 매겨 정밀도를 높입니다.
- **OpenAI 호환 임베딩** — `/v1/embeddings`를 제공하는 엔드포인트라면 무엇이든 씁니다. 직접 띄운
  서버(TEI, vLLM 등)로 오프라인 운영하거나 호스팅 API(OpenAI 등)를 쓸 수 있습니다.
- **한국어 지원** — 기본 임베딩 모델은 다국어 모델 `bge-m3`, 기본 리랭커는 다국어 모델입니다.
- **마크다운 + PDF 수집** — YAML front matter, 헤딩 기준 청킹, 멱등(idempotent) 재실행을 지원합니다.
- **간단한 설치** — Linux + systemd 서버에 스크립트 하나로 설치합니다. root 권한이 필요 없습니다.
- **Claude Code 플러그인** — 이 저장소 자체가 플러그인 마켓플레이스입니다.

### 설계 원칙

| 원칙 | 내용 |
|------|------|
| **기본은 읽기 전용** | 기본 설정에서 MCP 도구는 검색과 문서 목록 조회만 합니다. 지식 베이스 기록은 수집 명령(`rag-ingest`)으로 이루어집니다. 모델이 문서를 추가·삭제하는 쓰기 도구는 운영자가 `RAG_MCP_WRITE=true`로 켤 때만 등록됩니다. |
| **벤더 중립** | 채팅 LLM은 연결하는 MCP 클라이언트가 정합니다. 임베딩은 OpenAI 호환 `/v1/embeddings`라면 무엇이든 씁니다. |
| **수집과 질의의 일관성** | 수집과 질의가 같은 임베딩 코드와 설정을 공유하도록 만들어, 벡터가 어긋날 여지를 없앴습니다. |
| **실패해도 검색은 유지** | 리랭킹, BM25, 오래된 청크 정리 같은 부가 기능은 실패하면 조용히 건너뛰고, 기본 검색은 계속 동작합니다(최선형, best-effort). |

## 구성 요소

| 구성 요소 | 역할 |
|------|------|
| **rag-mcp 서버** (`tools/server.py`) | MCP 도구를 `http://<서버>:8084/mcp`(streamable-http)로 제공 |
| **Qdrant** | 문서 청크의 벡터를 저장하는 벡터 DB (`127.0.0.1:6333`) |
| **rag-ingest** (`tools/ingest.py`) | 문서를 청크로 나누고 임베딩해 Qdrant에 기록하는 수집 명령 |
| **rag-promote** (`tools/promote.py`) | 검토한 초안(`draft/`)을 정식 문서(`official/`)로 올리는 사람 전용 명령 |
| **임베딩 엔드포인트** | 텍스트를 벡터로 바꾸는 OpenAI 호환 서버 — **별도로 준비** |
| **Claude Code 플러그인** (`plugins/rag-mcp`) | Claude Code에서 rag-mcp 서버에 연결하고, 도구 사용법을 스킬로 안내 |

## 아키텍처

```
                       WRITE PATH — populate the KB
  ┌───────────────────────────┐
  │       knowledge/**        │   your markdown & PDF docs,
  │    official/ · draft/     │   optional YAML front matter
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
  │   FastMCP server — read tools               │         │
  │                                             │  2. em- │
  │   rag_search          search_official       │  beds   │
  │   search_draft        rag_collections       │◄────────┘
  │   rag_health          rag_list_documents    │  the
  │   (+ optional step 3: cross-encoder rerank  │  query
  │     via a Cohere/Jina-compatible /rerank)   │
  │   (+ opt-in write tools, draft/ only:       │
  │     rag_add_document · rag_delete_document) │
  └──▲──────────────────────────────────────────┘
     │
     │  MCP streamable-http
     │
  ┌──┴───────────────────┐
  │      MCP clients     │
  │  Claude Code ·       │
  │  Claude Desktop ·    │
  │  your own agents     │
  └──────────────────────┘
```

- **쓰기 경로:** `rag-ingest`가 문서 디렉터리(`official/`, `draft/`)의 문서를 읽어 청크로 나누고, 임베딩 엔드포인트로
  벡터를 만들어 Qdrant에 저장합니다. BM25 희소 벡터는 rag-mcp가 직접(FastEmbed) 계산합니다.
- **읽기 경로:** MCP 클라이언트가 검색 도구를 호출하면, rag-mcp가 **같은 임베딩 엔드포인트**로 질의를
  벡터로 바꾸고 Qdrant에서 하이브리드 검색한 뒤 (선택적으로 리랭킹해) 결과를 돌려줍니다.
- **(선택) MCP 쓰기 도구:** 설정으로 켜면 모델이 `draft/` 아래에 초안 문서를 추가·삭제할 수 있습니다.
  정식 문서로 올리는 것은 사람이 `rag-promote` 명령으로 합니다.

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

`rag-ingest`·`rag-promote` 명령은 `~/.local/bin`에 설치됩니다. 이 경로가 PATH에 없으면 설치 스크립트가 알려
줍니다.

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

> 최초 시작 시 FastEmbed가 BM25 모델을 `huggingface.co`에서 한 번 내려받아 `fastembed_cache`에 저장합니다.
> 프록시가 필요하면 설정 파일에 `HTTPS_PROXY`를 넣으세요. 내려받지 못하면 키워드(BM25) 검색 없이
> 의미 검색만으로 동작합니다(로그에 `FastEmbed unavailable` 경고, 시작 로그에 `hybrid=False`).

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
| `~/.local/share/rag-mcp/data/knowledge` | [문서 디렉터리](#문서-디렉터리): `official/`(정식 문서, 샘플 포함), `draft/`(모델이 만든 초안) |
| `~/.local/share/rag-mcp/data/qdrant` | Qdrant 데이터 |
| `~/.local/share/rag-mcp/data/fastembed_cache` | BM25 모델 캐시 (서버와 `rag-ingest`가 함께 씀) |
| `~/.config/systemd/user/{qdrant,rag-mcp}.service` | systemd 사용자 서비스 ([`deploy/systemd/`](deploy/systemd), 경로는 `%h`) |
| `~/.local/bin/rag-ingest` | 문서 수집 명령 |
| `~/.local/bin/rag-promote` | 초안(`draft/`) 승격 명령 |

### 운영 명령

| 작업 | 명령 |
|------|------|
| 상태 | `systemctl --user status qdrant rag-mcp` |
| 로그 | `journalctl --user -u rag-mcp -f` |
| 설정 적용 | `systemctl --user restart rag-mcp` |
| 문서 수집 | `rag-ingest [--recreate]` |
| 초안 목록 / 승격 | `rag-promote` / `rag-promote <source> [--overwrite]` |
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
- `XDG_CONFIG_HOME`을 기본값이 아닌 곳으로 바꾼 환경은 지원하지 않습니다.

### 설치 구조의 설계

- **사용자 권한으로만 설치합니다.** 전용 시스템 사용자나 `/opt`, `/etc` 같은 시스템 경로를 쓰지 않으므로
  관리자 권한 없이 설치·업그레이드·제거할 수 있습니다. 설치 스크립트는 root로 실행하면 멈춥니다.
- **Qdrant는 `127.0.0.1`에만 바인드합니다.** rag-mcp만 접속하면 되므로 외부에 열 이유가 없습니다.
- **Qdrant API 키는 값이 있을 때만 넘깁니다.** Qdrant는 빈 키도 "키가 설정됨"으로 보고 모든 요청을
  거부하기 때문입니다.
- **systemd 샌드박스 옵션(`ProtectSystem` 등)은 쓰지 않습니다.** 사용자 systemd에서는 배포판에 따라 이
  옵션들이 동작하지 않거나 서비스 시작을 막을 수 있습니다. 대신 데이터와 설정 디렉터리를 본인만 접근할 수
  있게(`700`/`600`) 만듭니다.
- **`rag-ingest`·`rag-promote`는 깨끗한 환경에서 실행합니다.** 호출한 셸의 환경 변수를 넘기지 않고 설정
  파일만으로 환경을 만들어, 서비스와 같은 조건으로 색인합니다.

## MCP 클라이언트 연결

서버는 `http://<서버 주소>:8084/mcp`에서 **streamable-http**로 통신합니다. 다른 서버에서 접속한다면
방화벽에서 8084 포트를 여세요. MCP 경로에는 인증이 없으므로 신뢰할 수 있는 네트워크에서만 여세요.

### Claude Code 플러그인 (권장)

이 저장소는 Claude Code 플러그인 마켓플레이스입니다. `rag-mcp` 플러그인을 설치하면 MCP 서버 연결과
함께, 다음 스킬이 추가됩니다. 플러그인에는 서버 코드가 들어 있지 않습니다.

| 스킬 | 내용 |
|------|------|
| `rag-knowledge` | 언제 어떤 검색 도구를 쓸지 안내 |
| `rag-list-documents` | 정식 문서·초안 목록 보기 (`rag_list_documents`) |
| `rag-add-document` | 초안 문서 추가 (`rag_add_document`, 쓰기 도구를 켰을 때) |
| `rag-delete-document` | 초안 문서 삭제 (`rag_delete_document`, 쓰기 도구를 켰을 때) |

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

플러그인은 서버에 연결만 하므로, rag-mcp 서버는 [빠른 시작](#빠른-시작)대로 따로 설치해 두어야 합니다.

### Claude Code 직접 설정

플러그인 없이 프로젝트의 `.mcp.json`에 직접 등록할 수도 있습니다.

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

### 클라이언트 쪽 서버 주소 (`RAG_MCP_URL`)

서버가 열리는 포트(`MCP_PORT`)는 서버 설정 파일에서 정하지만, 클라이언트가 **어느 주소로 접속할지**는
클라이언트 쪽에서 정합니다. 플러그인과 위의 프로젝트 `.mcp.json` 예시는 모두 환경 변수 `RAG_MCP_URL`을
먼저 보고, 없을 때 기본 주소를 씁니다.

```bash
RAG_MCP_URL=http://10.0.0.5:8084/mcp claude
```

| 연결 방식 | 주소 결정 | 기본 주소 |
|------|------|------|
| 플러그인 (`plugins/rag-mcp/.mcp.json`) | `${RAG_MCP_URL:-${user_config.server_url}}` | 플러그인 설정 `server_url` (`/plugin configure`) |
| 프로젝트 `.mcp.json` | `${RAG_MCP_URL:-http://localhost:8084/mcp}` | `http://localhost:8084/mcp` |

- **왜 환경 변수인가:** 설정 파일이나 플러그인 설정을 고치지 않고도 셸·서버·CI마다 다른 rag-mcp 서버에
  접속할 수 있습니다.
- **언제 읽히나:** Claude Code가 시작할 때 MCP 서버 설정을 만들면서 읽습니다. 값을 바꿨다면 Claude Code를
  다시 시작하세요.
- **서버는 이 변수를 읽지 않습니다.** 서버의 바인드 주소와 포트는 `MCP_HOST` / `MCP_PORT`([설정](#설정))로
  정하며, 둘을 바꾸면 클라이언트의 `RAG_MCP_URL`이나 `server_url`도 맞춰야 합니다.

## MCP 도구

| 도구 | 용도 |
|------|---------|
| `rag_search(query, doc_type?, cluster?, component?, limit?)` | 지식 베이스 전체에 대한 시맨틱 검색 (`doc_type`: `official` 또는 `draft`) |
| `search_official(query, cluster?, component?, limit?)` | 정식 문서(`official/`)만 검색 — `doc_type=official`로 고정 |
| `search_draft(query, cluster?, component?, limit?)` | 초안(`draft/`)만 검색 — `doc_type=draft`로 고정 |
| `rag_collections()` | 컬렉션 목록과 포인트 수 (지식 베이스가 채워졌는지 확인) |
| `rag_health()` | Qdrant와 임베딩 엔드포인트 접근 가능 여부, 리랭커·하이브리드 설정 |
| `rag_list_documents(folder?, subdir?, limit?)` | 문서 파일 목록 — [문서 목록 보기](#문서-목록-보기-rag_list_documents) |
| `rag_add_document(title, content, tags?, component?, cluster?, overwrite?)` | (선택) `draft/`에 문서 추가 — [쓰기 도구와 초안 승격](#쓰기-도구와-초안-승격) |
| `rag_delete_document(source)` | (선택) `draft/` 문서 삭제 — 파일과 청크를 함께 삭제 |

검색 도구의 `limit`은 1부터 `RAG_MAX_LIMIT`(기본 20) 사이로 제한되며, 생략하면 `RAG_DEFAULT_LIMIT`(기본 5)입니다.
결과의 `source`가 `official/`로 시작하면 정식 문서, `draft/`로 시작하면 아직 검토하지 않은 초안입니다.

### 정식 문서·초안 검색 (`search_official`, `search_draft`)

세 검색 도구는 같은 검색 경로를 쓰고, 어느 폴더의 문서를 대상으로 하는지만 다릅니다. Claude Code에서는 자연어로 물으면
플러그인의 `rag-knowledge` 스킬이 알맞은 도구를 고릅니다(검토된 정식 문서부터 찾고, 없으면 초안을 봅니다).

| 요청 예 | 도구 호출 | 검색 대상 |
|------|------|------|
| "Longhorn 볼륨이 attaching에서 멈췄을 때 어떻게 해?" | `search_official(query="Longhorn 볼륨이 attaching 상태에서 멈춤")` | `official/`만 |
| "longhorn 관련 정식 문서만 찾아 줘" | `search_official(query="볼륨 복구", component="longhorn")` | `official/` 중 `component: longhorn` |
| "최근에 추가된 초안 중에 볼륨 복구 내용 있어?" | `search_draft(query="볼륨 복구 절차")` | `draft/`만 |
| "prod-01 클러스터 기준으로 초안까지 다 찾아 줘" | `rag_search(query="볼륨 복구", cluster="prod-01")` | 둘 다 (`cluster`는 소프트 필터) |
| — | `rag_search(query="볼륨 복구", doc_type="draft")` | `search_draft`와 같음 |

응답 예 (`search_draft(query="볼륨 복구 절차", limit=1)`):

```json
{
  "status": "ok",
  "collection": "rag_kb",
  "query": "볼륨 복구 절차",
  "doc_type": "draft",
  "cluster": null,
  "cluster_narrowed": null,
  "component": null,
  "reranked": false,
  "count": 1,
  "results": [
    {
      "score": 0.0328,
      "doc_type": "draft",
      "title": "Longhorn 볼륨 복구 절차",
      "source": "draft/longhorn-볼륨-복구.md",
      "tags": ["longhorn"],
      "text": "# 절차\n1. 볼륨을 detach 합니다 ...",
      "truncated": false
    }
  ]
}
```

- `doc_type`은 결과마다 `official` 또는 `draft`이고, `source`의 맨 앞 폴더와 같습니다. 초안을 근거로 답할 때는
  검토 전이라는 점을 밝히세요.
- `score`는 하이브리드 검색에서 RRF 결합 점수라 작은 값으로 나옵니다([쉬운 설명](#쉬운-설명-질의-하나가-처리되는-과정)).
  리랭킹이 켜져 있으면 `reranked: true`와 함께 결과마다 `rerank_score`가 붙습니다.
- `text`는 `RAG_SNIPPET_CHARS`(기본 1200자)까지만 돌려주고, 잘렸으면 `truncated: true`입니다.
- 결과가 없으면 `count: 0`, `results: []`입니다. 정식 문서에서 찾지 못하면 `search_draft`나 표현을 바꾼 질의로
  다시 찾아보세요.

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

### 문서 목록 보기 (`rag_list_documents`)

검색이 "어떤 내용이 있나"에 답한다면, 이 도구는 문서 디렉터리에 "어떤 파일이 있나"에 답합니다. 읽기 전용이라
항상 켜져 있습니다. Claude Code에서는 자연어로 물으면 플러그인의 `rag-list-documents` 스킬이 알맞은 호출을
고릅니다.

| 요청 예 | 도구 호출 | 결과 |
|------|------|------|
| "지식 베이스에 어떤 문서가 있어?" | `rag_list_documents()` | `official/` 전체 |
| "official/team-a 폴더 문서만 보여 줘" | `rag_list_documents(subdir="team-a")` | `official/team-a/` 아래만 |
| "검토할 초안 목록 보여 줘" | `rag_list_documents(folder="draft")` | `draft/` 전체 (항목마다 `promote_to` 포함) |
| "색인 안 된 문서 있어?" | `rag_list_documents()` | `chunks`가 0인 항목 확인 |
| — | `rag_list_documents(limit=500)` | 최대 500개까지 (기본 100) |
| — | `rag_list_documents(folder="team-a")` | 오류 — `folder`는 `official`/`draft`만 |
| — | `rag_list_documents(subdir="../draft")` | 오류 — `folder` 밖을 가리킴 |

응답 예 (`rag_list_documents(folder="draft")`):

```json
{
  "status": "ok",
  "folder": "draft/",
  "total": 1,
  "returned": 1,
  "truncated": false,
  "documents": [
    {
      "source": "draft/longhorn-볼륨-복구.md",
      "title": "Longhorn 볼륨 복구 절차",
      "doc_type": "draft",
      "size_bytes": 1532,
      "modified": "2026-10-07T05:12:40+00:00",
      "chunks": 2,
      "promote_to": "official/longhorn-볼륨-복구.md"
    }
  ]
}
```

| 필드 | 의미 |
|------|------|
| `source`, `title`, `doc_type` | 문서 경로, 제목, 유형 (`doc_type`은 폴더 이름 `official`/`draft`로, 검색 필터 값과 같음) |
| `size_bytes`, `modified` | 파일 크기, 마지막 수정 시각 (UTC) |
| `chunks` | 색인된 청크 수. **0이면 파일은 있지만 아직 `rag-ingest` 전이라 검색되지 않음.** Qdrant에 연결하지 못하면 `null` |
| `promote_to` | (초안만) `rag-promote <source>`로 승격하면 옮겨질 위치 |
| `total`, `returned`, `truncated` | 전체 개수, 돌려준 개수(기본 100, 최대 500), 잘렸는지. `draft/`가 아직 없으면 빈 목록 |

처리 과정과 설계 (`documents.list_documents`):

1. `folder`가 `official`/`draft`인지, `subdir`를 정규화했을 때 그 폴더 안인지 확인합니다.
2. 폴더 아래의 `.md`/`.pdf`를 경로 순으로 모으고, 앞에서부터 `limit`개만 자세히 읽습니다.
3. 마크다운은 front matter에서 `title`을 읽고, `doc_type`은 수집과 같은 규칙(`ingest._doc_type`, 폴더 이름)으로
   정합니다.
4. 돌려줄 `source`들로 Qdrant **facet**(`source` 키, `MatchAny` 필터)을 한 번 호출해 문서별 청크 수를 셉니다.

- **목록은 파일에서, 색인 상태는 Qdrant에서 가져옵니다.** 원본은 문서 디렉터리이므로 파일을 기준으로 해야 아직
  색인하지 않은 문서(`chunks: 0`)도 보입니다.
- **청크 수는 요청 한 번으로 셉니다.** 문서마다 `count`를 호출하면 목록 길이만큼 요청이 늘어납니다. `source`는
  이미 키워드 인덱스가 있으므로 facet으로 한 번에 셀 수 있습니다.
- **Qdrant가 실패해도 목록은 돌려줍니다.** 그때 `chunks`는 `null`(알 수 없음)이고, 0(색인 전)과 구분됩니다.
- **본문은 돌려주지 않습니다.** 응답을 작게 유지하고, 내용 확인은 검색 도구로 하게 합니다.

## 문서 디렉터리

지식 베이스의 **원본은 문서 디렉터리**입니다. 기본 위치는 `~/.local/share/rag-mcp/data/knowledge`이고, 설정 파일의
`RAG_KNOWLEDGE_DIR`로 바꿀 수 있습니다. Qdrant의 포인트는 이 디렉터리의 파일에서 만든 사본이므로, 언제든
`rag-ingest --recreate`로 다시 만들 수 있습니다. MCP 쓰기 도구로 추가한 문서도 먼저 파일로 저장한 뒤 색인합니다.

### 폴더 구조

```text
knowledge/
├── official/                  정식 문서 — 사람이 관리
│   ├── longhorn-volume-attach.md      → source: official/longhorn-volume-attach.md
│   └── team-a/...                     (선택) 하위 폴더로 나눠도 됨
└── draft/                     초안 — 모델이 MCP 쓰기 도구로 만듦
    └── longhorn-볼륨-복구.md          → source: draft/longhorn-볼륨-복구.md
```

| 폴더 | 쓰는 주체 | 들어오는 경로 | 나가는 경로 | 검색 |
|------|------|------|------|------|
| `official/` | 사람 | 파일 복사 + `rag-ingest`, 또는 `rag-promote` 승격 | 사람이 파일 삭제 후 `--recreate` | `rag-ingest` 후 |
| `draft/` | 모델 (`RAG_MCP_WRITE=true`일 때만) | `rag_add_document` | `rag_delete_document`, 또는 `rag-promote`로 `official/`에 승격 | 저장 즉시 |

- **문서의 흐름은 모델이 `draft/`에 작성 → 사람이 검토 → `rag-promote`로 `official/`에 승격입니다.** 설치
  스크립트가 두 폴더를 만들고, 샘플 문서를 `official/`에 넣어 둡니다.
- **문서는 두 폴더 안에만 두세요.** 문서 디렉터리 바로 아래나 다른 폴더에 둔 문서는 `rag-ingest`가 수집하지 않고,
  건너뛴 개수를 경고로 알려 줍니다(`ingest._discover_files`).
- **두 폴더 모두 검색 대상입니다.** 초안은 저장 즉시 검색되고, 검토 여부는 `source`로 구분합니다.
- **폴더가 곧 문서 유형입니다.** `official/` 아래 문서는 `doc_type=official`, `draft/` 아래 문서는 `doc_type=draft`로
  색인되고, `search_official`·`search_draft`와 `rag_search`의 `doc_type` 필터가 이 값을 씁니다. 그래서 승격은
  `draft/<경로>` → `official/<경로>`로 맨 앞 폴더만 바꾸는 이동이면 되고, 승격하면 유형도 함께 바뀝니다.
- **`source`는 문서 디렉터리 기준 상대 경로입니다.** 포인트 ID(`uuid5(source#chunk)`)와 오래된 청크 정리의 기준이
  되고, 검색 결과에도 그대로 나옵니다.
- **권한 경계는 디렉터리 하나로 판단합니다.** MCP 쓰기 도구는 경로를 정규화한 뒤 `draft/` 안인지만 확인하므로
  (`documents._resolve_source`), `official/`을 비롯한 그 밖의 경로는 모델이 만들거나 지울 수 없습니다.
- **서버가 문서 디렉터리를 읽는 것은 `rag_list_documents`뿐입니다.** 검색은 Qdrant만 씁니다.

### 정식 문서 추가하기

`official/`에 마크다운(`.md`)이나 PDF(`.pdf`)를 넣고 수집 명령을 실행합니다. 서버 재시작은 필요 없습니다.

```bash
cp my-doc.md ~/.local/share/rag-mcp/data/knowledge/official/
rag-ingest                      # 문서 색인
rag-ingest --recreate           # 컬렉션을 지우고 전체 재구축
```

- `rag-ingest`는 서버와 같은 설정 파일을 읽으므로, 서버와 정확히 같은 임베딩 설정을 사용합니다.
- 요약 줄 `done: N file(s), M chunk(s)`가 예상과 맞는지 확인하세요. 문서가 빠졌다면 그 위의 로그에
  이유(두 폴더 밖의 문서, 지원하지 않는 확장자, 빈 파일, 텍스트 없는 PDF)가 나옵니다.
- 정기적으로 수집하려면 cron이나 systemd 타이머에 등록하세요.

```cron
# 매시 정각 (설치한 사용자의 crontab: crontab -e)
0 * * * * $HOME/.local/bin/rag-ingest >> $HOME/.local/share/rag-mcp/rag-ingest.log 2>&1
```

### 문서 형식

- **마크다운** — 선택적인 YAML front matter를 지원합니다.
- **PDF** — 페이지 단위로 텍스트를 추출하고, 각 페이지를 `# [Page N]` 섹션으로 만듭니다. front matter가
  없으므로 제목은 파일 이름에서 가져옵니다. 텍스트를 추출할 수 없는 스캔 PDF는
  건너뜁니다.

```markdown
---
title: Longhorn 볼륨이 attaching 상태에서 멈춤
tags: [longhorn, storage]
component: longhorn   # 선택: 필터용
cluster: prod-eu      # 선택: 필터용
---
# 본문...
```

- **`title`:** 생략하면 파일 이름을 씁니다.
- **문서 유형(`doc_type`):** front matter로 정하지 않습니다. 맨 앞 폴더 이름이 유형이 됩니다
  (`official/…` → `official`, `draft/…` → `draft`, `ingest._doc_type`). 하위 폴더 이름이나 front matter의
  `type`은 유형에 영향을 주지 않습니다.
- **`component`, `cluster`, `severity`:** 검색 필터가 되지만 형식이 정해지지 않은 레이블입니다. 환경, 고객, 제품,
  팀 등 도메인에 맞게 쓰거나 생략해도 됩니다.

## 쓰기 도구와 초안 승격

기본 설정에서 지식 베이스에 문서를 넣는 길은 `official/`과 `rag-ingest`뿐입니다. 여기에 더해, 운영자가 켜면
모델이 대화 중에 초안을 추가·삭제할 수 있고, 사람이 그 초안을 검토해 정식 문서로 올릴 수 있습니다.

### 대화로 문서 추가·삭제하기 (MCP 쓰기 도구)

설정 파일에서 쓰기 도구를 켜면, Claude Code 같은 MCP 클라이언트에서 대화로 문서를 추가하거나 삭제할 수 있습니다.

```bash
# ~/.config/rag-mcp/rag-mcp.env 에 추가한 뒤 재시작
RAG_MCP_WRITE=true
```
```bash
systemctl --user restart rag-mcp
```

그다음 "방금 정리한 내용을 지식 베이스에 추가해 줘"처럼 요청하면 모델이 `rag_add_document`로
문서를 저장하고, "그 초안 지워 줘"처럼 요청하면 `rag_delete_document`로 지웁니다. 플러그인의
`rag-add-document`·`rag-delete-document` 스킬이 저장·삭제 전에 사용자의 확인을 받도록 안내합니다.

> ⚠️ **기본으로 꺼져 있습니다.** 켜면 모델이 대화 중에 지식 베이스에 기록하거나 문서를 지울 수 있어, 잘못된
> 내용이 쌓이거나 필요한 문서가 사라질 수 있습니다. 신뢰하는 사용자와 클라이언트만 접속하는 서버에서만 켜세요.
> 꺼져 있으면 도구가 등록되지 않아 모델에게 보이지도 않습니다.

| 항목 | 동작 |
|------|------|
| 등록 | `RAG_MCP_WRITE=true`일 때만 서버 시작 시 도구를 등록. 꺼져 있으면 도구 목록에도 없음 |
| 쓰기 범위 | 문서 디렉터리의 **`draft/` 아래만** 추가·삭제. 사람이 관리하는 정식 문서(`official/`)는 만들거나 지울 수 없음 |
| 저장 위치 | `<문서 디렉터리>/draft/<제목>.md` (예: `draft/longhorn-볼륨-복구.md`). 폴더가 `draft/`이므로 `doc_type`은 `draft` |
| 파일 내용 | 인자로 받은 `title`, `tags`, `component`, `cluster`를 front matter로 쓰고 그 아래 본문 |
| 색인 | 저장 직후 `ingest.ingest_file`로 색인 — `rag-ingest`와 같은 코드라 청크 ID·페이로드가 같음 |
| 덮어쓰기 | 같은 경로에 파일이 있으면 `overwrite=true`일 때만 바꿈 (청크 수가 줄면 남은 청크도 정리) |
| 크기 제한 | 본문 `RAG_MAX_DOC_CHARS`자(기본 200000)까지 |
| 삭제 | `rag_delete_document(source)` — `draft/` 아래 문서 파일과 그 `source`의 청크를 함께 삭제 |

설계상 선택과 그 이유:

- **파일로 먼저 저장합니다.** 파일로 남겨야 `rag-ingest --recreate`로 재구축해도 추가한 문서가 사라지지 않고,
  사람이 직접 확인·수정·삭제할 수 있습니다.
- **경로는 서버가 정합니다.** 호출자는 제목만 넘기고, 파일 이름은 제목에서 만든 안전한 이름(글자·숫자·`-`)
  입니다. 경로에는 `draft/`와 이 파일 이름만 쓰이므로 `draft/` 밖에 쓸 수 없습니다.
- **색인에 실패해도 파일은 남깁니다.** 응답에 `saved: true`와 오류를 함께 돌려주므로, 원인을 고친 뒤
  `rag-ingest`로 다시 색인하면 됩니다.
- **삭제는 파일과 청크를 함께 지웁니다.** 파일이 이미 없고 청크만 남아 있어도 청크를 정리하므로, 파일을 지우거나
  이름을 바꾼 뒤 남은 청크를 없앨 때도 쓸 수 있습니다.
- **삭제 범위를 제한합니다.** `source`는 문서 디렉터리 기준 상대 경로여야 하고, 정규화했을 때 `draft/` 안이어야
  합니다(절대 경로, `draft/` 밖, `..`로 빠져나가는 경로는 거부). `.md`/`.pdf` 문서만 지울 수 있습니다.
- **청크 삭제에 실패하면 파일은 지우지 않습니다.** 파일만 사라지고 청크가 검색에 남는 상태를 피하기 위해서입니다.

### 초안 승격 (`rag-promote`)

모델이 만든 초안을 사람이 검토한 뒤 정식 문서로 올리는 **사람 전용 명령**입니다. MCP 도구가 아니므로 쓰기 도구를
켜도 모델은 승격할 수 없습니다. 그래서 "모델은 `draft/`에만, 정식 문서는 사람이 검토해서"라는 원칙이 유지됩니다.

```bash
rag-promote                                    # 초안 목록과 옮겨질 위치
rag-promote draft/longhorn-볼륨-복구.md         # → official/longhorn-볼륨-복구.md 로 옮기고 색인
rag-promote --overwrite draft/...              # 정식 위치에 같은 이름의 문서가 있으면 바꾸기
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

## 데이터 모델

Qdrant 컬렉션 하나(기본 이름 `rag_kb`)에 모든 문서를 저장합니다.

| 벡터 | 종류 | 내용 |
|------|------|------|
| `dense` | 밀집 벡터, 코사인 거리, HNSW 인덱스 | 임베딩 모델이 만든 의미 벡터 (bge-m3는 1024차원) |
| `bm25` | 희소 벡터, IDF 적용 | FastEmbed `Qdrant/bm25`로 만든 키워드 벡터 (하이브리드가 켜져 있을 때만) |

- 벡터 차원은 수집할 때 실제 임베딩 길이로 정해지므로, 모델을 바꿔도 코드를 고칠 필요가 없습니다.
- IDF는 컬렉션의 `Modifier.IDF`로 서버 측에서 계산하므로, 질의 쪽은 단어 존재 여부만 보내면 됩니다.
- **포인트(청크):** ID는 `uuid5(source#chunk)`이고, 페이로드는 `text`, `doc_type`, `title`, `source`, `tags`, `chunk`,
  그리고 front matter의 `component`/`severity`/`cluster`입니다. ID가 문서 경로와 청크 번호에서 결정되므로, 같은
  문서를 다시 수집하면 중복이 생기지 않고 기존 포인트를 덮어씁니다.
- **페이로드 인덱스:** 필터링을 빠르게 하려고 `doc_type`, `component`, `cluster`, `source`에 키워드 인덱스를
  만듭니다(`source`는 오래된 청크 정리와 문서 목록의 청크 수 집계용).

## 수집 과정

### 청킹

1. 마크다운 헤딩 기준으로 섹션을 나눕니다. 절차의 한 단계처럼 문서의 한 섹션이 한 덩어리로 남습니다.
2. 각 청크 앞에 섹션 헤딩을 붙여, 청크 하나만 검색돼도 맥락을 알 수 있게 합니다.
3. 섹션이 `CHUNK_SIZE`(기본 1500자)보다 크면 문단 단위로 나누고, 청크 사이에 `CHUNK_OVERLAP`(기본
   100자)만큼 겹치게 합니다. 너무 큰 문단은 강제로 자릅니다.

### 멱등성과 오래된 청크

- **다시 실행해도 안전합니다.** 포인트 ID가 `(문서 경로, 청크 번호)`에서 정해지므로 기존 포인트를
  덮어씁니다.
- **문서가 줄어든 경우:** 예를 들어 30개였던 청크가 20개로 줄면, 그대로 두었을 때 20~29번 청크가 오래된
  검색 결과로 남습니다. 그래서 업서트 직후 현재 개수 이상의 청크를 삭제합니다.
- **문서를 삭제하거나 이름을 바꾼 경우:** 처리하지 않습니다. 수집할 파일이 없으니 그 청크가 고아가
  된 것을 알 수 없기 때문입니다. `--recreate`로 전체를 다시 만드세요.

### 배치 처리

큰 문서(예: 100페이지 PDF)는 청크 수백 개가 되고, 각 청크가 밀집 벡터·희소 벡터·텍스트를 담고 있어
한 번에 보내면 수 메가바이트가 됩니다. 그래서 요청을 나눕니다.

| 단계 | 요청당 크기 | 설정 |
|------|------|------|
| 임베딩 | 청크 32개 | `EMBED_BATCH_SIZE` |
| Qdrant 업서트 | 포인트 64개 | `QDRANT_UPSERT_BATCH` |

나눠 보내면 큰 파일을 처리하다 실패해도 앞선 배치는 이미 반영되어 있습니다. 엔드포인트가 413/400
오류를 돌려주면 `EMBED_BATCH_SIZE`를 줄이세요(엔드포인트마다 요청당 입력 개수와 토큰 상한이 다릅니다).

## 검색 파이프라인

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

## 임베딩 엔드포인트 설정

임베딩은 OpenAI 호환 엔드포인트로 처리합니다. `EMBEDDINGS_BASE_URL`의 `/v1/embeddings`(URL이 `/v1`로 끝나면
`/embeddings`)에 배치 `input` 배열로 요청합니다.

**직접 띄운 서버 (오프라인, 권장)** — 예: 같은 서버의 TEI로 `BAAI/bge-m3` 서빙. vLLM, LocalAI 등도
같은 방식입니다.

```bash
# ~/.config/rag-mcp/rag-mcp.env
EMBEDDINGS_BASE_URL=http://localhost:8080
# 인증이 없으면 비워 둠
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

- **`EMBEDDINGS_BASE_URL`에는 기본값이 없습니다.** 설정을 빠뜨렸을 때 문서가 의도치 않게 외부 API로
  전송되지 않도록, 운영자가 엔드포인트를 명시해야 합니다. 비어 있으면 검색과 수집이 오류로 알려 줍니다.
- `EMBEDDINGS_API_KEY`가 있으면 `Authorization: Bearer` 헤더로 보내고, 비어 있으면 보내지 않습니다.
- 응답은 `index`로 다시 정렬하므로, 엔드포인트가 순서를 바꿔 돌려줘도 청크와 벡터가 어긋나지 않습니다.
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
> 의미가 달라 비교할 수 없고, 섞어 쓰면 아무 경고 없이 검색이 망가집니다. 엔드포인트나 모델을 바꾼 뒤에는
> 서버를 재시작하고 컬렉션을 다시 만드세요.
>
> ⚠️ **하이브리드를 켜고 끄면 컬렉션 스키마가 바뀝니다.** `RAG_HYBRID`를 바꾼 뒤에도 같은 방법으로
> 재구축해야 합니다.
>
> ```bash
> systemctl --user restart rag-mcp
> rag-ingest --recreate
> ```

### 한국어 / 다국어 문서

기본 임베딩 모델 이름은 한국어를 포함한 다국어 모델 **`bge-m3`**(1024차원, 최대 입력 8192 토큰)입니다.
영어 위주로 학습된 `nomic-embed-text`(768차원)보다 한국어 질의·문서의 의미 검색이 정확합니다. 호스팅
API라면 OpenAI `text-embedding-3-small`/`-large`도 다국어를 지원합니다. 영어 문서만 쓰고 더 가벼운
모델을 원하면 엔드포인트에서 `nomic-embed-text`를 서빙하고 `EMBEDDINGS_MODEL`만 바꾸면 됩니다(nomic 계열은
작업 접두사가 자동으로 붙습니다).

**TEI로 bge-m3 서빙하기 (오프라인, 권장):**
[Hugging Face TEI](https://github.com/huggingface/text-embeddings-inference)(Text Embeddings Inference)는
`BAAI/bge-m3`를 OpenAI 호환 `/v1/embeddings`로 서빙합니다. 설치 방법은 TEI 문서를 따르세요(CPU/GPU 빌드
제공). 같은 서버의 8080 포트에서 띄웠다면 위의 "직접 띄운 서버" 설정 그대로 쓰면 됩니다.

**적용 및 확인:**

```bash
systemctl --user restart rag-mcp            # 새 모델로 질의하도록 재시작
rag-ingest --recreate                       # 컬렉션 재생성 + 재수집

# 컬렉션 차원 확인 → "size":1024 이면 성공
curl -s http://localhost:6333/collections/rag_kb | grep -o '"size":[0-9]*'
```

그다음 `rag_health()`에서 모델이 `bge-m3`로 표시되는지 확인하고, 한국어 질의로 `rag_search`를 실행해
보세요.

**참고 사항:**

- **BM25는 한국어에 약합니다.** `Qdrant/bm25`는 영어 기준으로 단어를 나누므로 조사가 붙은 형태("볼륨이",
  "볼륨을")를 서로 다른 단어로 봅니다. RRF 결합에서 의미 검색이 상당 부분 보완하며, 영어 토큰(에러 문자열,
  리소스 이름)은 잘 찾습니다. 결과가 이상하면 `RAG_HYBRID=false`(밀집 전용)와 비교해 보세요.
- **리랭커도 다국어 모델이 기본값입니다.** 리랭킹을 켜면(`RERANK_PROVIDER=cohere`) 기본 모델은 Cohere
  `rerank-multilingual-v3.0`입니다. Jina라면 `jina-reranker-v2-base-multilingual`을 지정하세요. 영어 전용
  `rerank-english-v3.0`은 한국어 문서에 쓰지 마세요.
- **속도.** bge-m3(약 1.2GB)는 CPU로 서빙하면 수집이 느릴 수 있습니다. 문서가 많다면 GPU에서 서빙하거나
  `EMBED_BATCH_SIZE`를 엔드포인트가 허용하는 범위에서 늘리세요.
- **청크 크기.** 기본 `CHUNK_SIZE=1500`자는 bge-m3의 최대 입력 길이보다 훨씬 작아 그대로 써도 됩니다.

## 설정

모든 서버 설정은 설정 파일(`~/.config/rag-mcp/rag-mcp.env`)의 환경 변수로 합니다. 주석이 달린 예시는
[.env.example](.env.example)에 있습니다. 아래 표는 모두 **서버**가 읽는 변수입니다. 클라이언트(Claude Code)가
읽는 `RAG_MCP_URL`은 [클라이언트 쪽 서버 주소](#클라이언트-쪽-서버-주소-rag_mcp_url)를 참고하세요.

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

> 설정 파일에서는 `KEY=value  # 주석`처럼 같은 줄 끝에 주석을 달지 마세요. systemd의 `EnvironmentFile`은
> 주석까지 값으로 읽습니다. 주석은 항상 별도 줄에 쓰세요.

## 알려진 제한

- 문서를 삭제하거나 이름을 바꾸면 이전 청크가 남습니다 → `rag-ingest --recreate`로 재구축하세요(`draft/` 문서라면
  `rag_delete_document`로도 정리됩니다).
- BM25 키워드 검색은 한국어 형태소를 고려하지 않습니다.
- 스캔(이미지) PDF는 OCR을 하지 않으므로 수집되지 않습니다.
- 하이브리드 검색의 `score`는 RRF 결합 점수라서, 코사인 유사도처럼 임계값을 정하는 데 쓸 수 없습니다.
- 같은 서버에서는 rag-mcp를 하나만 실행할 수 있습니다. Qdrant 포트(6333, 6334)가 systemd 유닛에 고정되어
  있기 때문입니다.

## 저장소 구조

| 경로 | 내용 |
|------|------|
| [`tools/server.py`](tools/server.py) | FastMCP 서버. 읽기 도구 6개(검색 3개, 상태 확인 2개, 문서 목록)와 선택적 쓰기 도구 2개를 제공 |
| [`tools/ingest.py`](tools/ingest.py) | 마크다운/PDF 문서를 읽어 청크로 나누고 임베딩해 Qdrant에 업서트 (`rag-ingest`) |
| [`tools/documents.py`](tools/documents.py) | 문서 디렉터리 로직: 문서 목록, MCP 쓰기 도구의 추가·삭제, `rag-promote`의 목록·승격 |
| [`tools/promote.py`](tools/promote.py) | 초안 승격 명령 `rag-promote` (사람 전용, MCP 도구 아님) |
| [`tools/embeddings.py`](tools/embeddings.py) | OpenAI 호환 임베딩 호출, 비대칭 모델 접두사 처리 |
| [`tools/vectorstore.py`](tools/vectorstore.py) | Qdrant 컬렉션 스키마, BM25 희소 벡터(FastEmbed), 하이브리드 질의 |
| [`tools/reranker.py`](tools/reranker.py) | Cohere/Jina 호환 크로스 인코더 리랭킹 (선택 사항) |
| `requirements.txt` | Python 의존성 |
| `deploy/` | 설치·제거 스크립트, systemd 유닛, `rag-ingest`·`rag-promote` 명령 템플릿 |
| `knowledge/official/` | 샘플 문서 |
| `.claude-plugin/marketplace.json` | Claude Code 플러그인 마켓플레이스 정의 |
| `plugins/rag-mcp/` | Claude Code 플러그인 (MCP 서버 연결 설정, `rag-knowledge`·`rag-list-documents`·`rag-add-document`·`rag-delete-document` 스킬) |
| `.claude/skills/repo-check/` | 저장소 점검 스킬 (Claude Code에서 `/repo-check`, 직접 실행: `bash .claude/skills/repo-check/scripts/check.sh`) |
| `tests/` | 테스트 |

`ingest.py`, `documents.py`, `server.py`는 모두 `vectorstore.py`와 `embeddings.py`를 거칩니다. 그래서
쓰기 경로와 읽기 경로 사이에서 컬렉션 스키마, 벡터 이름, 임베딩 설정이 어긋나지 않습니다.

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
