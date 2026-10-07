---
name: rag-list-documents
description: rag MCP 서버의 정식 문서(official/) 파일 목록을 보여 줍니다. "지식 베이스에 어떤 문서가 있어?", "런북 목록 보여 줘", "official 폴더에 뭐가 있지?", "이 문서가 색인됐나?"처럼 문서 구성이나 색인 상태를 물을 때 사용하세요.
---

# 정식 문서 목록 (rag-mcp)

`rag` MCP 서버의 `rag_list_documents` 도구로 문서 디렉터리의 **정식 문서(`official/`)** 파일 목록을 가져와
보여 줍니다. 내용을 찾는 검색이 아니라 **파일 목록**입니다. 내용에 대한 질문에는 `rag_search`(또는
`search_runbooks`)를 쓰세요.

## 도구

`rag_list_documents(subdir?, limit?)`

| 인자 | 설명 |
|------|------|
| `subdir` | 선택. `official/` 아래 하위 폴더만 볼 때 (예: `runbooks`). 생략하면 `official/` 전체 |
| `limit` | 선택. 돌려줄 최대 개수 (기본 100, 최대 500) |

응답의 `documents` 항목마다 다음 값이 있습니다.

| 필드 | 의미 |
|------|------|
| `source` | 문서 경로 (예: `official/runbooks/longhorn.md`). 검색 결과의 `source`와 같은 값 |
| `title` | 제목 (front matter의 `title`, 없으면 파일 이름) |
| `doc_type` | 문서 유형 (`runbook`, `rca`, `note` 등) |
| `size_bytes`, `modified` | 파일 크기, 마지막 수정 시각 (UTC) |
| `chunks` | 색인된 청크 수. **0이면 파일은 있지만 아직 색인 전이라 검색되지 않음.** `null`이면 색인 상태를 확인하지 못함 |

그 밖에 `total`(전체 개수), `returned`(돌려준 개수), `truncated`(잘렸는지)가 있습니다.

## 사용 순서

1. 사용자가 특정 분야만 물으면(예: "런북 목록") 먼저 전체 목록을 가져온 뒤 `doc_type`이나 경로로 추리세요.
   하위 폴더 이름을 이미 안다면 `subdir`를 넘겨도 됩니다.
2. 결과를 **표로** 보여 주세요. 기본 열은 `source`, `title`, `doc_type`, `modified`입니다. 사용자가 색인 상태를
   물었거나 `chunks`가 0인 문서가 있으면 `chunks` 열도 넣으세요.
3. 많으면 하위 폴더나 `doc_type`별로 묶어 개수와 함께 요약하고, 사용자가 원하면 전체를 보여 주세요.
4. `truncated: true`이면 `total` 중 일부만 보였다고 알리고, `subdir`나 더 큰 `limit`(최대 500)로 다시 조회할지
   물어보세요.

## 결과 해석

- **`chunks: 0`인 문서**는 검색에 나오지 않습니다. 서버 관리자가 `rag-ingest`를 실행해야 한다고 안내하세요.
- **`chunks: null`**이면 Qdrant에 연결하지 못한 것입니다. 목록은 맞지만 색인 상태는 모릅니다. 필요하면
  `rag_health()`로 확인하세요.
- **초안(`draft/`)은 이 목록에 없습니다.** 초안은 검색 결과의 `source`가 `draft/`로 시작하는 것으로 알 수 있고,
  서버 관리자는 `rag-promote` 명령(인자 없이 실행)으로 초안 목록을 볼 수 있습니다.

## 문제가 생겼을 때

| 응답 | 의미와 대응 |
|------|------|
| `folder not found` | 그 하위 폴더가 없습니다. `subdir` 없이 전체 목록을 다시 조회하세요. |
| `subdir must be a folder under official/` | `..` 등으로 `official/` 밖을 가리켰습니다. `official/` 아래 폴더 이름만 쓰세요. |
| `folder not found: official/` | 서버의 문서 디렉터리에 `official/` 폴더가 없습니다. 서버 관리자에게 알리세요. |
| 도구가 목록에 없음 | 서버가 이 도구가 없는 이전 버전입니다. 서버 업그레이드(`./deploy/install.sh` 다시 실행)를 안내하세요. |

## 하지 말아야 할 것

- 목록에 없는 문서를 있다고 말하지 마세요.
- 파일 목록만 보고 문서 내용을 추측하지 마세요. 내용은 `rag_search`로 확인하세요.
