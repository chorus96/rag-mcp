#!/usr/bin/env bash
# rag-mcp 로컬 서버 설치 스크립트 (Linux + systemd).
#
#   sudo ./deploy/install.sh
#
# 하는 일:
#   1. 시스템 사용자 rag-mcp 생성
#   2. 애플리케이션을 /opt/rag-mcp/app 에 복사하고 /opt/rag-mcp/venv 에 Python 의존성 설치
#   3. Qdrant 바이너리를 /opt/rag-mcp/qdrant 에 설치
#   4. 설정 파일 /etc/rag-mcp/rag-mcp.env 생성 (이미 있으면 유지)
#   5. 샘플 문서를 /var/lib/rag-mcp/knowledge 에 복사 (비어 있을 때만)
#   6. systemd 서비스(qdrant, rag-mcp) 등록 후 시작, 수집 명령 /usr/local/bin/rag-ingest 설치
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

RUN_USER=rag-mcp
PREFIX=/opt/rag-mcp
DATA_DIR=/var/lib/rag-mcp
CONF_DIR=/etc/rag-mcp
ENV_FILE=$CONF_DIR/rag-mcp.env
UNIT_DIR=/etc/systemd/system

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m오류:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 사전 확인 ----------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "root 권한이 필요합니다: sudo $0"
command -v systemctl >/dev/null || die "systemd가 필요합니다 (systemctl을 찾을 수 없음)"
command -v curl >/dev/null || die "curl이 필요합니다"
command -v tar >/dev/null || die "tar가 필요합니다"
command -v "$PYTHON" >/dev/null || die "$PYTHON 을 찾을 수 없습니다 (PYTHON=... 으로 지정하세요)"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 이상이 필요합니다 (현재: $("$PYTHON" -V 2>&1))"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
    || die "Python venv 모듈이 없습니다. Debian/Ubuntu: apt install python3-venv"

case "$(uname -m)" in
    x86_64)        QDRANT_ARCH=x86_64-unknown-linux-musl ;;
    aarch64|arm64) QDRANT_ARCH=aarch64-unknown-linux-musl ;;
    *) die "지원하지 않는 CPU 아키텍처입니다: $(uname -m)" ;;
esac

# --- 1. 사용자 ------------------------------------------------------------------
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
    install -m 640 -o root -g "$RUN_USER" "$SRC_DIR/.env.example" "$ENV_FILE"
else
    log "기존 설정 파일 유지: $ENV_FILE (새 항목은 $SRC_DIR/.env.example 과 비교하세요)"
fi

# --- 5. 샘플 문서 ------------------------------------------------------------------
if [ -z "$(ls -A "$DATA_DIR/knowledge")" ]; then
    log "샘플 문서 복사 → $DATA_DIR/knowledge"
    cp -r "$SRC_DIR/knowledge/." "$DATA_DIR/knowledge/"
    chown -R "$RUN_USER:$RUN_USER" "$DATA_DIR/knowledge"
fi

# --- 6. systemd + 수집 명령 ---------------------------------------------------------
log "systemd 서비스 등록"
install -m 644 "$SRC_DIR/deploy/systemd/qdrant.service" "$UNIT_DIR/qdrant.service"
install -m 644 "$SRC_DIR/deploy/systemd/rag-mcp.service" "$UNIT_DIR/rag-mcp.service"
install -m 755 "$SRC_DIR/deploy/rag-ingest" /usr/local/bin/rag-ingest
systemctl daemon-reload
systemctl enable qdrant.service rag-mcp.service >/dev/null

if [ "$SKIP_START" != "1" ]; then
    log "서비스 시작"
    systemctl restart qdrant.service
    systemctl restart rag-mcp.service
fi

cat <<EOF

설치가 끝났습니다.

다음 단계:
  1. 임베딩 모델 준비 (Ollama, 같은 서버에 설치되어 있어야 합니다):
       curl -fsSL https://ollama.com/install.sh | sh    # Ollama가 없다면
       ollama pull bge-m3
  2. 필요하면 설정 수정 후 재시작:
       sudo \${EDITOR:-vi} $ENV_FILE
       sudo systemctl restart rag-mcp
  3. 문서 색인:
       sudo rag-ingest
  4. 상태 확인:
       systemctl status qdrant rag-mcp
       journalctl -u rag-mcp -f

MCP 엔드포인트: http://<이 서버 주소>:8084/mcp
EOF
