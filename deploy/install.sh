#!/usr/bin/env bash
# rag-mcp 로컬 서버 설치 스크립트 (Linux + systemd).
#
#   sudo ./deploy/install.sh          # 시스템 모드: 전용 사용자 rag-mcp, 시스템 서비스
#   ./deploy/install.sh --user        # 사용자 모드: root 없이 현재 사용자 홈에 설치,
#                                     #             사용자 서비스(systemctl --user)
#
# 하는 일:
#   1. (시스템 모드) 시스템 사용자 rag-mcp 생성
#   2. 애플리케이션을 복사하고 Python 가상환경에 의존성 설치
#   3. Qdrant 바이너리 설치
#   4. 설정 파일 rag-mcp.env 생성 (이미 있으면 유지)
#   5. 샘플 문서를 문서 디렉터리에 복사 (비어 있을 때만)
#   6. systemd 서비스(qdrant, rag-mcp) 등록 후 시작, 수집 명령 rag-ingest 설치
#
#                   시스템 모드                     사용자 모드
#   앱 / venv       /opt/rag-mcp/{app,venv}         ~/.local/share/rag-mcp/{app,venv}
#   Qdrant          /opt/rag-mcp/qdrant             ~/.local/share/rag-mcp/qdrant
#   설정 파일       /etc/rag-mcp/rag-mcp.env        ~/.config/rag-mcp/rag-mcp.env
#   데이터          /var/lib/rag-mcp                ~/.local/share/rag-mcp/data
#   서비스          /etc/systemd/system             ~/.config/systemd/user
#   수집 명령       /usr/local/bin/rag-ingest       ~/.local/bin/rag-ingest
#
# 다시 실행하면 업그레이드로 동작합니다: 코드와 의존성을 갱신하고 서비스를 재시작하며,
# 설정 파일과 데이터(문서, Qdrant 저장소)는 건드리지 않습니다.
#
# 환경 변수로 동작을 바꿀 수 있습니다:
#   PYTHON=python3.12        사용할 Python 인터프리터 (3.10 이상, 기본값 python3)
#   QDRANT_VERSION=v1.12.4   설치할 Qdrant 버전
#   SKIP_START=1             서비스를 등록만 하고 시작하지 않음
set -euo pipefail

PYTHON=${PYTHON:-python3}
QDRANT_VERSION=${QDRANT_VERSION:-v1.12.4}
SKIP_START=${SKIP_START:-0}

MODE=system
case "${1:-}" in
    "") ;;
    --user) MODE=user ;;
    -h|--help) sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 옵션: $1 (사용법: $0 [--user])" >&2; exit 1 ;;
esac

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m주의:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m오류:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$MODE" = system ]; then
    RUN_USER=rag-mcp
    PREFIX=/opt/rag-mcp
    DATA_DIR=/var/lib/rag-mcp
    CONF_DIR=/etc/rag-mcp
    UNIT_DIR=/etc/systemd/system
    UNIT_SRC=$SRC_DIR/deploy/systemd
    BIN_DIR=/usr/local/bin
    SYSTEMCTL=(systemctl)
    SUDO=sudo
else
    RUN_USER=""
    PREFIX=$HOME/.local/share/rag-mcp
    DATA_DIR=$PREFIX/data
    CONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/rag-mcp
    UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
    UNIT_SRC=$SRC_DIR/deploy/systemd/user
    BIN_DIR=$HOME/.local/bin
    SYSTEMCTL=(systemctl --user)
    SUDO=""
fi
ENV_FILE=$CONF_DIR/rag-mcp.env

# 사용자 유닛은 %h/.config/... 경로를 쓰므로 XDG_CONFIG_HOME을 바꾼 환경은 지원하지 않습니다.
if [ "$MODE" = user ] && [ "$CONF_DIR" != "$HOME/.config/rag-mcp" ]; then
    die "사용자 모드는 XDG_CONFIG_HOME=$HOME/.config 일 때만 지원합니다"
fi

# --- 사전 확인 ----------------------------------------------------------------
if [ "$MODE" = system ]; then
    [ "$(id -u)" -eq 0 ] || die "root 권한이 필요합니다: sudo $0  (root 없이 설치하려면: $0 --user)"
else
    [ "$(id -u)" -ne 0 ] || die "사용자 모드는 일반 사용자로 실행하세요 (root는 시스템 모드: sudo $0)"
fi
command -v systemctl >/dev/null || die "systemd가 필요합니다 (systemctl을 찾을 수 없음)"
command -v curl >/dev/null || die "curl이 필요합니다"
command -v tar >/dev/null || die "tar가 필요합니다"
command -v "$PYTHON" >/dev/null || die "$PYTHON 을 찾을 수 없습니다 (PYTHON=... 으로 지정하세요)"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 이상이 필요합니다 (현재: $("$PYTHON" -V 2>&1))"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
    || die "Python venv 모듈이 없습니다. Debian/Ubuntu: apt install python3-venv (관리자 권한 필요)"
if [ "$MODE" = user ] && [ "$SKIP_START" != "1" ]; then
    "${SYSTEMCTL[@]}" show-environment >/dev/null 2>&1 || die \
"사용자 systemd에 연결할 수 없습니다. ssh 등으로 해당 사용자에 직접 로그인한 세션에서 실행하세요
       (su/sudo -u 로 전환한 셸에서는 보통 동작하지 않습니다). 서비스 없이 설치만 하려면 SKIP_START=1"
fi

case "$(uname -m)" in
    x86_64)        QDRANT_ARCH=x86_64-unknown-linux-musl ;;
    aarch64|arm64) QDRANT_ARCH=aarch64-unknown-linux-musl ;;
    *) die "지원하지 않는 CPU 아키텍처입니다: $(uname -m)" ;;
esac

# --- 1. 사용자 / 디렉터리 -------------------------------------------------------------
if [ "$MODE" = system ]; then
    if ! id "$RUN_USER" >/dev/null 2>&1; then
        log "시스템 사용자 $RUN_USER 생성"
        useradd --system --home-dir "$DATA_DIR" --no-create-home \
            --shell /usr/sbin/nologin "$RUN_USER"
    fi
    install -d -m 755 "$PREFIX" "$PREFIX/app" "$PREFIX/qdrant"
    install -d -m 750 -o "$RUN_USER" -g "$RUN_USER" \
        "$DATA_DIR" "$DATA_DIR/qdrant" "$DATA_DIR/fastembed_cache"
    install -d -m 755 -o "$RUN_USER" -g "$RUN_USER" "$DATA_DIR/knowledge"
    install -d -m 750 -o root -g "$RUN_USER" "$CONF_DIR"
else
    install -d -m 755 "$PREFIX" "$PREFIX/app" "$PREFIX/qdrant" "$BIN_DIR" "$UNIT_DIR"
    install -d -m 700 "$DATA_DIR" "$DATA_DIR/qdrant" "$DATA_DIR/fastembed_cache" "$CONF_DIR"
    install -d -m 755 "$DATA_DIR/knowledge"
fi

# --- 2. 애플리케이션 + Python 의존성 ---------------------------------------------
log "애플리케이션 복사 → $PREFIX/app"
install -m 644 "$SRC_DIR"/{server,ingest,embeddings,capture,reranker,vectorstore}.py \
    "$SRC_DIR/requirements.txt" "$PREFIX/app/"

if [ ! -x "$PREFIX/venv/bin/python" ]; then
    log "Python 가상환경 생성 → $PREFIX/venv"
    "$PYTHON" -m venv "$PREFIX/venv"
fi
log "Python 의존성 설치"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/app/requirements.txt"

# --- 3. Qdrant -------------------------------------------------------------------
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

# --- 4. 설정 파일 ------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
    log "설정 파일 생성 → $ENV_FILE"
    if [ "$MODE" = system ]; then
        install -m 640 -o root -g "$RUN_USER" "$SRC_DIR/.env.example" "$ENV_FILE"
    else
        install -m 600 "$SRC_DIR/.env.example" "$ENV_FILE"
        # 문서 디렉터리를 홈 아래 경로로 바꿉니다.
        sed -i "s|^RAG_KNOWLEDGE_DIR=.*|RAG_KNOWLEDGE_DIR=$DATA_DIR/knowledge|" "$ENV_FILE"
    fi
else
    log "기존 설정 파일 유지: $ENV_FILE (새 항목은 $SRC_DIR/.env.example 과 비교하세요)"
fi

# --- 5. 샘플 문서 ------------------------------------------------------------------
if [ -z "$(ls -A "$DATA_DIR/knowledge")" ]; then
    log "샘플 문서 복사 → $DATA_DIR/knowledge"
    cp -r "$SRC_DIR/knowledge/." "$DATA_DIR/knowledge/"
    if [ "$MODE" = system ]; then
        chown -R "$RUN_USER:$RUN_USER" "$DATA_DIR/knowledge"
    fi
fi

# --- 6. systemd + 수집 명령 ---------------------------------------------------------
log "systemd 서비스 등록 → $UNIT_DIR"
install -m 644 "$UNIT_SRC/qdrant.service" "$UNIT_DIR/qdrant.service"
install -m 644 "$UNIT_SRC/rag-mcp.service" "$UNIT_DIR/rag-mcp.service"

log "수집 명령 설치 → $BIN_DIR/rag-ingest"
sed -e "s|@ENV_FILE@|$ENV_FILE|" \
    -e "s|@APP_DIR@|$PREFIX/app|" \
    -e "s|@VENV@|$PREFIX/venv|" \
    -e "s|@DATA_DIR@|$DATA_DIR|" \
    -e "s|@RUN_USER@|$RUN_USER|" \
    "$SRC_DIR/deploy/rag-ingest" > "$BIN_DIR/rag-ingest.tmp"
chmod 755 "$BIN_DIR/rag-ingest.tmp"
mv "$BIN_DIR/rag-ingest.tmp" "$BIN_DIR/rag-ingest"

# 사용자 모드에서 SKIP_START=1이면 사용자 systemd에 연결할 수 없을 수 있으므로 등록도 건너뜁니다
# (나중에 로그인 세션에서: systemctl --user daemon-reload && systemctl --user enable --now qdrant rag-mcp).
if [ "$MODE" = system ] || [ "$SKIP_START" != "1" ]; then
    "${SYSTEMCTL[@]}" daemon-reload
    "${SYSTEMCTL[@]}" enable qdrant.service rag-mcp.service >/dev/null
fi
if [ "$SKIP_START" != "1" ]; then
    log "서비스 시작"
    "${SYSTEMCTL[@]}" restart qdrant.service
    "${SYSTEMCTL[@]}" restart rag-mcp.service
fi

# --- 사용자 모드 안내 -------------------------------------------------------------------
if [ "$MODE" = user ]; then
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR 가 PATH에 없습니다. ~/.bashrc 등에 추가하세요: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
        warn "linger가 꺼져 있어 로그아웃하면 서비스가 멈추고, 재부팅 후 로그인해야 시작됩니다.
       계속 실행하려면 한 번 실행하세요: loginctl enable-linger $USER
       (배포판 설정에 따라 관리자 권한이 필요할 수 있습니다: sudo loginctl enable-linger $USER)"
    fi
fi

if [ "$MODE" = system ]; then
    STATUS_CMD="systemctl status qdrant rag-mcp"
    LOG_CMD="journalctl -u rag-mcp -f"
    RESTART_CMD="sudo systemctl restart rag-mcp"
else
    STATUS_CMD="systemctl --user status qdrant rag-mcp"
    LOG_CMD="journalctl --user -u rag-mcp -f"
    RESTART_CMD="systemctl --user restart rag-mcp"
fi

cat <<EOF

설치가 끝났습니다 ($MODE 모드).

다음 단계:
  1. 임베딩 모델 준비 (Ollama, 같은 서버에서 실행 중이어야 합니다):
       ollama pull bge-m3
  2. 필요하면 설정 수정 후 재시작:
       ${SUDO:+$SUDO }\${EDITOR:-vi} $ENV_FILE
       $RESTART_CMD
  3. 문서 색인:
       ${SUDO:+$SUDO }rag-ingest
  4. 상태 확인:
       $STATUS_CMD
       $LOG_CMD

MCP 엔드포인트: http://<이 서버 주소>:8084/mcp
EOF
