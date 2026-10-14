# rag-mcp

**AI 어시스턴트용 RAG 메모리 서버**입니다. 마크다운 문서를 색인해 두고, [Model Context Protocol](https://modelcontextprotocol.io)(MCP)로
검색 도구를 제공합니다. Claude Code, Claude Desktop 등 MCP를 지원하는 클라이언트라면
어디서든 "이 작업은 어떻게 하지?", "관련 문서에는 뭐라고 되어 있지?" 같은 질문에 여러분의 문서를 근거로
답하게 할 수 있습니다. AI 에이전트는 작업하면서 필요할 때마다 관련 문서를 직접 찾아 씁니다(에이전틱 검색,
agentic retrieval).

## 특징

- **읽기 전용** — 모델은 *검색*만 할 수 있습니다. 지식 베이스 기록은 사람이 수집 명령(`rag-ingest`)으로
  합니다.
- **하이브리드 검색** — 의미 기반 밀집(dense) 벡터와 BM25 키워드 희소(sparse) 벡터를 Reciprocal Rank
  Fusion(RRF)으로 결합합니다. `CrashLoopBackOff` 같은 에러 문자열이나 리소스 이름도 정확히 찾습니다.
- **선택적 리랭킹** — Cohere/Jina 호환 크로스 인코더로 결과 순서를 다시 매겨 정밀도를 높입니다.
- **사내 임베딩 서버 연결** — 사내에서 운영 중인 OpenAI 호환 `/v1/embeddings` 서버에 연결합니다. 임베딩 모델을
  rag-mcp가 직접 실행하지 않으므로 모델 파일을 준비할 필요가 없습니다.
- **한국어 지원** — 기본 임베딩 모델은 다국어 모델 `bge-m3`, 키워드 검색(BM25)은 한국어 형태소 분석기 Kiwi로
  조사·어미를 떼어 단어를 맞추고, 기본 리랭커도 다국어 모델입니다.
- **마크다운 수집** — YAML front matter, 헤딩 기준 청킹, 멱등(idempotent) 재실행을 지원합니다.
- **간단한 설치** — Linux + systemd 서버에 스크립트 하나로 설치합니다. root 권한이 필요 없습니다.
- **Claude Code 플러그인** — 이 저장소 자체가 플러그인 마켓플레이스입니다.

### 설계 원칙

| 원칙 | 내용 |
|------|------|
| **읽기 전용** | MCP 도구는 검색만 합니다. 모델은 지식 베이스를 바꿀 수 없고, 기록은 사람이 수집 명령(`rag-ingest`)으로 합니다. 서버는 문서 디렉터리를 읽거나 쓰지 않습니다. |
| **벤더 중립** | 채팅 LLM은 연결하는 MCP 클라이언트가 정합니다. 임베딩은 사내 서버의 OpenAI 호환 `/v1/embeddings`로 요청하므로 특정 모델이나 제품에 묶이지 않습니다. |
| **수집과 질의의 일관성** | 수집과 질의가 같은 임베딩 코드와 설정을 공유하도록 만들어, 벡터가 어긋날 여지를 없앴습니다. |
| **실패해도 검색은 유지** | 리랭킹, BM25, 오래된 청크 정리 같은 부가 기능은 실패하면 조용히 건너뛰고, 기본 검색은 계속 동작합니다(최선형, best-effort). |

## 구성 요소

| 구성 요소 | 역할 |
|------|------|
| **rag-mcp 서버** (`tools/server.py`) | MCP 도구를 `http://<서버>:8084/mcp`(streamable-http)로 제공 |
| **Qdrant** | 문서 청크의 벡터를 저장하는 벡터 DB (`127.0.0.1:6333`) |
| **rag-ingest** (`tools/ingest.py`) | 문서를 청크로 나누고 임베딩해 Qdrant에 기록하는 수집 명령 |
| **사내 임베딩 서버** | 텍스트를 벡터로 바꾸는 OpenAI 호환 서버 — **rag-mcp에 포함되지 않음, 사내 서버에 연결** |
| **Claude Code 플러그인** (`plugins/rag-mcp`) | Claude Code에서 rag-mcp 서버에 연결하고, 도구 사용법을 스킬로 안내 |

## 아키텍처

```
                       WRITE PATH — populate the KB
  ┌───────────────────────────┐
  │       knowledge/**        │   your markdown docs,
  │         official/         │   optional YAML front matter
  └─────────────┬─────────────┘
                │
                ▼
  ┌───────────────────────────┐   embed    ┌─────────────────────────────┐
  │        rag-ingest         │  chunks    │  in-house embedding server  │
  │  · parses front matter    │───────────►│      OpenAI-compatible      │
  │  · heading-aware chunks   │            │        /v1/embeddings       │
  │  · Kiwi BM25 vectors      │◄───────────│    (not part of rag-mcp)    │
  │  · idempotent upserts,    │  vectors   └──────────────▲──────────────┘
  │    stale-tail cleanup     │                           │
  └─────────────┬─────────────┘                           │  the SAME
                │ upsert                                  │  server also
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
  │   FastMCP server — read-only tools          │         │
  │                                             │  2. em- │
  │   rag_search          rag_collections       │  beds   │
  │   rag_health                                │◄────────┘
  │                                             │  the
  │   (+ optional step 3: cross-encoder rerank  │  query
  │     via a Cohere/Jina-compatible /rerank)   │
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

- **쓰기 경로:** `rag-ingest`가 문서 디렉터리(`official/`)의 문서를 읽어 청크로 나누고, 사내 임베딩 서버로
  벡터를 만들어 Qdrant에 저장합니다. BM25 희소 벡터는 rag-mcp가 직접(Kiwi 형태소 분석) 계산합니다.
- **읽기 경로:** MCP 클라이언트가 검색 도구를 호출하면, rag-mcp가 **같은 사내 임베딩 서버**로 질의를
  벡터로 바꾸고 Qdrant에서 하이브리드 검색한 뒤 (선택적으로 리랭킹해) 결과를 돌려줍니다.
- **쓰기 경로는 사람만:** MCP 도구는 모두 읽기 전용입니다. 모델은 지식 베이스를 바꿀 수 없습니다.

## 빠른 시작

### 사전 요구 사항

- systemd를 쓰는 Linux 서버 (Ubuntu, Debian, RHEL 계열 등)
- Python 3.10 이상과 venv 모듈 (Debian/Ubuntu: `apt install python3-venv`)
- `curl`, `tar`
- **사내 임베딩 서버** — OpenAI 호환 `/v1/embeddings`를 제공하는 서버의 주소, 인증 정보, 모델 이름
  ([사내 임베딩 서버 연결](#사내-임베딩-서버-연결) 참고)

### 1. 설치

서비스를 실행할 **일반 사용자**로 설치합니다. root(sudo)는 필요 없습니다.

```bash
git clone https://github.com/chorus96/rag-mcp.git && cd rag-mcp
./deploy/install.sh             # Qdrant + rag-mcp 설치, systemd 사용자 서비스로 시작
```

`rag-ingest` 명령은 `~/.local/bin`에 설치됩니다. 이 경로가 PATH에 없으면 설치 스크립트가 알려
줍니다.

### 2. 사내 임베딩 서버 지정

설정 파일에서 사내 임베딩 서버를 지정하고 서버를 재시작합니다.

```bash
vi ~/.config/rag-mcp/rag-mcp.env
#   EMBEDDINGS_BASE_URL=http://embedding.internal.example:8080   (사내 임베딩 서버)
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

> 키워드(BM25) 검색에 쓰는 한국어 형태소 분석기 Kiwi는 설치 때 `pip`로 함께 설치되며(약 100MB), 실행 중에
> 따로 내려받는 것이 없습니다. 시작 로그의 `hybrid=True`로 하이브리드 검색이 켜졌는지 확인할 수 있습니다.
> Kiwi를 불러오지 못하면 의미 검색만으로 동작합니다(로그에 `sparse model ... unavailable` 경고,
> 시작 로그에 `hybrid=False`).

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
| `~/.local/share/rag-mcp/data/knowledge` | [문서 디렉터리](#문서-디렉터리): `official/`(사람이 관리하는 문서, 샘플 포함) |
| `~/.local/share/rag-mcp/data/qdrant` | Qdrant 데이터 |
| `~/.config/systemd/user/{qdrant,rag-mcp}.service` | systemd 사용자 서비스 ([`deploy/systemd/`](deploy/systemd), 경로는 `%h`) |
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
- **`rag-ingest`는 깨끗한 환경에서 실행합니다.** 호출한 셸의 환경 변수를 넘기지 않고 설정
  파일만으로 환경을 만들어, 서비스와 같은 조건으로 색인합니다.

## MCP 클라이언트 연결

서버는 `http://<서버 주소>:8084/mcp`에서 **streamable-http**로 통신합니다. 다른 서버에서 접속한다면
방화벽에서 8084 포트를 여세요. MCP 경로에는 인증이 없으므로 신뢰할 수 있는 네트워크에서만 여세요.

### Claude Code 플러그인 (권장)

이 저장소는 Claude Code 플러그인 마켓플레이스입니다. `rag-mcp` 플러그인을 설치하면 MCP 서버 연결과
함께, 다음 스킬이 추가됩니다. 플러그인에는 서버 코드가 들어 있지 않습니다.

| 스킬 | 내용 |
|------|------|
| `rag-knowledge` | 언제 어떤 검색 도구를 쓸지, 결과를 어떻게 인용할지 안내 |

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
| `rag_search(query, cluster?, component?, limit?)` | 지식 베이스에 대한 시맨틱 검색 (하이브리드, 선택적 리랭킹) |
| `rag_collections()` | 컬렉션 목록과 포인트 수 (지식 베이스가 채워졌는지 확인) |
| `rag_health()` | Qdrant와 사내 임베딩 서버 접근 가능 여부, 리랭커·하이브리드 설정 |

도구는 모두 **읽기 전용**입니다. 지식 베이스를 바꾸는 도구는 없습니다.

검색 도구의 `limit`은 1부터 `RAG_MAX_LIMIT`(기본 20) 사이로 제한되며, 생략하면 `RAG_DEFAULT_LIMIT`(기본 5)입니다.
결과의 `source`는 원본 문서의 경로(예: `official/longhorn-volume-attach.md`)입니다.

### 검색 사용 예

Claude Code에서는 자연어로 물으면 플러그인의 `rag-knowledge` 스킬이 알맞은 호출을 고릅니다.

| 요청 예 | 도구 호출 | 검색 대상 |
|------|------|------|
| "Longhorn 볼륨이 attaching에서 멈췄을 때 어떻게 해?" | `rag_search(query="Longhorn 볼륨이 attaching 상태에서 멈춤")` | 전체 |
| "longhorn 관련 문서만 찾아 줘" | `rag_search(query="볼륨 복구", component="longhorn")` | `component: longhorn`인 문서 |
| "prod-01 클러스터 기준으로 찾아 줘" | `rag_search(query="볼륨 복구", cluster="prod-01")` | `cluster`는 소프트 필터 |

응답 예 (`rag_search(query="볼륨이 attaching 에서 멈춤", limit=1)`):

```json
{
  "status": "ok",
  "collection": "rag_kb",
  "query": "볼륨이 attaching 에서 멈춤",
  "cluster": null,
  "cluster_narrowed": null,
  "component": null,
  "reranked": false,
  "count": 1,
  "results": [
    {
      "score": 0.0328,
      "title": "Longhorn 볼륨이 attaching 상태에서 멈춤",
      "source": "official/longhorn-volume-attach.md",
      "tags": ["longhorn", "storage"],
      "text": "# 증상\n노드 재부팅 후 ...",
      "truncated": false
    }
  ]
}
```

- `score`는 하이브리드 검색에서 RRF 결합 점수라 작은 값으로 나옵니다([쉬운 설명](#쉬운-설명-질의-하나가-처리되는-과정)).
  리랭킹이 켜져 있으면 `reranked: true`와 함께 결과마다 `rerank_score`가 붙습니다.
- `text`는 `RAG_SNIPPET_CHARS`(기본 1200자)까지만 돌려주고, 잘렸으면 `truncated: true`입니다.
- 결과가 없으면 `count: 0`, `results: []`입니다. 표현을 바꾼 질의로 다시 찾아보세요.

### 필터의 의미

| 필터 | 종류 | 결과가 비었을 때 |
|------|------|------|
| `component` | 하드 | 그대로 빈 결과 |
| `cluster` | **소프트** | 클러스터 조건만 빼고 전체 범위로 다시 검색 |

`cluster`를 소프트 필터로 둔 이유는, 새 클러스터에서 증상이 처음 나타났을 때 다른 클러스터의 선례가
가려지지 않게 하기 위해서입니다. 응답의 `cluster_narrowed`로 어떤 경우인지 알 수 있습니다.

| `cluster_narrowed` | 의미 |
|------|------|
| `None` | 클러스터를 지정하지 않음 |
| `true` | 지정한 클러스터 안에서 결과를 찾음 |
| `false` | 같은 클러스터에는 없어서 전체 범위 결과를 돌려줌 (응답에 `note` 포함) |

## 문서 디렉터리

지식 베이스의 **원본은 문서 디렉터리**입니다. 기본 위치는 `~/.local/share/rag-mcp/data/knowledge`이고, 설정 파일의
`RAG_KNOWLEDGE_DIR`로 바꿀 수 있습니다. Qdrant의 포인트는 이 디렉터리의 파일에서 만든 사본이므로, 언제든
`rag-ingest --recreate`로 다시 만들 수 있습니다. MCP 서버는 이 디렉터리를 읽거나 쓰지 않습니다.

### 폴더 구조

```text
knowledge/
└── official/                  사람이 관리하는 문서
    ├── longhorn-volume-attach.md      → source: official/longhorn-volume-attach.md
    └── team-a/...                     (선택) 하위 폴더로 나눠도 됨
```

- **문서는 `official/` 안에만 두세요.** 문서 디렉터리 바로 아래나 다른 폴더에 둔 문서는 `rag-ingest`가 수집하지
  않고, 건너뛴 개수를 경고로 알려 줍니다(`ingest._discover_files`). 설치 스크립트가 `official/`을 만들고 샘플
  문서를 넣어 둡니다.
- **`source`는 문서 디렉터리 기준 상대 경로입니다.** 포인트 ID(`uuid5(source#chunk)`)와 오래된 청크 정리의 기준이
  되고, 검색 결과에도 그대로 나옵니다.
- **문서를 바꾸는 것은 사람뿐입니다.** 파일을 넣거나 고치거나 지운 뒤 `rag-ingest`(지우거나 이름을 바꿨다면
  `--recreate`)를 실행합니다. MCP 서버는 이 디렉터리를 읽거나 쓰지 않습니다.

### 문서 추가하기

`official/`에 마크다운(`.md`) 파일을 넣고 수집 명령을 실행합니다. 서버 재시작은 필요 없습니다.

```bash
cp my-doc.md ~/.local/share/rag-mcp/data/knowledge/official/
rag-ingest                      # 문서 색인
rag-ingest --recreate           # 컬렉션을 지우고 전체 재구축
```

- `rag-ingest`는 서버와 같은 설정 파일을 읽으므로, 서버와 정확히 같은 임베딩 설정을 사용합니다.
- 요약 줄 `done: N file(s), M chunk(s)`가 예상과 맞는지 확인하세요. 문서가 빠졌다면 그 위의 로그에
  이유(`official/` 밖의 문서, 지원하지 않는 확장자, 빈 파일)가 나옵니다.
- 정기적으로 수집하려면 cron이나 systemd 타이머에 등록하세요.

```cron
# 매시 정각 (설치한 사용자의 crontab: crontab -e)
0 * * * * $HOME/.local/bin/rag-ingest >> $HOME/.local/share/rag-mcp/rag-ingest.log 2>&1
```

### 문서 형식

마크다운(`.md`) 파일만 수집합니다. 선택적인 YAML front matter를 지원합니다. 다른 형식(PDF, Word 등)은
마크다운으로 바꿔서 넣으세요.

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
- **`component`, `cluster`:** `rag_search`의 필터(`component`는 하드, `cluster`는 소프트)가 되는, 형식이 정해지지
  않은 레이블입니다. 환경, 고객, 제품, 팀 등 도메인에 맞게 쓰거나 생략해도 됩니다.

## 데이터 모델

Qdrant 컬렉션 하나(기본 이름 `rag_kb`)에 모든 문서를 저장합니다.

| 벡터 | 종류 | 내용 |
|------|------|------|
| `dense` | 밀집 벡터, 코사인 거리, HNSW 인덱스 | 임베딩 모델이 만든 의미 벡터 (bge-m3는 1024차원) |
| `bm25` | 희소 벡터, IDF 적용 | Kiwi 형태소 분석으로 만든 BM25 키워드 벡터 (하이브리드가 켜져 있을 때만, [키워드 검색과 한국어](#키워드-검색bm25과-한국어)) |

- 벡터 차원은 수집할 때 실제 임베딩 길이로 정해지므로, 모델을 바꿔도 코드를 고칠 필요가 없습니다.
- IDF는 컬렉션의 `Modifier.IDF`로 서버 측에서 계산하므로, 질의 쪽은 단어 존재 여부만 보내면 됩니다.
- **포인트(청크):** ID는 `uuid5(source#chunk)`이고, 페이로드는 `text`, `title`, `source`, `tags`, `chunk`,
  그리고 front matter의 `component`/`cluster`입니다. ID가 문서 경로와 청크 번호에서 결정되므로, 같은
  문서를 다시 수집하면 중복이 생기지 않고 기존 포인트를 덮어씁니다.
- **페이로드 인덱스:** 필터링을 빠르게 하려고 `component`, `cluster`, `source`에 키워드 인덱스를
  만듭니다(`source`는 오래된 청크 정리용).

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

큰 문서(예: 아주 긴 마크다운)는 청크 수백 개가 되고, 각 청크가 밀집 벡터·희소 벡터·텍스트를 담고 있어
한 번에 보내면 수 메가바이트가 됩니다. 그래서 요청을 나눕니다.

| 단계 | 요청당 크기 | 설정 |
|------|------|------|
| 임베딩 | 청크 32개 | `EMBED_BATCH_SIZE` |
| Qdrant 업서트 | 포인트 64개 | `QDRANT_UPSERT_BATCH` |

나눠 보내면 큰 파일을 처리하다 실패해도 앞선 배치는 이미 반영되어 있습니다. 사내 임베딩 서버가 413/400
오류를 돌려주면 `EMBED_BATCH_SIZE`를 줄이세요(서버마다 요청당 입력 개수와 토큰 상한이 다릅니다).

## 검색 파이프라인

`server.py`의 검색 도구는 모두 같은 경로(`_search`)를 거칩니다.

1. **질의 임베딩** — 질의 텍스트를 사내 임베딩 서버로 밀집 벡터로 바꿉니다.
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

### 키워드 검색(BM25)과 한국어

BM25는 질의의 단어가 문서에 얼마나 들어 있는지로 점수를 매기는 키워드 검색 방식입니다. 드문 단어일수록
가중치가 크고(IDF, Qdrant가 컬렉션 전체 기준으로 계산), 같은 단어가 많을수록 점수가 오르되 일정 수준에서
포화됩니다. 그래서 **단어를 어떻게 나누느냐**가 품질을 좌우합니다. 흔한 BM25처럼 공백에서 나누면 한국어는 조사·어미가
붙은 채로 한 단어가 되므로, rag-mcp는 한국어 형태소 분석기 [Kiwi](https://github.com/bab2min/kiwipiepy)로 단어를
나눕니다(`tools/kiwi_bm25.py`).

| 단어 나누기 | "볼륨이 멈췄어요" | 문서의 "…볼륨을 … 멈추면…" |
|------|------|------|
| 공백 기준 (흔한 BM25) | `볼륨이`, `멈췄어요` | `볼륨을`, `멈추면` — 맞는 단어 없음 |
| Kiwi 형태소 (rag-mcp) | `볼륨`, `멈추` | `볼륨`, `멈추` — 둘 다 맞음 |

- **남기는 형태소:** 일반·고유 명사, 수사, 동사·형용사 어간, 어근, 한자. 조사·어미·접두사는 버립니다
  ("재부팅한" → `부팅`).
- **영문·숫자는 형태소 분석과 별개로 원문에서 그대로 뽑습니다.** `CrashLoopBackOff`, `cert-manager`(→ `cert`,
  `manager`), `c-1a2b3c`(→ `c`, `1a2b3c`) 같은 토큰이 소문자로 정확히 맞습니다.
- **가중치:** BM25(`k=1.2`, `b=0.75`)로 문서 쪽 단어 가중치를 매기고, 질의 쪽은 단어마다 1.0입니다. 단어는 mmh3로
  해시한 ID로 저장하고, IDF는 Qdrant가 계산합니다.
- **설치와 속도:** `kiwipiepy`는 `pip`로 설치되며 모델(약 100MB)이 패키지에 들어 있어 실행 중에 내려받는 것이
  없습니다. 형태소 분석은 초당 수만 글자 수준이라 임베딩 시간에 비하면 작습니다. 대신 Kiwi를 불러오는 데 몇 초가
  걸리고 메모리를 수백 MB(측정 환경에서 약 0.5GB) 더 씁니다. 서버와 `rag-ingest`가 각각 불러옵니다. 메모리가
  부족하면 키워드 검색을 끄세요([키워드 검색 끄기와 재구축](#키워드-검색-끄기와-재구축)).

#### 비교 결과

한국어 문서 6개와 질의 8개로, 공백 기준으로 단어를 나눈 BM25와 Kiwi 형태소로 나눈 BM25의 순위를 비교했습니다.
질의는 문서와 조사·어미가 다르게 붙은 자연스러운 문장입니다. 표의 숫자는 정답 문서의 BM25 순위이고, `—`는 정답 문서와
맞는 단어가 하나도 없어 점수가 0인 경우입니다.

| 질의 | 공백 기준 토큰 | 순위 | Kiwi 형태소 토큰 | 순위 |
|------|------|:--:|------|:--:|
| 볼륨을 분리하는 방법 | `볼륨을` `분리하는` `방법` | 1 | `볼륨` `분리` `방법` | 1 |
| 볼륨이 멈췄어요 | `볼륨이` `멈췄어요` | 1 | `볼륨` `멈추` | 1 |
| 인증서 갱신은 어떻게 하나요 | `인증서` `갱신은` `어떻게` `하나요` | — | `인증서` `갱신` `하` | 1 |
| 디스크가 가득 찼을 때 스냅샷 정리 | `디스크가` `가득` `찼을` `때` `스냅샷` `정리` | — | `디스크` `차` `때` `스냅샷` `정리` | 1 |
| 롤백하는 절차 | `롤백하는` `절차` | — | `롤` `백` `절차` | 1 |
| 데이터베이스에 연결되지 않아요 | `데이터베이스에` `연결되지` `않아요` | — | `데이터베이스` `연결` | 1 |
| CrashLoopBackOff 해결 | `crashloopbackoff` `해결` | 1 | `crashloopbackoff` `해결` | 1 |
| 로그를 정리하고 싶어요 | `로그를` `정리하고` `싶어요` | 2 | `로그` `정리` | 1 |
| **합계** | | **1위 3/8, 점수 0 4/8** | | **1위 8/8, 점수 0 0/8** |

- 공백 기준으로 나누면 "인증서**가**"(문서)와 "인증서"(질의)처럼 조사만 달라도 다른 단어가 되어, 질의의 절반에서 정답
  문서와 맞는 단어가 하나도 없었습니다. Kiwi 형태소로 나누면 모든 질의에서 정답 문서가 1위였습니다.
- 영어 토큰(`CrashLoopBackOff`)은 두 방식이 똑같이 찾습니다.
- "롤백" → `롤` `백`처럼 사전에 없는 단어는 형태소가 어색하게 나뉩니다. 문서와 질의가 같은 방식으로 나뉘므로 이 예에서는
  맞았지만, 신조어·전문 용어가 많은 문서에서는 결과가 달라질 수 있습니다.
- 같은 문서를 실제 Qdrant에 색인해 하이브리드 검색(`rag_search`와 같은 경로)으로 돌려도 8개 질의 모두
  정답 문서가 1위였습니다(의미 검색 쪽은 테스트용 임베딩이라, 이 결과는 BM25가 순위를 정한 것입니다).

> ⚠️ 문서 6개, 질의 8개짜리 작은 비교라 정확도 수치로 일반화할 수 없습니다. 방향을 확인하는 용도로 보고, 실제 문서와
> 질의로 확인하세요. "공백 기준" 열은 공백·기호로 나누고 소문자로 바꾼, 흔히 쓰는 BM25 토큰입니다.

<details>
<summary>비교에 쓴 문서 6개</summary>

| 문서 | 내용 |
|------|------|
| A | 노드를 재부팅한 뒤 Longhorn 볼륨이 attaching 상태에서 멈추면 볼륨을 강제로 분리하고 다시 연결합니다. |
| B | 파드가 CrashLoopBackOff 상태라면 컨테이너 로그를 확인하고 설정 파일의 오류를 고칩니다. |
| C | 인증서가 만료되었을 때는 cert-manager로 인증서를 갱신하고 인그레스를 다시 시작합니다. |
| D | 디스크 사용량이 90%를 넘으면 오래된 스냅샷과 로그를 정리해 공간을 확보합니다. |
| E | 배포가 실패하면 이전 버전으로 롤백하고 원인을 분석해 재배포합니다. |
| F | 데이터베이스 연결이 끊기면 커넥션 풀 설정과 네트워크 정책을 점검합니다. |

정답 문서: 질의 순서대로 A, A, C, D, E, F, B, D. 점수는 BM25(`k=1.2`, `b=0.75`)와 IDF `ln(1 + (N − df + 0.5)/(df + 0.5))`로
계산했습니다.

</details>

#### 키워드 검색 끄기와 재구축

키워드 검색은 설정 파일(`~/.config/rag-mcp/rag-mcp.env`)의 `RAG_HYBRID`로 켜고 끕니다. 끄면 의미 검색만 하고 Kiwi를
불러오지 않습니다. 컬렉션 구조가 바뀌므로 바꾼 뒤에는 재구축하세요.

```bash
vi ~/.config/rag-mcp/rag-mcp.env      # RAG_HYBRID=false (다시 켜려면 true)
systemctl --user restart rag-mcp
rag-ingest --recreate

# 확인 — 시작 로그에 hybrid=True/False 가 나옵니다
journalctl --user -u rag-mcp | grep -E 'sparse model|hybrid'
```

- **확인 방법:** MCP 도구 `rag_health()` 응답의 `retrieval`에 `hybrid`(키워드 검색 동작 여부)와 `sparse_model`
  (`kiwi-bm25`)이 나옵니다. Kiwi를 불러오지 못하면 시작 로그에 `sparse model ... unavailable` 경고가 남고 의미
  검색만으로 동작합니다.
- **이전 버전에서 업그레이드했다면** 설정을 바꾸지 않았더라도 한 번 `rag-ingest --recreate`를 실행하세요. 키워드
  벡터를 만드는 방식이 Kiwi 형태소 분석으로 바뀌어, 기존 색인의 키워드가 새 질의와 맞지 않습니다.

### 개념 정리

| 용어 | 쉬운 의미 | 이 프로젝트에서 |
|------|------|------|
| 밀집 벡터 (dense vector) | 의미 / 시맨틱 | 임베딩 모델(bge-m3) → 1024개의 float |
| 희소 벡터 (sparse vector) | 정확한 단어 / 키워드 | Kiwi 형태소 + BM25 → `{term_id: weight}` |
| HNSW | 근사 최근접 이웃 그래프 인덱스 | 빠른 코사인 검색, 거의 정확한 top-k |
| RRF | 두 순위 목록을 위치 기준으로 합치는 방법 | 밀집 + 희소 결과를 결합 |
| 리랭커 (reranker) | `(질의, 청크)` 쌍의 순서를 다시 매기는 크로스 인코더 | Cohere/Jina (선택 사항) |
| 비대칭 접두사 | 질의와 문서에 서로 다른 작업 태그를 붙이는 방식 | e5·nomic 계열은 필요, bge-m3는 불필요 |

> ℹ️ **임베딩 모델은 여러분의 데이터로 학습되지 않습니다.** 사전 학습된 모델이 일반적인 의미 지식으로
> 텍스트를 숫자로 바꿀 뿐입니다. 조직이나 업계 고유의 맥락은 Qdrant에 수집한 **문서**와 (선택적으로)
> 리랭커에서 나옵니다.

## 사내 임베딩 서버 연결

rag-mcp는 텍스트를 벡터로 바꾸는 임베딩 모델을 직접 실행하지 않고, **사내 임베딩 서버**에 요청합니다. 임베딩 서버는
rag-mcp에 포함되어 있지 않으므로, 사내에서 운영 중인 서버의 주소·인증 정보·모델 이름을 설정 파일에 적어 연결합니다.

```bash
# ~/.config/rag-mcp/rag-mcp.env
EMBEDDINGS_BASE_URL=http://embedding.internal.example:8080
# 인증이 없으면 비워 둠
EMBEDDINGS_API_KEY=
# 사내 서버가 쓰는 모델 이름을 정확히
EMBEDDINGS_MODEL=bge-m3
```

```bash
systemctl --user restart rag-mcp
rag-ingest
```

### 사내 서버가 갖춰야 할 조건

| 항목 | 내용 |
|------|------|
| API 형식 | **OpenAI 호환 `/v1/embeddings`** — `{"model": ..., "input": [...]}`로 요청하고 `data[].embedding`으로 받는 형식. 다른 형식은 연결할 수 없습니다 |
| 주소 | `EMBEDDINGS_BASE_URL` 뒤에 `/v1/embeddings`가 자동으로 붙습니다. 주소가 `/v1`로 끝나면 `/embeddings`만 붙습니다 |
| 인증 | `EMBEDDINGS_API_KEY`가 있으면 `Authorization: Bearer <키>` 헤더로 보내고, 비어 있으면 보내지 않습니다 |
| 모델 | 한국어 문서라면 다국어 모델(예: `bge-m3`, 1024차원)을 권장합니다. 벡터 차원은 첫 수집 때 자동으로 정해집니다 |
| 요청 크기 | 한 번에 청크 32개씩(`EMBED_BATCH_SIZE`) 보냅니다. 서버의 요청당 입력 개수·토큰 제한에 걸려 413/400 오류가 나면 줄이세요 |
| 응답 시간 | 요청당 기본 60초(`RAG_TIMEOUT_SECONDS`)까지 기다립니다 |

- **`EMBEDDINGS_BASE_URL`에는 기본값이 없습니다.** 설정을 빠뜨렸을 때 문서가 의도하지 않은 곳으로 전송되지 않도록,
  운영자가 사내 서버 주소를 명시해야 합니다. 비어 있으면 검색과 수집이 오류로 알려 줍니다.
- 응답은 `index`로 다시 정렬하므로, 서버가 순서를 바꿔 돌려줘도 청크와 벡터가 어긋나지 않습니다.
- HTTP 오류는 상태 코드와 응답 본문을 그대로 보여 줘, 잘못된 주소(404)나 인증 문제(401)를 바로 알 수 있습니다.

### 설정 예시

모두 설정 파일 `~/.config/rag-mcp/rag-mcp.env`에 적고, 바꾼 뒤에는 `systemctl --user restart rag-mcp`로 재시작합니다.
같은 줄 끝에 주석을 달지 마세요(systemd가 주석까지 값으로 읽습니다).

**① 기본 — 인증 없는 사내 서버**

```bash
EMBEDDINGS_BASE_URL=http://embedding.internal.example:8080
EMBEDDINGS_API_KEY=
EMBEDDINGS_MODEL=bge-m3
```

요청 주소: `http://embedding.internal.example:8080/v1/embeddings`

**② API 게이트웨이 경로 아래에 있고 토큰 인증을 쓰는 서버**

```bash
# 주소가 /v1 로 끝나면 /embeddings 만 붙습니다
EMBEDDINGS_BASE_URL=https://ai-gateway.internal.example/embedding/v1
# Authorization: Bearer <토큰> 으로 보냅니다
EMBEDDINGS_API_KEY=사내-발급-토큰
EMBEDDINGS_MODEL=bge-m3
```

요청 주소: `https://ai-gateway.internal.example/embedding/v1/embeddings`. 토큰이 들어 있으므로 설정 파일 권한(`600`)을
그대로 두세요.

**③ 사내 인증서(사설 CA)를 쓰는 HTTPS 서버**

```bash
EMBEDDINGS_BASE_URL=https://embedding.internal.example
EMBEDDINGS_MODEL=bge-m3
# 사내 CA 인증서(PEM) 묶음. 이 파일의 인증서만 신뢰하므로 사내 CA가 반드시 들어 있어야 합니다
SSL_CERT_FILE=/etc/pki/tls/certs/internal-ca-bundle.pem
```

인증서를 확인할 수 없으면 `CERTIFICATE_VERIFY_FAILED` 오류가 납니다. 인증서 검증을 끄는 설정은 없습니다. 리랭킹으로 외부
API도 함께 쓴다면 이 묶음에 공용 CA 인증서도 넣어야 합니다.

**④ 질의·문서 접두사가 필요한 모델 (예: e5 계열)**

```bash
EMBEDDINGS_BASE_URL=http://embedding.internal.example:8080
EMBEDDINGS_MODEL=multilingual-e5-large
# 끝의 공백까지 접두사이므로 큰따옴표로 감쌉니다
EMBED_QUERY_PREFIX="query: "
EMBED_DOC_PREFIX="passage: "
```

**⑤ 요청 크기·응답 시간 제한이 있는 서버**

```bash
EMBEDDINGS_BASE_URL=http://embedding.internal.example:8080
EMBEDDINGS_MODEL=bge-m3
# 요청당 청크 수를 줄임 (기본 32) — 413/400 오류가 날 때
EMBED_BATCH_SIZE=8
# 응답이 느린 서버 — 초 단위 (기본: 임베딩 60, 서버의 Qdrant 연결 30. 설정하면 둘 다 이 값)
RAG_TIMEOUT_SECONDS=120
# 모델의 최대 입력이 짧을 때(예: 512 토큰) 청크를 줄임 (기본 1500자). 바꾸면 rag-ingest 를 다시 실행
CHUNK_SIZE=800
```

**⑥ 프록시를 거쳐야 닿는 서버**

```bash
EMBEDDINGS_BASE_URL=https://embedding.internal.example
EMBEDDINGS_MODEL=bge-m3
HTTPS_PROXY=http://proxy.internal.example:3128
# 같은 서버의 Qdrant(localhost:6333)는 프록시를 거치지 않게 합니다
NO_PROXY=localhost,127.0.0.1
```

### 연결 확인

rag-mcp에 연결하기 전에, 사내 서버가 OpenAI 호환 형식으로 응답하는지 확인합니다.

```bash
curl -s http://embedding.internal.example:8080/v1/embeddings \
  -H "Authorization: Bearer <사내-서버-토큰>" -H "Content-Type: application/json" \
  -d '{"model": "bge-m3", "input": ["테스트"]}' | head -c 200
```

응답에 `"embedding": [...]`이 보이면 됩니다. rag-mcp를 재시작한 뒤 MCP 도구 `rag_health()`에서
`embeddings.reachable: true`와 모델 이름을 확인하고, 수집 후 컬렉션 차원이 모델과 맞는지 봅니다.

```bash
# 컬렉션 차원 확인 → bge-m3 라면 "size":1024
curl -s http://localhost:6333/collections/rag_kb | grep -o '"size":[0-9]*'
```

### 비대칭 모델 접두사

일부 모델은 질의와 문서에 서로 다른 작업 태그를 붙여야 성능이 납니다. 사내 서버의 모델이 여기에 해당하는지
확인하세요.

| 모델 | 질의 접두사 | 문서 접두사 | 설정 |
|------|------|------|------|
| nomic 계열 | `search_query: ` | `search_document: ` | 이름에 `nomic`이 있으면 자동 |
| bge-m3 등 대칭 모델 | 없음 | 없음 | 자동 (기본값) |
| e5, 구형 bge 등 | `query: ` | `passage: ` | `EMBED_QUERY_PREFIX` / `EMBED_DOC_PREFIX`로 직접 지정 |

### 일관성 규칙

> ⚠️ **수집과 질의는 반드시 같은 서버와 모델을 써야 합니다.** 서로 다른 모델의 벡터는 차원과 의미가 달라 비교할 수
> 없고, 섞어 쓰면 아무 경고 없이 검색이 망가집니다. 사내 서버의 모델이 바뀌었거나 `EMBEDDINGS_MODEL`을 바꾼 뒤에는
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

- **임베딩 모델:** 사내 서버에서 다국어 모델을 쓰세요. 기본 모델 이름 `bge-m3`(1024차원, 최대 입력 8192 토큰)는
  한국어를 포함한 다국어 모델로, 영어 위주 모델보다 한국어 질의·문서의 의미 검색이 정확합니다.
- **키워드 검색도 한국어를 이해합니다.** 한국어 형태소 분석기 Kiwi가 조사·어미를 떼어 "볼륨이"와 "볼륨을"을
  같은 단어로 맞춥니다([키워드 검색(BM25)과 한국어](#키워드-검색bm25과-한국어)).
- **리랭커도 다국어 모델이 기본값입니다.** 리랭킹을 켜면(`RERANK_PROVIDER=cohere`) 기본 모델은
  `rerank-multilingual-v3.0`입니다. 리랭킹은 기본으로 Cohere의 외부 API(`RERANK_BASE_URL`)에 접속하므로, 외부 접속이
  막힌 환경에서는 사내의 Cohere 호환 `/rerank` 서버로 `RERANK_BASE_URL`을 바꾸거나 끈 채로 두세요.
- **청크 크기.** 기본 `CHUNK_SIZE=1500`자는 bge-m3의 최대 입력 길이보다 훨씬 작아 그대로 써도 됩니다. 사내 서버의
  모델이 입력 길이가 짧다면 `CHUNK_SIZE`를 그에 맞게 줄이세요.
- **속도.** 수집 속도는 사내 서버의 처리량에 달려 있습니다. 서버가 허용하는 범위에서 `EMBED_BATCH_SIZE`를 늘리면
  빨라집니다.

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
| `EMBEDDINGS_BASE_URL` | _(없음, 필수)_ | 사내 임베딩 서버 주소 — OpenAI 호환 `/v1/embeddings` (`/v1`은 자동으로 붙음). 예: `http://embedding.internal.example:8080` |
| `EMBEDDINGS_API_KEY` | _(미설정)_ | 사내 임베딩 서버 인증 토큰 (`Authorization: Bearer`로 보냄, 비워 두면 보내지 않음) |
| `EMBEDDINGS_MODEL` | `bge-m3` | 임베딩 모델 이름 (사내 서버가 쓰는 이름에 맞춤) |
| `EMBED_QUERY_PREFIX` / `EMBED_DOC_PREFIX` | 자동 (nomic → `search_query: `/`search_document: `, 그 외 → 빈 값) | 자동 감지가 놓치는 비대칭 모델에만 지정 (e5 등 → `query: `/`passage: `) |
| **수집** | | |
| `RAG_KNOWLEDGE_DIR` | `~/.local/share/rag-mcp/data/knowledge` | 문서 디렉터리 (`official/`). `rag-ingest`가 수집 (설치 스크립트가 실제 경로로 바꿔 넣음) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1500` / `100` | 청크 크기와 겹침 (글자 수) |
| `EMBED_BATCH_SIZE` | `32` | 임베딩 요청당 청크 수 |
| `QDRANT_UPSERT_BATCH` | `64` | Qdrant 업서트 요청당 포인트 수 |
| **검색** | | |
| `RAG_HYBRID` | `true` | 하이브리드 검색(밀집 + BM25). 바꾸면 `--recreate` 필요 |
| `RAG_DEFAULT_LIMIT` / `RAG_MAX_LIMIT` | `5` / `20` | 검색 결과 기본 개수 / 최대 개수 |
| `RAG_SNIPPET_CHARS` | `1200` | 결과마다 반환하는 텍스트의 최대 글자 수 (넘으면 `truncated: true`) |
| **리랭킹** | | |
| `RERANK_PROVIDER` | `none` | `none`(비활성) 또는 `cohere`(Cohere/Jina 호환 `/rerank`) |
| `RERANK_MODEL` | `rerank-multilingual-v3.0` | 리랭커 모델 (Cohere 다국어). Jina는 `jina-reranker-v2-base-multilingual` |
| `RERANK_BASE_URL` | `https://api.cohere.com` | 리랭커 API 기본 URL (Jina는 `https://api.jina.ai/v1`) |
| `RERANK_API_KEY` | _(미설정)_ | 리랭커 API 키 |
| `RERANK_CANDIDATES` | `30` | 리랭킹 전에 가져오는 후보 수 |
| `RERANK_TIMEOUT` | `30` | 리랭커 HTTP 타임아웃 (초) |

> 설정 파일에서는 `KEY=value  # 주석`처럼 같은 줄 끝에 주석을 달지 마세요. systemd의 `EnvironmentFile`은
> 주석까지 값으로 읽습니다. 주석은 항상 별도 줄에 쓰세요.

## 알려진 제한

- 문서를 삭제하거나 이름을 바꾸면 이전 청크가 남습니다 → `rag-ingest --recreate`로 재구축하세요.
- 키워드 검색의 한국어 형태소 분석은 사전 기반이라, 사전에 없는 신조어나 붙여 쓴 복합어는 다르게 나뉠 수 있습니다.
- 마크다운(`.md`)만 수집합니다. PDF 등 다른 형식은 마크다운으로 바꿔서 넣어야 합니다.
- 하이브리드 검색의 `score`는 RRF 결합 점수라서, 코사인 유사도처럼 임계값을 정하는 데 쓸 수 없습니다.
- 같은 서버에서는 rag-mcp를 하나만 실행할 수 있습니다. Qdrant 포트(6333, 6334)가 systemd 유닛에 고정되어
  있기 때문입니다.

## 저장소 구조

| 경로 | 내용 |
|------|------|
| [`tools/server.py`](tools/server.py) | FastMCP 서버. 읽기 전용 도구 3개(검색 `rag_search`, 상태 확인 `rag_collections`·`rag_health`)를 제공 |
| [`tools/ingest.py`](tools/ingest.py) | 마크다운 문서를 읽어 청크로 나누고 임베딩해 Qdrant에 업서트 (`rag-ingest`) |
| [`tools/embeddings.py`](tools/embeddings.py) | OpenAI 호환 임베딩 호출, 비대칭 모델 접두사 처리 |
| [`tools/vectorstore.py`](tools/vectorstore.py) | Qdrant 컬렉션 스키마, BM25 희소 벡터, 하이브리드 질의 |
| [`tools/kiwi_bm25.py`](tools/kiwi_bm25.py) | 한국어 형태소 분석(Kiwi) 기반 BM25 희소 벡터 |
| [`tools/reranker.py`](tools/reranker.py) | Cohere/Jina 호환 크로스 인코더 리랭킹 (선택 사항) |
| `requirements.txt` | Python 의존성 |
| `deploy/` | 설치·제거 스크립트, systemd 유닛, `rag-ingest` 명령 템플릿 |
| `knowledge/official/` | 샘플 문서 |
| `.claude-plugin/marketplace.json` | Claude Code 플러그인 마켓플레이스 정의 |
| `plugins/rag-mcp/` | Claude Code 플러그인 (MCP 서버 연결 설정, `rag-knowledge` 스킬) |
| `.claude/skills/repo-check/` | 저장소 점검 스킬 (Claude Code에서 `/repo-check`, 직접 실행: `bash .claude/skills/repo-check/scripts/check.sh`) |
| `tests/` | 테스트 |

`ingest.py`와 `server.py`는 모두 `vectorstore.py`와 `embeddings.py`를 거칩니다. 그래서
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
