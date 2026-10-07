#!/usr/bin/env bash
# =============================================================================
# rag-mcp 설치 스크립트 (Linux + systemd, root 불필요)
# =============================================================================
#
# 사용법
#   ./deploy/install.sh          현재 사용자 홈에 설치하고 사용자 서비스(systemctl --user)로 실행
#   ./deploy/install.sh --help   이 안내 보기
#
#   root(sudo)로 실행하지 마세요. 설치하는 사용자 권한으로 서비스가 실행됩니다.
#
# 설치 순서
#   1. 디렉터리 준비
#   2. 애플리케이션 복사, Python 가상환경에 의존성 설치
#   3. Qdrant 바이너리 설치
#   4. 설정 파일 rag-mcp.env 생성 (이미 있으면 그대로 둠)
#   5. 샘플 문서 복사 (문서 디렉터리가 비어 있을 때만)
#   6. systemd 사용자 서비스(qdrant, rag-mcp) 등록·시작, 명령 rag-ingest·rag-promote 설치
#
# 설치 위치
#   앱 / venv    ~/.local/share/rag-mcp/{app,venv}
#   Qdrant       ~/.local/share/rag-mcp/qdrant
#   설정 파일    ~/.config/rag-mcp/rag-mcp.env
#   데이터       ~/.local/share/rag-mcp/data/{knowledge,qdrant,fastembed_cache}
#   서비스       ~/.config/systemd/user/{qdrant,rag-mcp}.service
#   명령         ~/.local/bin/rag-ingest (문서 수집), ~/.local/bin/rag-promote (초안 승격)
#
# 업그레이드
#   다시 실행하면 업그레이드로 동작합니다. 코드와 의존성을 갱신하고 서비스를 재시작하며,
#   설정 파일과 데이터(문서, Qdrant 저장소)는 건드리지 않습니다.
#
# 옵션 (환경 변수)
#   PYTHON=python3.12        사용할 Python (3.10 이상, 기본값 python3)
#   QDRANT_VERSION=v1.12.4   설치할 Qdrant 버전
#   SKIP_START=1             서비스를 등록·시작하지 않음 (사용자 systemd가 없는 셸에서 파일만 설치)
set -euo pipefail

PYTHON=${PYTHON:-python3}
QDRANT_VERSION=${QDRANT_VERSION:-v1.12.4}
SKIP_START=${SKIP_START:-0}

case "${1:-}" in
    # --user 는 예전 사용법과의 호환을 위해 받기만 합니다 (이제 항상 사용자 설치).
    ""|--user) ;;
    # 맨 위 주석 블록을 안내문으로 출력합니다.
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1 (사용법: $0 [--help])" >&2; exit 1 ;;
esac

# 저장소 최상위 디렉터리 (이 스크립트는 deploy/ 안에 있음)
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# 출력 도우미: 진행(파랑), 주의(노랑), 오류 후 종료(빨강)
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m주의:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m오류:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 경로 ---------------------------------------------------------------------------
PREFIX=$HOME/.local/share/rag-mcp
DATA_DIR=$PREFIX/data
CONF_DIR=$HOME/.config/rag-mcp
UNIT_DIR=$HOME/.config/systemd/user
BIN_DIR=$HOME/.local/bin
ENV_FILE=$CONF_DIR/rag-mcp.env
SYSTEMCTL=(systemctl --user)

# systemd 유닛은 %h/.config/... 경로를 고정으로 쓰므로, XDG_CONFIG_HOME을 바꾼 환경은 지원하지 않습니다.
if [ -n "${XDG_CONFIG_HOME:-}" ] && [ "$XDG_CONFIG_HOME" != "$HOME/.config" ]; then
    die "XDG_CONFIG_HOME=$HOME/.config 일 때만 지원합니다 (현재: $XDG_CONFIG_HOME)"
fi

# --- 사전 확인 ----------------------------------------------------------------------
# 필요한 명령이 있는지 먼저 확인하고, 없으면 해결 방법과 함께 멈춥니다.
[ "$(id -u)" -ne 0 ] || die "root(sudo)로 실행하지 마세요. 서비스를 실행할 일반 사용자로 실행하세요: $0"
command -v systemctl >/dev/null || die "systemd가 필요합니다 (systemctl을 찾을 수 없음)"
command -v curl >/dev/null || die "curl이 필요합니다"
command -v tar >/dev/null || die "tar가 필요합니다"
command -v "$PYTHON" >/dev/null || die "$PYTHON 을 찾을 수 없습니다 (PYTHON=... 으로 지정하세요)"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 이상이 필요합니다 (현재: $("$PYTHON" -V 2>&1))"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
    || die "Python venv 모듈이 없습니다. Debian/Ubuntu: apt install python3-venv (관리자에게 요청)"
# 서비스를 등록하려면 로그인 세션의 사용자 systemd에 연결할 수 있어야 합니다.
if [ "$SKIP_START" != "1" ]; then
    "${SYSTEMCTL[@]}" show-environment >/dev/null 2>&1 || die \
"사용자 systemd에 연결할 수 없습니다. ssh 등으로 해당 사용자에 직접 로그인한 세션에서 실행하세요
       (su/sudo -u 로 전환한 셸에서는 보통 동작하지 않습니다). 서비스 없이 설치만 하려면 SKIP_START=1"
fi

# Qdrant는 musl 정적 빌드를 받습니다. glibc 버전과 관계없이 대부분의 배포판에서 동작합니다.
case "$(uname -m)" in
    x86_64)        QDRANT_ARCH=x86_64-unknown-linux-musl ;;
    aarch64|arm64) QDRANT_ARCH=aarch64-unknown-linux-musl ;;
    *) die "지원하지 않는 CPU 아키텍처입니다: $(uname -m)" ;;
esac

# --- 1. 디렉터리 ---------------------------------------------------------------------
# 데이터와 설정(API 키 포함)은 본인만 접근할 수 있게 합니다(700).
install -d -m 755 "$PREFIX" "$PREFIX/app" "$PREFIX/qdrant" "$BIN_DIR" "$UNIT_DIR"
install -d -m 700 "$DATA_DIR" "$DATA_DIR/qdrant" "$DATA_DIR/fastembed_cache" "$CONF_DIR"
install -d -m 755 "$DATA_DIR/knowledge"

# --- 2. 애플리케이션 + Python 의존성 -------------------------------------------------
# 가상환경은 처음 한 번만 만들고, 업그레이드 때는 의존성만 갱신합니다.
log "애플리케이션 복사 → $PREFIX/app"
install -m 644 "$SRC_DIR"/tools/{server,ingest,embeddings,documents,promote,reranker,vectorstore}.py \
    "$SRC_DIR/requirements.txt" "$PREFIX/app/"

if [ ! -x "$PREFIX/venv/bin/python" ]; then
    log "Python 가상환경 생성 → $PREFIX/venv"
    "$PYTHON" -m venv "$PREFIX/venv"
fi
log "Python 의존성 설치"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/app/requirements.txt"

# --- 3. Qdrant ----------------------------------------------------------------------
# 설치한 버전을 VERSION 파일에 기록해 두고, 버전이 바뀌었을 때만 다시 내려받습니다.
if [ "$(cat "$PREFIX/qdrant/VERSION" 2>/dev/null || true)" != "$QDRANT_VERSION" ]; then
    log "Qdrant $QDRANT_VERSION 설치 → $PREFIX/qdrant"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL -o "$tmp/qdrant.tar.gz" \
        "https://github.com/qdrant/qdrant/releases/download/$QDRANT_VERSION/qdrant-$QDRANT_ARCH.tar.gz"
    tar -xzf "$tmp/qdrant.tar.gz" -C "$tmp"
    install -m 755 "$tmp/qdrant" "$PREFIX/qdrant/qdrant"
    echo "$QDRANT_VERSION" > "$PREFIX/qdrant/VERSION"
fi

# --- 4. 설정 파일 -------------------------------------------------------------------
# 이미 있으면 절대 덮어쓰지 않습니다 (운영자가 고친 값을 보존). API 키가 들어가므로 권한은 600.
if [ ! -f "$ENV_FILE" ]; then
    log "설정 파일 생성 → $ENV_FILE"
    install -m 600 "$SRC_DIR/.env.example" "$ENV_FILE"
    # systemd는 설정 파일 안의 ~ 나 $HOME 을 풀어 주지 않으므로, 문서 디렉터리를 실제 경로로 바꿉니다.
    sed -i "s|^RAG_KNOWLEDGE_DIR=.*|RAG_KNOWLEDGE_DIR=$DATA_DIR/knowledge|" "$ENV_FILE"
else
    log "기존 설정 파일 유지: $ENV_FILE (새 항목은 $SRC_DIR/.env.example 과 비교하세요)"
fi

# --- 5. 샘플 문서 -------------------------------------------------------------------
# 문서 디렉터리가 비어 있을 때만 복사해, 운영 중인 문서를 건드리지 않습니다.
if [ -z "$(ls -A "$DATA_DIR/knowledge")" ]; then
    log "샘플 문서 복사 → $DATA_DIR/knowledge"
    cp -r "$SRC_DIR/knowledge/." "$DATA_DIR/knowledge/"
fi

# --- 6. systemd 서비스 + 수집 명령 ---------------------------------------------------
log "systemd 사용자 서비스 등록 → $UNIT_DIR"
install -m 644 "$SRC_DIR/deploy/systemd/qdrant.service" "$UNIT_DIR/qdrant.service"
install -m 644 "$SRC_DIR/deploy/systemd/rag-mcp.service" "$UNIT_DIR/rag-mcp.service"

# 명령 템플릿(rag-ingest, rag-promote)의 @...@ 자리에 실제 경로를 채워 넣습니다.
# 임시 파일에 쓴 뒤 mv로 바꿔, 실행 중인 명령이 반쯤 쓰인 파일을 읽지 않게 합니다.
for cmd in rag-ingest rag-promote; do
    log "명령 설치 → $BIN_DIR/$cmd"
    sed -e "s|@ENV_FILE@|$ENV_FILE|" \
        -e "s|@APP_DIR@|$PREFIX/app|" \
        -e "s|@VENV@|$PREFIX/venv|" \
        -e "s|@DATA_DIR@|$DATA_DIR|" \
        "$SRC_DIR/deploy/$cmd" > "$BIN_DIR/$cmd.tmp"
    chmod 755 "$BIN_DIR/$cmd.tmp"
    mv "$BIN_DIR/$cmd.tmp" "$BIN_DIR/$cmd"
done

# 서비스 등록과 시작. SKIP_START=1이면 사용자 systemd에 연결할 수 없을 수 있으므로 모두 건너뜁니다.
# 나중에 로그인 세션에서 직접 등록하세요:
#   systemctl --user daemon-reload && systemctl --user enable --now qdrant rag-mcp
# 서비스는 start가 아니라 restart로 띄워, 업그레이드 때 새 코드가 바로 적용되게 합니다.
if [ "$SKIP_START" != "1" ]; then
    "${SYSTEMCTL[@]}" daemon-reload
    "${SYSTEMCTL[@]}" enable qdrant.service rag-mcp.service >/dev/null
    log "서비스 시작"
    "${SYSTEMCTL[@]}" restart qdrant.service
    "${SYSTEMCTL[@]}" restart rag-mcp.service
fi

# --- 추가 확인 ----------------------------------------------------------------------
# 수집 명령을 바로 쓸 수 있는지(PATH), 로그아웃 후에도 서비스가 도는지(linger) 알려 줍니다.
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR 가 PATH에 없습니다. ~/.bashrc 등에 추가하세요: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
    warn "linger가 꺼져 있어 로그아웃하면 서비스가 멈추고, 재부팅 후 로그인해야 시작됩니다.
       계속 실행하려면 한 번 실행하세요: loginctl enable-linger $USER
       (배포판 설정에 따라 관리자 권한이 필요할 수 있습니다: sudo loginctl enable-linger $USER)"
fi

# --- 마무리 안내 ------------------------------------------------------------------------
cat <<EOF

설치가 끝났습니다.

다음 단계:
  1. OpenAI 호환 임베딩 엔드포인트를 설정 파일에 지정한 뒤 재시작:
       (EMBEDDINGS_BASE_URL / EMBEDDINGS_API_KEY / EMBEDDINGS_MODEL)
       \${EDITOR:-vi} $ENV_FILE
       systemctl --user restart rag-mcp
  2. 문서 색인:
       rag-ingest
  3. 상태 확인:
       systemctl --user status qdrant rag-mcp
       journalctl --user -u rag-mcp -f

MCP 엔드포인트: http://<이 서버 주소>:8084/mcp
EOF
