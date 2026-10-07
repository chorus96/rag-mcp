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
- **OpenAI 호환 임베딩** — `/v1/embeddings`를 제공하는 엔드포인트라면 무엇이든 씁니다. 직접 띄운
  서버(TEI, vLLM 등, 오프라인)나 호스팅 API(OpenAI 등)를 설정 몇 줄로 지정합니다.
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
  │  LibreChat ·         │  (agent / CI capturing
  │  your own agents     │  incidents + human feedback)
  └──────────────────────┘
```

## 빠른 시작 (Linux 서버 설치)

사전 요구 사항: systemd를 쓰는 Linux 서버(Ubuntu, Debian, RHEL 계열 등), Python 3.10 이상과
venv 모듈(Debian/Ubuntu: `apt install python3-venv`), `curl`, 그리고 **OpenAI 호환 임베딩
엔드포인트**(아래 [임베딩 엔드포인트 설정](#임베딩-엔드포인트-설정) 참고)가 필요합니다.

```bash
git clone https://github.com/mmelmesary/rag-mcp.git && cd rag-mcp
sudo ./deploy/install.sh        # Qdrant + rag-mcp 설치, systemd 서비스로 시작

# 임베딩 엔드포인트 지정 (EMBEDDINGS_BASE_URL / EMBEDDINGS_API_KEY / EMBEDDINGS_MODEL)
sudo vi /etc/rag-mcp/rag-mcp.env
sudo systemctl restart rag-mcp

sudo rag-ingest                 # 샘플 문서 색인
```

`install.sh`가 설치하는 것:

| 경로 | 내용 |
|------|------|
| `/opt/rag-mcp/app`, `/opt/rag-mcp/venv` | 애플리케이션 코드와 Python 가상환경 |
| `/opt/rag-mcp/qdrant/qdrant` | Qdrant 바이너리 (기본 `v1.12.4`, `127.0.0.1:6333`에만 바인드) |
| `/etc/rag-mcp/rag-mcp.env` | 설정 파일 ([.env.example](.env.example) 복사본) |
| `/var/lib/rag-mcp/knowledge` | 색인할 문서 (샘플 문서가 복사됨) |
| `/var/lib/rag-mcp/qdrant` | Qdrant 데이터 |
| `qdrant.service`, `rag-mcp.service` | systemd 서비스 (부팅 시 자동 시작) |
| `/usr/local/bin/rag-ingest` | 문서 수집 명령 |

모든 서비스는 시스템 사용자 `rag-mcp` 권한으로 실행됩니다. 스크립트를 다시 실행하면 업그레이드로
동작합니다 — 코드와 의존성을 갱신하고 서비스를 재시작하며, 설정 파일과 데이터는 건드리지 않습니다.

확인:

```bash
systemctl status qdrant rag-mcp
curl -s http://localhost:8084/mcp            # MCP 엔드포인트 (핸드셰이크 없이 400 응답 = 정상 동작)
curl -s http://localhost:6333/collections    # rag_kb 컬렉션이 포인트와 함께 존재
journalctl -u rag-mcp -f                     # 서버 로그
```

설정을 바꾼 뒤에는 `sudo systemctl restart rag-mcp`로 적용합니다. 제거는
`sudo ./deploy/uninstall.sh`(데이터 유지) 또는 `sudo ./deploy/uninstall.sh --purge`(모두 삭제)입니다.

> 최초 시작 시 FastEmbed가 BM25 모델을 `huggingface.co`에서 한 번 내려받아
> `/var/lib/rag-mcp/fastembed_cache`에 저장합니다. 서버가 인터넷에 접속할 수 없거나 프록시가
> 필요하면 설정 파일에 `HTTPS_PROXY`를 넣으세요. 내려받지 못하면 키워드(BM25) 검색 없이 의미
> 검색만으로 동작합니다.

### 사용자 모드 설치 (root 없이)

관리자 권한이 없거나 개인 계정에만 설치하려면 `--user`로 설치하세요. 현재 사용자 홈 아래에
설치되고, 서비스는 사용자 systemd(`systemctl --user`)로 실행됩니다.

```bash
./deploy/install.sh --user
rag-ingest                                  # ~/.local/bin 이 PATH에 있어야 합니다
```

| 항목 | 시스템 모드 (`sudo`) | 사용자 모드 (`--user`) |
|------|------|------|
| 앱 / venv / Qdrant | `/opt/rag-mcp` | `~/.local/share/rag-mcp` |
| 설정 파일 | `/etc/rag-mcp/rag-mcp.env` | `~/.config/rag-mcp/rag-mcp.env` |
| 데이터 (문서, Qdrant, 캐시) | `/var/lib/rag-mcp` | `~/.local/share/rag-mcp/data` |
| 서비스 | `systemctl ...` | `systemctl --user ...` |
| 로그 | `journalctl -u rag-mcp` | `journalctl --user -u rag-mcp` |
| 수집 | `sudo rag-ingest` | `rag-ingest` |
| 제거 | `sudo ./deploy/uninstall.sh [--purge]` | `./deploy/uninstall.sh --user [--purge]` |

이 문서의 다른 명령도 사용자 모드에서는 `sudo`를 빼고, `systemctl`을 `systemctl --user`로,
설정 파일 경로를 `~/.config/rag-mcp/rag-mcp.env`로 바꿔 쓰면 됩니다.

주의할 점:

- **로그아웃하면 서비스가 멈춥니다.** 사용자 서비스는 기본적으로 로그인해 있는 동안만 실행됩니다.
  계속 실행하고 부팅 시 자동으로 시작하려면 `loginctl enable-linger $USER`를 한 번 실행하세요
  (배포판에 따라 관리자 권한이 필요할 수 있습니다).
- **ssh 등으로 직접 로그인한 세션에서 설치하세요.** `su`나 `sudo -u`로 전환한 셸에서는 사용자
  systemd에 연결되지 않아 설치 스크립트가 멈춥니다. 서비스 등록 없이 파일만 설치하려면
  `SKIP_START=1 ./deploy/install.sh --user`를 쓰세요.
- **Python venv 모듈**(Debian/Ubuntu의 `python3-venv`)이 없다면 그 설치만은 관리자에게
  요청해야 합니다.
- 같은 서버에서 여러 사용자가 설치하면 포트(8084, 6333, 6334)가 겹치므로 한 명만 실행할 수
  있습니다.

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
    url: http://<rag-mcp 서버 주소>:8084/mcp
```

그 밖의 MCP 클라이언트: 위 URL로 HTTP/streamable-http 서버를 등록하면 됩니다.

다른 서버에서 접속한다면 방화벽에서 8084 포트를 열고, 내부 쓰기 API를 보호하도록 설정 파일에
`RAG_INTERNAL_TOKEN`을 설정하세요.

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

`/var/lib/rag-mcp/knowledge/` 아래에 마크다운이나 PDF를 넣으세요(하위 폴더 이름이 기본 `doc_type`이
됩니다. 예: `knowledge/incidents/*` → `incident`). 디렉터리는 설정 파일의 `RAG_KNOWLEDGE_DIR`로
바꿀 수 있습니다. 마크다운은 선택적으로 YAML front matter를 지원합니다.

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

그런 다음 다시 색인하세요. 서버 재시작은 필요 없습니다.

```bash
sudo cp my-runbook.md /var/lib/rag-mcp/knowledge/runbooks/
sudo rag-ingest
```

수집은 멱등적입니다(청크 ID는 `(source, chunk index)`에서 파생됩니다). 내용이 줄어든 문서는
더 이상 쓰이지 않는 뒷부분 청크가 삭제됩니다. 문서가 바뀔 때마다 CI/cron에서 실행하세요.
알려진 제한 사항: 파일을 삭제하거나 이름을 바꿔도 기존 청크는 제거되지 않습니다 — 삭제/이름 변경
후에는 `sudo rag-ingest --recreate`를 사용하세요.

## 임베딩 엔드포인트 설정

임베딩은 OpenAI 호환 `/v1/embeddings` 엔드포인트로만 처리합니다. `EMBEDDINGS_BASE_URL`에는
기본값이 없으므로 반드시 지정해야 합니다(문서가 의도치 않게 외부로 전송되지 않도록). `/v1`은
자동으로 붙습니다.

**직접 띄운 서버 (오프라인, 권장)** — 예: 같은 서버에서
[Hugging Face TEI](https://github.com/huggingface/text-embeddings-inference)로 `BAAI/bge-m3` 서빙.
vLLM, LocalAI 등도 같은 방식입니다.

```bash
# /etc/rag-mcp/rag-mcp.env
EMBEDDINGS_BASE_URL=http://localhost:8080
EMBEDDINGS_API_KEY=
# 엔드포인트가 쓰는 모델 이름 (예: vLLM은 BAAI/bge-m3)
EMBEDDINGS_MODEL=bge-m3
```

**호스팅 API** — 예: OpenAI. 문서 내용이 외부로 전송됩니다.

```bash
# /etc/rag-mcp/rag-mcp.env
EMBEDDINGS_BASE_URL=https://api.openai.com
EMBEDDINGS_API_KEY=sk-...
EMBEDDINGS_MODEL=text-embedding-3-small
```

엔드포인트나 모델을 바꾼 뒤에는 서버를 재시작하고 컬렉션을 다시 만드세요(수집과 질의는 항상
같은 엔드포인트+모델을 사용해야 합니다).

```bash
sudo systemctl restart rag-mcp
sudo rag-ingest --recreate
```

> **Ollama 제공자를 쓰던 기존 설치:** Ollama 전용 제공자(`EMBEDDINGS_PROVIDER=ollama`,
> `OLLAMA_BASE_URL`)는 제거되었습니다. 설치 스크립트는 기존 설정 파일을 덮어쓰지 않으므로, 그
> 두 줄을 지우고 `EMBEDDINGS_BASE_URL`을 지정하세요. Ollama를 계속 쓰려면 Ollama의 OpenAI 호환
> 엔드포인트를 지정하면 됩니다(`EMBEDDINGS_BASE_URL=http://localhost:11434`). 같은 모델이면
> 벡터가 같으므로 재수집은 필요 없지만, 확실하게 하려면 `--recreate`로 다시 수집하세요.

## 한국어 문서와 임베딩 모델

기본 임베딩 모델 이름은 한국어를 포함한 다국어 모델 **`bge-m3`**입니다. 엔드포인트에서 bge-m3를
서빙하면 한국어 문서와 질의를 별도 설정 없이 바로 쓸 수 있습니다. 호스팅 API를 쓴다면 OpenAI
`text-embedding-3-small`/`-large`도 다국어를 지원합니다.

영어 문서만 쓰고 더 가벼운 모델을 원하면 엔드포인트에서 `nomic-embed-text` 같은 모델을 서빙하고
이름을 바꾸면 됩니다(nomic 계열은 작업 접두사가 자동으로 붙습니다).

```bash
# /etc/rag-mcp/rag-mcp.env
EMBEDDINGS_MODEL=nomic-embed-text
```

모델을 바꾼 뒤에는 서버를 재시작하고 컬렉션을 재구축하세요. 이전 기본값(`nomic-embed-text`)으로
만든 기존 컬렉션을 bge-m3로 옮길 때도 마찬가지입니다.

```bash
sudo systemctl restart rag-mcp
sudo rag-ingest --recreate
```

TEI로 bge-m3를 띄우는 예, 다국어 리랭커, BM25의 한국어 한계 등 자세한 내용은
[docs/DESIGN.md의 "한국어 / 다국어 문서"](docs/DESIGN.md#한국어--다국어-문서)를 참고하세요.

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
cp .env.example .env && set -a && . ./.env && set +a   # 설정 불러오기 (선택)
python3 server.py                       # 로컬 :6333 의 Qdrant에 연결해 실행
python3 ingest.py --path knowledge
```

## 라이선스

[MIT](LICENSE)
