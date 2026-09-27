#!/usr/bin/env bash
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "🎙️ [Record-Brief] Mac 메뉴바 앱을 실행합니다..."
export PATH="$HOME/.local/bin:$HOME/.gemini/antigravity/bin:/opt/homebrew/bin:$PATH"
for NODE_BIN in "$HOME"/.nvm/versions/node/*/bin; do
    [ ! -d "$NODE_BIN" ] || export PATH="$NODE_BIN:$PATH"
done

# 메뉴바 앱 실행
./venv/bin/python3 menu_app.py
