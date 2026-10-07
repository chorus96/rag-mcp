#!/usr/bin/env bash
# =============================================================================
# rag-mcp 제거 스크립트
# =============================================================================
#
# 사용법
#   ./deploy/uninstall.sh            제거 (설정·문서·Qdrant 데이터는 남김)
#   ./deploy/uninstall.sh --purge    완전 삭제 (설정·데이터까지)
#   ./deploy/uninstall.sh --help     이 안내 보기
#
#   설치한 사용자로 실행하세요 (root 불필요).
#
# 지우는 것
#   항상:      systemd 사용자 서비스(qdrant, rag-mcp), 명령 rag-ingest·rag-promote,
#              애플리케이션·Python 가상환경·Qdrant 바이너리
#   --purge:   위에 더해 설정 파일(~/.config/rag-mcp)과
#              데이터(~/.local/share/rag-mcp/data: 문서, Qdrant 저장소, 캐시)
#
# --purge 없이 지우면 나중에 install.sh로 다시 설치했을 때 기존 설정과 데이터를 그대로 씁니다.
set -euo pipefail

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        # --user 는 예전 사용법과의 호환을 위해 받기만 합니다.
        --user) ;;
        # 맨 위 주석 블록을 안내문으로 출력합니다.
        -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
        *) echo "알 수 없는 옵션: $arg (사용법: $0 [--purge])" >&2; exit 1 ;;
    esac
done

[ "$(id -u)" -ne 0 ] || { echo "root(sudo)로 실행하지 마세요. 설치한 사용자로 실행하세요: $0" >&2; exit 1; }

# --- 경로 (install.sh와 같아야 합니다) ---------------------------------------------------
PREFIX=$HOME/.local/share/rag-mcp
DATA_DIR=$PREFIX/data
CONF_DIR=$HOME/.config/rag-mcp
UNIT_DIR=$HOME/.config/systemd/user
BIN_DIR=$HOME/.local/bin
SYSTEMCTL=(systemctl --user)

# --- 1. 서비스와 명령 ----------------------------------------------------------------
# 이미 지워졌거나 사용자 systemd에 연결할 수 없어도 나머지 정리는 계속합니다.
"${SYSTEMCTL[@]}" disable --now rag-mcp.service qdrant.service 2>/dev/null || true
rm -f "$UNIT_DIR/rag-mcp.service" "$UNIT_DIR/qdrant.service"
"${SYSTEMCTL[@]}" daemon-reload 2>/dev/null || true
rm -f "$BIN_DIR/rag-ingest" "$BIN_DIR/rag-promote"

# --- 2. 프로그램 (애플리케이션, 가상환경, Qdrant 바이너리) -----------------------------------
# 데이터가 $PREFIX/data 안에 있으므로 프로그램 부분만 골라 지웁니다.
rm -rf "$PREFIX/app" "$PREFIX/venv" "$PREFIX/qdrant"

# --- 3. 설정과 데이터 (--purge 일 때만) ---------------------------------------------------
if [ "$PURGE" = "1" ]; then
    rm -rf "$DATA_DIR" "$CONF_DIR"
    # 다른 파일이 없으면 빈 상위 디렉터리도 정리합니다.
    rmdir "$PREFIX" 2>/dev/null || true
    echo "rag-mcp를 데이터와 설정까지 모두 제거했습니다."
else
    echo "rag-mcp를 제거했습니다. 설정($CONF_DIR)과 데이터($DATA_DIR)는 남아 있습니다."
    echo "모두 지우려면: $0 --purge"
fi
