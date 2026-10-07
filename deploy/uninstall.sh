#!/usr/bin/env bash
# =============================================================================
# rag-mcp 제거 스크립트
# =============================================================================
#
# 사용법
#   sudo ./deploy/uninstall.sh                시스템 모드 제거 (설정·문서·Qdrant 데이터는 남김)
#   sudo ./deploy/uninstall.sh --purge        시스템 모드 완전 삭제 (설정·데이터·rag-mcp 사용자까지)
#   ./deploy/uninstall.sh --user              사용자 모드 제거 (설정·데이터는 남김)
#   ./deploy/uninstall.sh --user --purge      사용자 모드 완전 삭제 (설정·데이터까지)
#   ./deploy/uninstall.sh --help              이 안내 보기
#
# 지우는 것
#   항상:      systemd 서비스(qdrant, rag-mcp), 수집 명령 rag-ingest,
#              애플리케이션·Python 가상환경·Qdrant 바이너리
#   --purge:   위에 더해 설정 파일, 데이터(문서, Qdrant 저장소, 캐시),
#              (시스템 모드) 시스템 사용자 rag-mcp
#
# --purge 없이 지우면 나중에 install.sh로 다시 설치했을 때 기존 설정과 데이터를 그대로 씁니다.
set -euo pipefail

MODE=system
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --user) MODE=user ;;
        --purge) PURGE=1 ;;
        # 맨 위 주석 블록을 안내문으로 출력합니다.
        -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
        *) echo "알 수 없는 옵션: $arg (사용법: $0 [--user] [--purge])" >&2; exit 1 ;;
    esac
done

# --- 모드별 경로 (install.sh와 같아야 합니다) -------------------------------------------
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

# --- 1. 서비스와 수집 명령 ----------------------------------------------------------------
# 이미 지워졌거나 사용자 systemd에 연결할 수 없어도 나머지 정리는 계속합니다.
"${SYSTEMCTL[@]}" disable --now rag-mcp.service qdrant.service 2>/dev/null || true
rm -f "$UNIT_DIR/rag-mcp.service" "$UNIT_DIR/qdrant.service"
"${SYSTEMCTL[@]}" daemon-reload 2>/dev/null || true
rm -f "$BIN"

# --- 2. 프로그램 (애플리케이션, 가상환경, Qdrant 바이너리) -----------------------------------
if [ "$MODE" = system ]; then
    # 시스템 모드는 프로그램(/opt)과 데이터(/var/lib)가 따로 있으므로 /opt/rag-mcp 를 통째로 지웁니다.
    rm -rf "$PREFIX"
else
    # 사용자 모드는 데이터가 $PREFIX/data 안에 있으므로 프로그램 부분만 골라 지웁니다.
    rm -rf "$PREFIX/app" "$PREFIX/venv" "$PREFIX/qdrant"
fi

# --- 3. 설정과 데이터 (--purge 일 때만) ---------------------------------------------------
if [ "$PURGE" = "1" ]; then
    rm -rf "$DATA_DIR" "$CONF_DIR"
    if [ "$MODE" = system ]; then
        if id "$RUN_USER" >/dev/null 2>&1; then
            userdel "$RUN_USER"
        fi
    else
        # 다른 파일이 없으면 빈 상위 디렉터리도 정리합니다.
        rmdir "$PREFIX" 2>/dev/null || true
    fi
    echo "rag-mcp를 데이터와 설정까지 모두 제거했습니다 ($MODE 모드)."
else
    echo "rag-mcp를 제거했습니다 ($MODE 모드). 설정($CONF_DIR)과 데이터($DATA_DIR)는 남아 있습니다."
    if [ "$MODE" = system ]; then echo "모두 지우려면: sudo $0 --purge"; else echo "모두 지우려면: $0 --user --purge"; fi
fi
