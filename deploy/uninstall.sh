#!/usr/bin/env bash
# rag-mcp 로컬 서버 제거 스크립트.
#
#   sudo ./deploy/uninstall.sh           # 서비스와 프로그램만 제거 (설정·문서·Qdrant 데이터 유지)
#   sudo ./deploy/uninstall.sh --purge   # 설정, 문서, Qdrant 데이터, 사용자까지 모두 삭제
set -euo pipefail

RUN_USER=rag-mcp
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

[ "$(id -u)" -eq 0 ] || { echo "root 권한이 필요합니다: sudo $0" >&2; exit 1; }

systemctl disable --now rag-mcp.service qdrant.service 2>/dev/null || true
rm -f /etc/systemd/system/rag-mcp.service /etc/systemd/system/qdrant.service
systemctl daemon-reload
rm -f /usr/local/bin/rag-ingest
rm -rf /opt/rag-mcp

if [ "$PURGE" = "1" ]; then
    rm -rf /var/lib/rag-mcp /etc/rag-mcp
    if id "$RUN_USER" >/dev/null 2>&1; then
        userdel "$RUN_USER"
    fi
    echo "rag-mcp를 데이터와 설정까지 모두 제거했습니다."
else
    echo "rag-mcp를 제거했습니다. 설정(/etc/rag-mcp)과 데이터(/var/lib/rag-mcp)는 남아 있습니다."
    echo "모두 지우려면: sudo $0 --purge"
fi
