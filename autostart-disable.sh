#!/usr/bin/env bash
set -e

PLIST_FILE="$HOME/Library/LaunchAgents/com.recordbrief.app.plist"

if [ -f "$PLIST_FILE" ]; then
    launchctl unload "$PLIST_FILE" 2>/dev/null || true
    rm -f "$PLIST_FILE"
    echo "✅ [Record-Brief] 자동 켜짐 설정이 성공적으로 해제되었습니다."
else
    echo "ℹ️ 등록된 자동 켜짐 설정이 없습니다."
fi
