"""tools/promote.py — 초안 승격 명령 (rag-promote).

역할
  MCP 쓰기 도구로 추가된 초안(문서 디렉터리의 draft/ 아래)을 사람이 검토한 뒤 정식 폴더로 옮깁니다.
  draft/<경로> → <경로> 로 옮기고 바로 색인하며, 초안의 파일과 청크는 지웁니다.
  이 명령은 MCP 도구가 아닙니다. 모델은 정식 폴더에 쓸 수 없고, 승격은 사람만 할 수 있습니다.

실행
  설치한 서버에서는 rag-promote 명령(deploy/rag-promote)으로 실행합니다.
      rag-promote                                   초안 목록 보기
      rag-promote draft/runbooks/foo.md [...]       초안을 정식 폴더로 옮기기
      rag-promote --overwrite draft/runbooks/foo.md 정식 위치에 같은 이름의 문서가 있으면 바꾸기
"""

from __future__ import annotations

import argparse
import sys

import documents


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="rag-promote",
        description="Promote reviewed draft documents (draft/...) into the official knowledge folders.",
    )
    parser.add_argument("sources", nargs="*",
                        help="draft documents to promote, e.g. draft/runbooks/foo.md (none: list drafts)")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing official document with the same path")
    args = parser.parse_args()

    if not args.sources:
        drafts = documents.list_drafts()
        if not drafts:
            print("초안이 없습니다.")
            return 0
        print(f"초안 {len(drafts)}개 (rag-promote <source> 로 정식 폴더에 옮깁니다):")
        for d in drafts:
            print(f"  {d['source']}  →  {d['promote_to']}   ({d['title']})")
        return 0

    failed = 0
    for source in args.sources:
        result = documents.promote_document(source, overwrite=args.overwrite)
        if result["status"] == "ok":
            note = " (기존 문서를 바꿈)" if result["replaced"] else ""
            print(f"승격: {result['source']} → {result['target']}  [{result['chunks']}개 청크]{note}")
        else:
            failed += 1
            print(f"실패: {source}: {result['error']}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
