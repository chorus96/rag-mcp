#!/usr/bin/env bash
# rag-mcp 로컬 서버 제거 스크립트.
#
#   sudo ./deploy/uninstall.sh                 # 시스템 모드 제거 (설정·문서·Qdrant 데이터 유지)
#   sudo ./deploy/uninstall.sh --purge         # 설정, 문서, Qdrant 데이터, 사용자까지 모두 삭제
#   ./deploy/uninstall.sh --user               # 사용자 모드 제거 (설정·데이터 유지)
#   ./deploy/uninstall.sh --user --purge       # 사용자 모드의 설정·데이터까지 모두 삭제
set -euo pipefail

MODE=system
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --user) MODE=user ;;
        --purge) PURGE=1 ;;
        *) echo "알 수 없는 옵션: $arg (사용법: $0 [--user] [--purge])" >&2; exit 1 ;;
    esac
done

if [ "$MODE" = system ]; then
    [ "$(id -u)" -eq 0 ] || { echo "root 권한이 필요합니다: sudo $0 $*" >&2; exit 1; }
    RUN_USER=rag-mcp
    PREFIX=/opt/rag-mcp
    DATA_DIR=/var/lib/rag-mcp
    CONF_DIR=/etc/rag-mcp
    UNIT_DIR=/etc/systemd/system
    BIN=/usr/local/bin/rag-ingest
    SYSTEMCTL=(systemctl)
else
    [ "$(id -u)" -ne 0 ] || { echo "사용자 모드는 일반 사용자로 실행하세요" >&2; exit 1; }
    PREFIX=$HOME/.local/share/rag-mcp
    DATA_DIR=$PREFIX/data
    CONF_DIR=$HOME/.config/rag-mcp
    UNIT_DIR=$HOME/.config/systemd/user
    BIN=$HOME/.local/bin/rag-ingest
    SYSTEMCTL=(systemctl --user)
fi

"${SYSTEMCTL[@]}" disable --now rag-mcp.service qdrant.service 2>/dev/null || true
rm -f "$UNIT_DIR/rag-mcp.service" "$UNIT_DIR/qdrant.service"
"${SYSTEMCTL[@]}" daemon-reload 2>/dev/null || true
rm -f "$BIN"

if [ "$MODE" = system ]; then
    rm -rf "$PREFIX"
else
    # 사용자 모드는 데이터가 $PREFIX/data 안에 있으므로 프로그램 부분만 지웁니다.
    rm -rf "$PREFIX/app" "$PREFIX/venv" "$PREFIX/qdrant"
fi

if [ "$PURGE" = "1" ]; then
    rm -rf "$DATA_DIR" "$CONF_DIR"
    if [ "$MODE" = system ]; then
        if id "$RUN_USER" >/dev/null 2>&1; then
            userdel "$RUN_USER"
        fi
    else
        rmdir "$PREFIX" 2>/dev/null || true
    fi
    echo "rag-mcp를 데이터와 설정까지 모두 제거했습니다 ($MODE 모드)."
else
    echo "rag-mcp를 제거했습니다 ($MODE 모드). 설정($CONF_DIR)과 데이터($DATA_DIR)는 남아 있습니다."
    if [ "$MODE" = system ]; then echo "모두 지우려면: sudo $0 --purge"; else echo "모두 지우려면: $0 --user --purge"; fi
fi
