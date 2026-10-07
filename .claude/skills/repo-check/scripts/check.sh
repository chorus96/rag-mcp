#!/usr/bin/env bash
# rag-mcp 저장소 점검 스크립트. 저장소 최상위에서 실행하세요:
#   bash .claude/skills/repo-check/scripts/check.sh
#
# 각 항목을 PASS / WARN / FAIL / SKIP 으로 출력하고, FAIL이 하나라도 있으면 종료 코드 1을 돌려줍니다.
set -uo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

FAILS=0
WARNS=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; WARNS=$((WARNS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILS=$((FAILS + 1)); }
skip() { printf '  \033[90mSKIP\033[0m  %s\n' "$*"; }
section() { printf '\n\033[1m[%s]\033[0m\n' "$*"; }

# --- 1. 파이썬 ------------------------------------------------------------------
section "1. 파이썬"
if python3 -m py_compile tools/*.py 2>/tmp/repo-check-compile.$$; then
    pass "tools/*.py 문법"
else
    fail "tools/*.py 문법 오류: $(cat /tmp/repo-check-compile.$$)"
fi
rm -f /tmp/repo-check-compile.$$

if python3 -c 'import pytest' 2>/dev/null; then
    out=$(python3 -m pytest -q tests 2>&1 | tail -1)
    if python3 -m pytest -q tests >/dev/null 2>&1; then
        pass "테스트: $out"
    else
        fail "테스트 실패: $out"
    fi
else
    skip "pytest가 없어 테스트를 건너뜀 (python3 -m pip install pytest -r requirements.txt)"
fi

# --- 2. 배포 스크립트 -----------------------------------------------------------------
section "2. 배포 스크립트"
for f in deploy/install.sh deploy/uninstall.sh; do
    if bash -n "$f" 2>/dev/null; then pass "$f 문법"; else fail "$f 문법 오류"; fi
done
if sh -n deploy/rag-ingest 2>/dev/null; then pass "deploy/rag-ingest 문법"; else fail "deploy/rag-ingest 문법 오류"; fi

# install.sh가 복사하는 파일 목록과 tools/ 의 실제 파일이 같은지
listed=$(grep -oE 'tools/\{[^}]+\}\.py' deploy/install.sh | sed -E 's|tools/\{([^}]+)\}\.py|\1|' | tr ',' '\n' | sort)
actual=$(ls tools/*.py | xargs -n1 basename | sed 's/\.py$//' | sort)
if [ "$listed" = "$actual" ]; then
    pass "install.sh 복사 목록 = tools/*.py"
else
    fail "install.sh 복사 목록과 tools/*.py 가 다름 (install.sh: $(echo $listed) / tools: $(echo $actual))"
fi

# rag-ingest 템플릿의 자리 표시자를 install.sh가 모두 채우는지
for ph in $(grep -oE '@[A-Z_]+@' deploy/rag-ingest | sort -u); do
    if grep -q "s|$ph|" deploy/install.sh; then pass "rag-ingest 자리 표시자 $ph 처리됨"
    else fail "rag-ingest 자리 표시자 $ph 를 install.sh가 채우지 않음"; fi
done

# --- 3. 설정과 문서의 환경 변수 -----------------------------------------------------------
section "3. 환경 변수"
# 코드가 읽는 변수 (EMBEDDINGS_PROVIDER는 지원 값이 하나뿐이라 문서화하지 않음)
code_vars=$(grep -ohE 'os\.environ\.get\("[A-Z_]+' tools/*.py | sed 's/.*"//' | sort -u | grep -vx EMBEDDINGS_PROVIDER)
missing_env=""; missing_doc=""
for v in $code_vars; do
    grep -qE "^#? ?$v=" .env.example || missing_env="$missing_env $v"
    grep -q "\`$v\`" docs/DESIGN.md || missing_doc="$missing_doc $v"
done
[ -z "$missing_env" ] && pass ".env.example 에 코드의 환경 변수가 모두 있음" || fail ".env.example 에 없는 변수:$missing_env"
[ -z "$missing_doc" ] && pass "docs/DESIGN.md 에 코드의 환경 변수가 모두 있음" || fail "docs/DESIGN.md 에 없는 변수:$missing_doc"

inline=$(grep -nE '^[A-Z_]+=.*#' .env.example || true)
[ -z "$inline" ] && pass ".env.example 에 같은 줄 끝 주석 없음 (systemd 규칙)" || fail ".env.example 같은 줄 끝 주석: $inline"

n=$(grep -c '^RAG_KNOWLEDGE_DIR=' .env.example)
[ "$n" = 1 ] && pass ".env.example 의 RAG_KNOWLEDGE_DIR= 줄이 1개 (사용자 모드 설치가 고쳐 씀)" \
            || fail ".env.example 의 RAG_KNOWLEDGE_DIR= 줄이 $n 개 (정확히 1개여야 함)"

if (set -a; . ./.env.example) 2>/dev/null; then pass ".env.example 을 셸로 불러올 수 있음"
else fail ".env.example 을 셸로 불러올 수 없음"; fi

# --- 4. Claude Code 플러그인 -------------------------------------------------------------
section "4. Claude Code 플러그인"
for j in .claude-plugin/marketplace.json plugins/rag-mcp/.claude-plugin/plugin.json plugins/rag-mcp/.mcp.json; do
    if python3 -m json.tool "$j" >/dev/null 2>&1; then pass "$j JSON 형식"; else fail "$j JSON 형식 오류"; fi
done
if command -v claude >/dev/null 2>&1; then
    for target in . plugins/rag-mcp; do
        if claude plugin validate --strict "$target" >/dev/null 2>&1; then pass "claude plugin validate --strict $target"
        else fail "claude plugin validate --strict $target (직접 실행해 오류 확인)"; fi
    done
else
    skip "claude CLI가 없어 플러그인 검증을 건너뜀"
fi
# 플러그인 파일을 고쳤는데 버전을 올리지 않았는지 (커밋 전 변경 기준)
if ! git diff --quiet HEAD -- plugins/rag-mcp 2>/dev/null; then
    if git diff HEAD -- plugins/rag-mcp/.claude-plugin/plugin.json | grep -qE '^[+-][^+-].*"version"'; then
        pass "플러그인 변경과 함께 version 이 바뀜"
    else
        warn "plugins/rag-mcp 가 바뀌었지만 plugin.json 의 version 은 그대로 (사용자가 /plugin update 로 받지 못함)"
    fi
fi

# --- 5. 문서 링크 ----------------------------------------------------------------------
section "5. 문서 링크"
python3 - <<'PY'
import os, re, sys
fails = 0
def slug(h):
    s = re.sub(r"[^\w\- ]", "", h.strip().lower())
    return s.replace(" ", "-")
def heads(path):
    return {slug(m) for m in re.findall(r"^#+ (.+)$", open(path, encoding="utf-8").read(), re.M)}
for doc in ["README.md", "docs/DESIGN.md", "plugins/rag-mcp/skills/rag-knowledge/SKILL.md"]:
    text = open(doc, encoding="utf-8").read()
    text = re.sub(r"```.*?```", "", text, flags=re.S)  # 코드 블록 안은 무시
    base = os.path.dirname(doc)
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
            continue
        path, _, anchor = target.partition("#")
        dest = os.path.normpath(os.path.join(base, path)) if path else doc
        if not os.path.exists(dest):
            bad.append(f"{target} (파일 없음)")
        elif anchor and dest.endswith(".md") and anchor not in heads(dest):
            bad.append(f"{target} (섹션 없음)")
    if bad:
        fails += 1
        print(f"  \033[31mFAIL\033[0m  {doc}: " + ", ".join(bad))
    else:
        print(f"  \033[32mPASS\033[0m  {doc} 의 문서 안 링크")
sys.exit(1 if fails else 0)
PY
[ $? -eq 0 ] || FAILS=$((FAILS + 1))

# --- 6. 남은 흔적과 비밀 값 -------------------------------------------------------------------
section "6. 남은 흔적 / 비밀 값"
# 제거한 기능(Docker, Ollama)의 흔적
leftover=$(git grep -nIiE 'ollama|docker-compose|docker compose|host\.docker\.internal|Dockerfile' -- . ':!.claude/skills/repo-check' || true)
[ -z "$leftover" ] && pass "Docker / Ollama 흔적 없음" || warn "제거한 기능의 흔적:
$leftover"
# 루트에 파이썬 파일이 다시 생기지 않았는지
root_py=$(ls ./*.py 2>/dev/null || true)
[ -z "$root_py" ] && pass "저장소 최상위에 .py 파일 없음 (소스는 tools/)" || warn "최상위에 .py 파일: $root_py"
# 커밋된 파일에 실제 키처럼 보이는 값
secrets=$(git grep -nIE 'sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----' -- . ':!.claude/skills/repo-check' || true)
[ -z "$secrets" ] && pass "키처럼 보이는 값 없음" || fail "비밀 값으로 보이는 문자열:
$secrets"
tracked_env=$(git ls-files | grep -E '(^|/)\.env($|\.)' | grep -v '\.env\.example$' || true)
[ -z "$tracked_env" ] && pass ".env 파일이 커밋되지 않음" || fail "커밋된 .env 파일: $tracked_env"

# --- 요약 -------------------------------------------------------------------------------
printf '\n결과: FAIL %d, WARN %d\n' "$FAILS" "$WARNS"
[ "$FAILS" -eq 0 ]
