---
name: rag-delete-document
description: rag MCP 서버의 rag_delete_document 도구로 지식 베이스의 초안 문서(draft/)를 삭제합니다. 사용자가 "그 초안 지워 줘", "잘못 저장한 문서 삭제해 줘"처럼 문서 삭제를 명시적으로 요청할 때만 사용하세요.
---

# 초안 문서 삭제 (rag-mcp)

`rag` MCP 서버의 `rag_delete_document` 도구로 **초안 폴더 `draft/`**의 문서를 삭제합니다. 문서 파일과 검색용
청크를 함께 지우며, **되돌릴 수 없습니다.** 정식 문서(`official/`)는 지울 수 없습니다.

> 이 도구는 서버 운영자가 `RAG_MCP_WRITE=true`로 켰을 때만 있습니다. 도구 목록에 없으면 꺼져 있는 것이니,
> 삭제할 수 없다고 알리고 서버 관리자에게 요청하도록 안내하세요.

## 도구

`rag_delete_document(source)`

| 인자 | 설명 |
|------|------|
| `source` | 문서 디렉터리 기준 경로. `draft/`로 시작해야 함 (예: `draft/longhorn-볼륨-복구.md`) |

성공 응답: `{"status": "ok", "source", "file_deleted", "chunks_deleted"}`

## 순서

1. **요청 확인** — 사용자가 삭제를 명시적으로 요청했을 때만 진행하세요.
2. **대상 찾기** — 삭제할 문서의 `source`를 찾습니다.
   - 초안 목록: `rag_list_documents(folder="draft")` — 제목, 유형, 수정 시각을 함께 볼 수 있습니다.
   - 내용으로 찾기: `search_draft`로 초안만 검색합니다(결과의 `source`는 모두 `draft/`로 시작).
3. **대상이 정식 문서이면 멈추세요.** `source`가 `draft/`로 시작하지 않으면 지울 수 없습니다. 정식 문서의 삭제는
   서버 관리자가 직접 해야 한다고 안내하세요.
4. **사용자 확인** — 지울 문서의 `source`와 `title`을 보여 주고 **"이 문서가 맞습니까?"라고 확인받은 뒤에**
   삭제하세요. 비슷한 문서가 여러 개면 어느 것인지 물어보세요.
5. **삭제** — 한 번에 하나씩 `rag_delete_document(source)`를 호출합니다. 여러 문서를 지워 달라는 요청이면 목록을
   보여 주고 한 번 확인받은 뒤, 하나씩 지우며 결과를 모으세요.
6. **결과 안내** — 응답의 `file_deleted`(파일 삭제 여부)와 `chunks_deleted`(지운 청크 수)를 알려 주세요.

## 결과 해석

| 응답 | 의미 |
|------|------|
| `file_deleted: true`, `chunks_deleted > 0` | 파일과 검색용 청크를 모두 지움 (보통의 경우) |
| `file_deleted: false`, `chunks_deleted > 0` | 파일은 이미 없었고, 남아 있던 청크만 정리함 |
| `file_deleted: true`, `chunks_deleted: 0` | 아직 색인되지 않은 파일이었음 |

## 오류 대응

| 오류 | 대응 |
|------|------|
| `only documents under draft/ can be deleted via MCP` | 정식 문서이거나 `draft/` 밖의 경로입니다. 지울 수 없으니 서버 관리자에게 요청하도록 안내하세요. |
| `no document found at ...` | 파일도 청크도 없습니다. `rag_list_documents(folder="draft")`로 `source`를 다시 확인하세요. |
| `only .md or .pdf documents can be deleted` | 문서 파일이 아닙니다. 경로를 다시 확인하세요. |
| `deleting chunks failed ... (the file was not deleted)` | 청크 삭제에 실패해 파일도 그대로 두었습니다. `rag_health()`로 Qdrant 상태를 확인한 뒤 다시 시도하세요. |

## 하지 말아야 할 것

- 사용자의 확인 없이 지우지 마세요. 삭제는 되돌릴 수 없습니다.
- `source`를 짐작해서 지우지 마세요. 목록이나 검색 결과에서 확인한 값만 쓰세요.
- 정식 문서를 지우려고 다른 경로를 시도하지 마세요.
