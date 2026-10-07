# rag-mcp — 설계 (Qdrant, 읽기 전용)

런북, 과거 장애, RCA(근본 원인 분석)로 이루어진 **Qdrant** 지식 베이스에 대한 시맨틱 검색을
MCP 서버로 노출하여, 에이전트가 운영 중인 클러스터를 디버깅하면서 과거 맥락을 가져올 수 있게
합니다(에이전틱 검색, agentic retrieval).

## 설계

- **읽기 전용 인터페이스.** MCP 도구는 *검색*만 합니다. 지식 베이스는
  [`ingest.py`](../ingest.py)가 별도 경로로 기록하므로, LLM에 노출되는 인터페이스는 읽기 전용으로
  유지됩니다.
- **벤더 중립.** 특정 LLM, UI, 임베딩 벤더에 묶여 있지 않습니다. 채팅 LLM은 연결하는 MCP
  클라이언트가 정합니다. 임베딩은 교체 가능한 제공자([`embeddings.py`](../embeddings.py))를
  거칩니다 — `ollama`(오프라인 기본값) 또는 `openai`(OpenAI 호환 엔드포인트). 코드는 같고 환경
  변수 하나만 바꾸면 됩니다. 접두사는 자동으로 붙습니다. 비대칭 모델(nomic)에는
  `search_query:`/`search_document:` 접두사가 붙고, 대칭 모델(OpenAI `text-embedding-*`)에는
  붙지 않습니다 — 별도 설정이 필요 없습니다. 자동 감지가 모르는 다른 비대칭 모델 계열(e5/bge는
  `query:`/`passage:` 사용)은 `EMBED_QUERY_PREFIX`/`EMBED_DOC_PREFIX`로 재정의하세요.
- **저장소.** **명명된 벡터(named vectors)**를 갖는 Qdrant 컬렉션 하나(`rag_kb`)를 씁니다 —
  `dense` 코사인 벡터와 (하이브리드가 켜져 있으면) `bm25` 희소 벡터, 그리고 장애와 런북을
  구분하는 필터용 `doc_type` 페이로드 필드로 구성됩니다.

### 검색 파이프라인 (`vectorstore.py` + `reranker.py`)

1. **하이브리드 검색**(재현율) — 밀집 임베딩 + 로컬 **BM25 희소** 벡터(FastEmbed
   `Qdrant/bm25`, 오프라인, 키 불필요)를 Qdrant Query API를 통해 Reciprocal Rank Fusion으로
   결합합니다. BM25는 밀집 벡터가 놓치는 정확한 토큰(`CrashLoopBackOff`, `c-xxxxx`, `Longhorn`)을
   찾아냅니다. 기본으로 켜져 있으며(`RAG_HYBRID`), FastEmbed를 쓸 수 없으면 밀집 전용으로
   동작합니다.
2. **리랭킹**(정밀도, 선택 사항) — `RERANK_CANDIDATES`만큼 넉넉히 가져온 뒤 Cohere/Jina 호환
   크로스 인코더로 순서를 다시 매기고 상위 몇 개만 남깁니다. 기본으로 꺼져 있으며
   (`RERANK_PROVIDER`), 최선형(best-effort)으로 동작합니다 — 어떤 실패가 나든 결합된 순서로
   되돌아갑니다.
3. **섹션 인식 청킹** — `ingest.py`는 문서를 마크다운 헤딩 기준으로 나누고 각 청크 앞에 헤딩을
   붙입니다. 선택적인 `component`/`severity`/`cluster` front matter는 저장되고 사전 필터링을 위해
   키워드 색인됩니다.

반복 장애 사전 확인(`find_similar`)은 **밀집 전용**으로 유지되어, 코사인 `min_score` 임계값이
의미를 유지합니다(RRF 점수는 척도가 다릅니다).

#### 질의가 파이프라인을 거치는 과정 (쉬운 설명)

`rag_search("Longhorn volume stuck attaching")`를 호출하면, 두 검색 방식이 텍스트를 서로 다르게
이해하기 때문에 질의 텍스트가 **두 가지 표현으로 동시에** 변환됩니다.

- **밀집 벡터(`bge-m3`)** — 질의의 *의미*가 1024개의 숫자 목록이 됩니다. Qdrant는
  **HNSW** 그래프 인덱스 위에서 **코사인 유사도**로 저장된 모든 청크와 비교합니다. HNSW는
  *근사* 최근접 이웃 검색입니다. 빠르고 수백만 개의 포인트까지 확장되지만, 반환된 top-k가
  수학적으로 정확한 top-k라는 보장은 없습니다 — 실제로는 매우 가깝습니다(정확한 전수 스캔은
  명시적으로 요청할 때만 실행됩니다).
- **BM25 희소 벡터(FastEmbed `Qdrant/bm25`)** — 질의의 *정확한 단어*가 `{term_id: weight}` 쌍의
  묶음이 됩니다. Qdrant는 역색인에서 해당 단어를 찾아, 밀집 벡터가 놓치는 정확한 토큰
  (`CrashLoopBackOff`, `c-xxxxx`, `Longhorn`)을 잡아냅니다.

두 검색은 Qdrant 안에서 동시에 실행되며(각각 `Prefetch` 하나), 각자 순위 목록을 만듭니다. 그런
다음 **RRF(Reciprocal Rank Fusion)**가 두 목록을 점수가 아닌 *순위 위치*로 병합합니다. 어느
목록에서든 상위권에 오른 청크는 높은 결합 점수(`score ≈ Σ 1/(60 + rank)`)를 받습니다. 그래서
최종 `point.score`는 코사인 유사도가 아니라 작은 결합 수치이며, `find_similar`가 밀집 전용으로
유지되는 것도 이 때문입니다(실제 코사인 값으로 임계값을 판단하므로).

마지막으로 **리랭커가 켜져 있으면**(`RERANK_PROVIDER=cohere`), 결합된 상위
`RERANK_CANDIDATES`(30)개 결과를 크로스 인코더(Cohere/Jina)로 보내 각 `(query, chunk)` 쌍에
점수를 매기고 순서를 다시 정합니다. 가장 적은 노력으로 가장 큰 정밀도 향상을 얻는 방법입니다 —
밀집 검색은 *재현율*은 좋지만 *순서*가 약하고, 리랭커가 그 순서를 바로잡습니다. 어떤 실패가 나든
결합된 순서로 되돌아가므로, 리랭킹 때문에 검색이 깨지는 일은 없습니다.

##### 개념 정리

| 용어 | 쉬운 의미 | 이 스택에서 |
|------|---------------|---------------|
| 밀집 벡터 (dense vector) | 의미 / 시맨틱 | `bge-m3` → 1024개의 float |
| 희소 벡터 (sparse vector) | 정확한 단어 / 키워드 | FastEmbed BM25 → `{term_id: weight}` |
| HNSW | 근사 최근접 이웃 그래프 인덱스 | 빠른 코사인 검색, 거의 정확한 top-k |
| RRF | 두 목록을 위치 기준으로 병합하는 순위 결합 | 밀집 + 희소 순서를 결합 |
| 리랭커 (reranker) | `(query, chunk)` 쌍의 순서를 다시 매기는 크로스 인코더 | Cohere/Jina (선택 사항) |
| 비대칭 접두사 | `search_query:` / `search_document:` 작업 태그 | nomic은 필요, 대칭 모델(OpenAI)은 불필요 |

> ℹ️ **임베딩 모델은 사전 학습된 상태로 고정되어 있습니다** — 여러분의 데이터로 학습되지
> *않습니다*. 일반적인 의미 지식으로 텍스트를 숫자로 바꿀 뿐, 조직이나 업계 고유의 맥락은 결코
> 학습하지 않습니다. 그런 맥락은 임베딩 가중치가 아니라 **Qdrant에 수집한 문서**와 (선택적으로)
> 리랭커에서 나옵니다. Qdrant 벡터는 모델에 종속되므로, 수집과 질의 사이에 모델이 절대 바뀌어서는
> 안 됩니다(아래 경고 참고).

> ⚠️ **수집과 질의는 같은 제공자 + 모델을 사용해야 합니다.** 서로 다른 모델의 벡터는 차원과
> 의미가 달라 비교할 수 없습니다 — 섞어 쓰면 아무 경고 없이 검색이 망가집니다. 모델을 바꾸면 ⇒
> 다시 수집하세요.
>
> ⚠️ **하이브리드를 켜고 끄면 컬렉션 스키마가 바뀝니다**(명명된 벡터 vs. 이름 없는 벡터).
> 전환하려면 한 번 재구축해야 합니다:
> `sudo rag-ingest --recreate`.

## 도구

| 도구 | 용도 |
|------|---------|
| `rag_search(query, doc_type?, cluster?, component?, limit?)` | KB 전체에 대한 시맨틱 검색 |
| `search_incidents(query, cluster?, component?, limit?)` | "전에 이런 일이 있었나?" — `doc_type=incident`로 필터 |
| `search_runbooks(query, cluster?, component?, limit?)` | "처리 절차가 뭐지?" — `doc_type=runbook`으로 필터 |
| `rag_collections()` | 컬렉션 목록 + 활성 포인트 수 (KB가 채워졌는지 확인) |
| `rag_health()` | Qdrant와 임베딩 모델 양쪽의 접근 가능 여부 |

세 검색 도구 모두 색인된 페이로드 필드(`cluster`, `component`)와 매칭되는 `cluster`, `component`
필터를 받습니다. **`cluster`는 *소프트* 필터입니다.** 같은 클러스터로 한정한 질의 결과가 비어
있으면 전체 범위로 다시 검색하고 응답에 `cluster_narrowed: false`를 표시하므로, "이 클러스터에서
전에 이런 일이 있었나?"라는 질문에도 다른 클러스터에서 있었던 선례가 나타납니다. `doc_type`과
`component`는 **하드** 필터입니다 — 결과가 비어 있으면 그대로 비어 있습니다. 응답에는
`cluster_narrowed`(클러스터를 요청하지 않았으면 `None`)가 포함되어, 호출자가 같은 클러스터에서
못 찾은 경우와 전체 범위에서 못 찾은 경우를 구분할 수 있습니다.

## 내부 쓰기 API — 지식 플라이휠 (MCP 도구 아님)

읽기 전용 검색 도구 외에도, `rag-mcp`는 조직의 기억이 매 조사마다 쌓이도록 작은 **내부 HTTP
API**를 제공합니다. 이것들은 일반 HTTP 라우트(`@mcp.custom_route`)이며 **`@mcp.tool()`이
아닙니다** — 따라서 LLM은 이를 보거나 호출할 수 없고, 모델에 노출되는 인터페이스는 엄격히 읽기
전용으로 유지됩니다. 신뢰할 수 있는 에이전트 프로세스만 호출해야 합니다.

| 라우트 (별도 표시가 없으면 POST) | 용도 |
|------|---------|
| `/internal/knowledge/capture` | 장애/RCA 하나를 업서트 (`fingerprint` 기준 멱등, `occurrence_count` 증가) |
| `/internal/knowledge/similar` | 반복 장애 사전 확인 — "이 증상을 본 적이 있나?" (소프트 범위 지정용 `cluster` 지원) |
| `/internal/knowledge/feedback` | 기록된 장애에 사람의 승인/거절 결정을 첨부 |
| `/internal/knowledge/stats` (GET) | 관리자/UI용 KB 개수 (전체 / 장애) |

운영 환경에서는 `RAG_INTERNAL_TOKEN`으로 보호하세요(에이전트는 같은 값을 `X-Internal-Token`으로
보냅니다). 비워 두면 로컬 개발용으로 열려 있습니다. 임베딩 + Qdrant를 이 서버가 소유하므로, 기록과
질의가 **똑같이** 임베딩됩니다 — 아래의 엄격한 규칙이 구조적으로 충족됩니다. 기록된 장애에는
구조화된 페이로드 필드(`cluster`, `namespace`, `alertname`, `component`, `status`,
`root_cause`, …)가 있으며, 저렴한 필터링을 위해 Qdrant 페이로드 인덱스가 걸려 있습니다.

## 사전 요구 사항

- systemd를 쓰는 Linux 서버, Python 3.10 이상(venv 모듈 포함), `curl`.
- 실행 중인 Qdrant — [`deploy/install.sh`](../deploy/install.sh)가 바이너리를 설치하고
  `qdrant.service`로 `127.0.0.1:6333`에서 실행합니다.
- 임베딩 제공자:
  - 같은 서버에서 실행 중이고 모델을 내려받은 **Ollama** (기본값, 오프라인):
    ```bash
    ollama pull bge-m3
    ```
  - **또는** OpenAI 호환 엔드포인트 — `EMBEDDINGS_PROVIDER=openai`로 설정하세요
    ([설정](#설정-환경-변수) 참고).

## 지식 베이스 채우기

문서는 선택적 YAML front matter가 있는 **마크다운** 또는 **PDF**입니다(PDF는 페이지 단위로
텍스트를 추출하고, 각 페이지가 `# [Page N]` 섹션이 됩니다). 하나의 `knowledge/` 트리에 두 형식을
섞어 둘 수 있습니다.

```markdown
---
title: Longhorn volume stuck attaching
type: incident        # incident | runbook | rca | ...  (기본값: 폴더 이름)
tags: [longhorn, storage]
---
# 본문...
```

PDF에는 front matter가 없습니다. `type`은 상위 폴더에서 추론하고(`knowledge/runbooks/*.pdf` →
`runbook`), 제목은 파일 이름에서 가져옵니다.

`type`의 기본값은 상위 폴더 이름(`knowledge/incidents/*` → `incident`)이므로, 유형별 폴더에
파일을 넣기만 해도 됩니다. 그런 다음 수집합니다.

```bash
# 설치 스크립트가 넣어 둔 수집 명령. 서버와 같은 설정 파일(/etc/rag-mcp/rag-mcp.env)을
# 읽으므로, rag-mcp와 정확히 같은 임베딩 설정을 사용합니다.
sudo rag-ingest

# 임베딩 제공자나 모델을 바꾼 뒤 전체 재구축:
sudo rag-ingest --recreate
```

문서 디렉터리(기본값 `/var/lib/rag-mcp/knowledge`, 설정 파일의 `RAG_KNOWLEDGE_DIR`)에 파일을
넣고 명령을 실행하기만 하면 됩니다 — 서버 재시작은 필요 없습니다.

```bash
sudo cp my-runbook.md /var/lib/rag-mcp/knowledge/runbooks/
sudo rag-ingest
```

작업의 요약 줄(`done: N file(s), M chunk(s)`)이 예상과 맞는지 항상 확인하세요. 새 문서가 개수에
포함되지 않았다면 건너뛴 것입니다 — 그 위의 로그 줄에 이유가 나옵니다(지원하지 않는 확장자, 빈
파일, 또는 추출할 텍스트가 없는 PDF). `rag-ingest`는 `rag-mcp` 사용자로 실행되므로 문서 파일을
그 사용자가 읽을 수 있어야 합니다.

정기적으로 수집하려면 cron이나 systemd 타이머에 등록하세요. 예 (매시 정각, root crontab):

```cron
0 * * * * /usr/local/bin/rag-ingest >> /var/log/rag-ingest.log 2>&1
```

설치 없이 개발용으로 직접 실행한다면 의존성을 설치하고 Qdrant 엔드포인트를 가리키게 하세요.

```bash
python3 -m pip install -r requirements.txt
QDRANT_URL=http://localhost:6333 python3 ingest.py --path knowledge
```

수집은 멱등적입니다 — 청크 ID가 `(source, chunk index)`에서 파생되므로, 다시 실행하면 중복을
만들지 않고 기존 포인트를 갱신합니다. `knowledge/` 문서가 바뀔 때마다 CI나 cron 작업에서
실행하세요.

실행 사이에 문서가 **줄어들면**(런북을 짧게 고쳤거나, PDF를 더 적은 페이지로 다시 내보낸 경우),
그대로 두면 이전의 더 긴 버전의 뒷부분 청크가 실제 파일 없이 오래된 검색 결과로 남게 됩니다 —
`(source, index)` 방식은 같지만, 문서가 이제 19번에서 끝나면 20..29번 인덱스를 덮어쓰는 것이
없기 때문입니다. 그래서 각 파일의 현재 개수를 넘는 청크는 업서트 직후 삭제되며, 오래된 텍스트를
지우려고 `--recreate`를 할 필요가 없습니다.

문서 **삭제**는 처리되지 않습니다. 수집할 파일이 남아 있지 않으니 그 청크가 고아가 되었다는 것을
아무도 모르고, 따라서 계속 검색됩니다. 명시적으로 제거하거나(`source` 페이로드 필드로 삭제)
`--recreate`로 재구축하세요. 파일을 **이동하거나 이름을 바꿀** 때도 마찬가지입니다 — `source`가
바뀌므로 이전 경로의 청크가 새 청크와 함께 남아 있게 됩니다.

문서별 쓰기는 HTTP 요청 하나의 크기가 제한되도록 배치로 나뉩니다 — 임베딩은 요청당
`EMBED_BATCH_SIZE`개 청크, 업서트는 `QDRANT_UPSERT_BATCH`개 포인트씩 처리합니다. 이는 PDF에서
특히 중요합니다. 100페이지 문서는 수백 개의 청크가 되고, 각각 밀집 벡터, BM25 희소 벡터, 텍스트를
담고 있어, 한 번의 호출로 보내면 `RAG_TIMEOUT_SECONDS` 안에 수 메가바이트를 보내야 합니다. 배치로
나누면 큰 파일 처리 도중 실패하더라도 문서 전체를 잃지 않고 앞선 배치는 커밋된 채로 남습니다.

> **`EMBED_BATCH_SIZE`와 Ollama에 관한 참고:** 이 값은 네이티브 배치 입력을 지원하는 제공자
> (OpenAI 호환 `/v1/embeddings`)에서만 효과가 있습니다. Ollama의 `/api/embeddings`는 한 번에
> 프롬프트 하나만 받으므로, 기본 제공자에서는 이 값을 무엇으로 설정하든 청크를 HTTP 호출 한 번에
> 하나씩 임베딩합니다 — 값을 올려도 아무 변화가 없습니다.

## 실행

```bash
sudo ./deploy/install.sh
```

[`deploy/install.sh`](../deploy/install.sh)는 다음을 설치하고 서비스를 시작합니다.

| 구성 요소 | 위치 |
|------|------|
| 애플리케이션 / Python 가상환경 | `/opt/rag-mcp/app`, `/opt/rag-mcp/venv` |
| Qdrant 바이너리 | `/opt/rag-mcp/qdrant/qdrant` (버전은 `QDRANT_VERSION`으로 지정, 기본 `v1.12.4`) |
| 설정 파일 | `/etc/rag-mcp/rag-mcp.env` (없을 때만 `.env.example`에서 생성, 권한 `640 root:rag-mcp`) |
| 데이터 | `/var/lib/rag-mcp/{knowledge,qdrant,fastembed_cache}` |
| systemd 서비스 | `qdrant.service`, `rag-mcp.service` ([`deploy/systemd/`](../deploy/systemd)) |
| 수집 명령 | `/usr/local/bin/rag-ingest` |

모든 프로세스는 시스템 사용자 `rag-mcp`로 실행되며, systemd 유닛은 `ProtectSystem=strict` 등으로
쓰기 가능한 경로를 데이터 디렉터리로 제한합니다. Qdrant는 `127.0.0.1`에만 바인드되므로 외부에서
직접 접근할 수 없습니다.

이제 MCP 엔드포인트를 `http://<서버 주소>:${MCP_PORT:-8084}/mcp`(streamable-http)에서 사용할 수
있습니다. MCP 클라이언트에 연결하는 방법은 최상위 [README](../README.md)를 참고하세요.

운영 명령:

```bash
systemctl status qdrant rag-mcp          # 상태
journalctl -u rag-mcp -f                 # 로그
sudo systemctl restart rag-mcp           # 설정 변경 적용
sudo ./deploy/install.sh                 # 업그레이드 (설정·데이터 유지)
sudo ./deploy/uninstall.sh [--purge]     # 제거 (--purge: 설정·데이터까지 삭제)
```

> **설정 파일 형식 주의:** systemd의 `EnvironmentFile`은 `KEY=value  # 주석`처럼 같은 줄 끝의
> 주석을 값의 일부로 읽습니다. 주석은 항상 별도 줄에 쓰세요.

> **최초 시작과 네트워크:** FastEmbed가 BM25 모델을 `huggingface.co`에서 한 번 내려받아
> `/var/lib/rag-mcp/fastembed_cache`에 저장합니다. 프록시가 필요하면 설정 파일에 `HTTPS_PROXY`를
> 넣으세요. 내려받지 못하면 하이브리드 검색 없이 밀집 전용으로 동작합니다(로그에
> `FastEmbed unavailable` 경고).

## 설정 (환경 변수)

| 변수 | 기본값 | 비고 |
|-----|---------|-------|
| `QDRANT_URL` | `http://localhost:6333` | Qdrant REST 엔드포인트 |
| `QDRANT_COLLECTION` | `rag_kb` | 컬렉션 이름 |
| `QDRANT_API_KEY` | _(미설정)_ | Qdrant 인증을 켠 경우 |
| `EMBEDDINGS_PROVIDER` | `ollama` | `ollama` 또는 `openai` (OpenAI 호환) |
| `EMBEDDINGS_MODEL` | `bge-m3` | 임베딩 모델 (다국어) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | `ollama` 제공자 호스트 |
| `EMBEDDINGS_BASE_URL` | `https://api.openai.com` | `openai` 제공자 기본 URL (예: LiteLLM 프록시) |
| `EMBEDDINGS_API_KEY` | _(미설정)_ | `openai` 제공자 키 |
| `EMBED_QUERY_PREFIX` / `EMBED_DOC_PREFIX` | 자동 (모델에 따라 설정: nomic → `search_query: `/`search_document: `, 대칭 모델 → 빈 값) | 자동 감지가 놓치는 비대칭 모델 계열에만 재정의 (e5/bge → `query: `/`passage: `) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1500` / `100` | 수집 시 청킹 |
| `EMBED_BATCH_SIZE` | `32` | 임베딩 요청당 청크 수. `ollama` 제공자에는 효과 없음 (단일 프롬프트 API) |
| `QDRANT_UPSERT_BATCH` | `64` | 수집 중 Qdrant 업서트 요청당 포인트 수 |
| `RAG_TIMEOUT_SECONDS` | `30` (서버의 Qdrant 연결) / `60` (임베딩 요청, 수집, 기록) | HTTP 타임아웃 (초). 설정하면 모두 이 값을 사용 |
| `RAG_HYBRID` | `true` | 하이브리드 검색(밀집 + BM25) 켜기/끄기. 바꾸면 `--recreate` 필요 |
| `RAG_SPARSE_MODEL` | `Qdrant/bm25` | FastEmbed 희소(BM25) 모델 |
| `RAG_DEFAULT_LIMIT` / `RAG_MAX_LIMIT` | `5` / `20` | 검색 결과 개수 상한 |
| `RAG_SNIPPET_CHARS` | `1200` | 검색 결과마다 반환하는 텍스트의 최대 글자 수 (넘으면 `truncated: true`) |
| `RERANK_PROVIDER` | `none` | 리랭커 제공자: `none`(비활성) 또는 `cohere`(Cohere/Jina 호환 `/rerank`) |
| `RERANK_MODEL` | `rerank-multilingual-v3.0` | 리랭커 모델 (Cohere 다국어). Jina는 `jina-reranker-v2-base-multilingual` |
| `RERANK_BASE_URL` | `https://api.cohere.com` | 리랭커 API 기본 URL (Jina는 `https://api.jina.ai/v1`) |
| `RERANK_API_KEY` | _(미설정)_ | 리랭커 제공자 API 키 |
| `RERANK_CANDIDATES` | `30` | 리랭킹 전에 가져오는 후보 수 |
| `RERANK_TIMEOUT` | `30` | 리랭커 HTTP 타임아웃 (초) |
| `MCP_PORT` | `8084` | 서버 포트 |
| `MCP_HOST` | `0.0.0.0` | 서버 바인드 주소 |
| `RAG_KNOWLEDGE_DIR` | `/var/lib/rag-mcp/knowledge` | `rag-ingest` 명령이 수집할 문서 디렉터리 |
| `RAG_INTERNAL_TOKEN` | _(비어 있음)_ | 내부 쓰기 API(`/internal/knowledge/*`) 보호 토큰. 비워 두면 열림 (개발용) |

### 호스팅 / OpenAI 호환 임베딩 제공자 사용하기

```bash
EMBEDDINGS_PROVIDER=openai \
EMBEDDINGS_BASE_URL=https://api.openai.com \   # 또는 LiteLLM 프록시 등
EMBEDDINGS_API_KEY=sk-... \
EMBEDDINGS_MODEL=text-embedding-3-small \       # 대칭 모델 → 접두사가 자동으로 비워지므로 설정 불필요
  python ingest.py --path knowledge
```

질의도 같은 방식으로 임베딩되도록 `rag-mcp` 서비스에도 똑같은 변수를 설정하세요.

## 한국어 / 다국어 문서

기본 임베딩 모델은 한국어를 포함한 다국어 모델 **`bge-m3`**(1024차원, 최대 입력 8192 토큰)입니다.
영어 위주로 학습된 이전 기본값 `nomic-embed-text`(768차원)보다 한국어 질의·문서의 의미 검색이
정확합니다. 영어 문서만 쓰고 더 가벼운 모델을 원하면 `EMBEDDINGS_MODEL=nomic-embed-text`로 바꿀 수
있습니다(이 경우 접두사는 자동으로 붙습니다).

- **차원 자동 처리** — 컬렉션은 실제 임베딩 길이로 생성되므로(`ingest.py`, `capture.py`) 1024차원에
  자동으로 맞춰집니다.
- **접두사 불필요** — bge-m3는 질의/문서 접두사가 필요 없습니다. 자동 감지는 `nomic`에만 접두사를
  붙이므로 기본값(빈 값) 그대로 두고, `EMBED_QUERY_PREFIX`/`EMBED_DOC_PREFIX`는 설정하지 마세요.

### Ollama로 설정하기 (오프라인, 권장)

```bash
ollama pull bge-m3
```

```bash
# /etc/rag-mcp/rag-mcp.env
EMBEDDINGS_PROVIDER=ollama
EMBEDDINGS_MODEL=bge-m3
```

### OpenAI 호환 서버로 설정하기 (예: TEI)

GPU 서버에서 Hugging Face TEI(Text Embeddings Inference)로 `BAAI/bge-m3`를 서빙하는 경우:

```bash
# /etc/rag-mcp/rag-mcp.env
EMBEDDINGS_PROVIDER=openai
EMBEDDINGS_BASE_URL=http://<tei-host>:8080   # /v1 은 자동으로 붙습니다
EMBEDDINGS_API_KEY=dummy                     # 인증이 없으면 아무 값
EMBEDDINGS_MODEL=BAAI/bge-m3
```

### 적용 및 확인

모델을 바꾸면 반드시 컬렉션을 재구축해야 합니다(기존 벡터와 차원·의미가 다릅니다). 이전 기본값
`nomic-embed-text`로 만든 기존 컬렉션도 마찬가지입니다.

```bash
sudo systemctl restart rag-mcp               # 새 모델로 질의하도록 재시작
sudo rag-ingest --recreate                   # 컬렉션 재생성 + 재수집

# 컬렉션 차원 확인 → "size":1024 이면 성공
curl -s http://localhost:6333/collections/rag_kb | grep -o '"size":[0-9]*'
```

그다음 `rag_health()`에서 모델이 `bge-m3`로 표시되는지 확인하고, 한국어 질의로 `rag_search`를
실행해 보세요.

### 참고 사항

- **BM25는 여전히 한국어에 약합니다.** 키워드 검색(`Qdrant/bm25`)은 영어 기준으로 토큰을 나누므로
  조사가 붙은 형태("볼륨이", "볼륨을")를 서로 다른 단어로 취급합니다. RRF 결합에서 dense 검색이
  상당 부분 보완하지만, 결과가 이상하면 `RAG_HYBRID=false`(dense 전용)와 비교해 보세요 — 이 값을
  바꿀 때도 `--recreate`가 필요합니다. 영어 토큰(`CrashLoopBackOff`, 리소스 이름 등)은 계속 잘
  검색됩니다.
- **리랭커도 다국어 모델이 기본값입니다.** 리랭킹을 켜면(`RERANK_PROVIDER=cohere`) 기본 모델은 Cohere
  `rerank-multilingual-v3.0`입니다. Jina를 쓴다면 `jina-reranker-v2-base-multilingual`을 지정하세요.
  영어 전용 `rerank-english-v3.0`은 한국어 문서에 쓰지 마세요.
- **속도.** bge-m3(약 1.2GB)는 nomic보다 커서 CPU에서는 수집이 느려질 수 있습니다. Ollama는 청크를
  하나씩 임베딩하므로 문서가 많으면 시간이 걸립니다.
- **청크 크기.** 기본 `CHUNK_SIZE=1500`자는 bge-m3의 최대 입력 길이보다 훨씬 작아 그대로 써도
  됩니다.
