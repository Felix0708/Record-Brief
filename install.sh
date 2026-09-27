#!/usr/bin/env bash
set -e

# 스크립트 위치로 이동
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "=========================================================="
echo "  🎙️ Record-Brief 설치를 시작합니다 (Mac 전용 자동 설치기)   "
echo "=========================================================="

# 1. Python 3 확인
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3가 설치되어 있지 않습니다. Mac 터미널에서 'brew install python'을 먼저 실행해주세요."
    exit 1
fi

echo "1/4. Python 가상환경(venv) 생성 중..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

echo "2/4. 필요한 핵심 패키지 설치 중 (FastAPI, Faster-Whisper, Rumps 등)..."
./venv/bin/pip install --upgrade pip --quiet
./venv/bin/pip install -r requirements.txt --quiet

echo "3/4. 실행 스크립트 권한 부여 및 Mac 정식 앱(Record-Brief.app) 생성 중..."
chmod +x start.sh Record-Brief.command autostart-enable.sh autostart-disable.sh install.sh Install.command 2>/dev/null || true

# macOS .app 번들 컴파일
if command -v osacompile &> /dev/null; then
    # Pass the path as a value, so spaces and quotes cannot become AppleScript/shell code.
    ./venv/bin/python3 - "$SCRIPT_DIR" <<'PY'
import json
import shlex
import subprocess
import sys
from pathlib import Path

directory = Path(sys.argv[1])
command = shlex.quote(str(directory / "start.sh")) + " > /dev/null 2>&1 &"
subprocess.run(["osacompile", "-o", str(directory / "Record-Brief.app"),
                "-e", "do shell script " + json.dumps(command, ensure_ascii=False)], check=True)
PY
    echo "   ✅ Record-Brief.app 생성 완료!"
fi

echo "4/4. AI 엔진(Codex / Antigravity) 상태 확인..."
export PATH="$HOME/.local/bin:$HOME/.gemini/antigravity/bin:/opt/homebrew/bin:$PATH"
for NODE_BIN in "$HOME"/.nvm/versions/node/*/bin; do
    [ ! -d "$NODE_BIN" ] || export PATH="$NODE_BIN:$PATH"
done

if command -v codex &> /dev/null; then
    echo "   🟢 OpenAI Codex CLI 발견됨"
else
    echo "   ⚪ OpenAI Codex CLI 미설치 (공식 설치 안내를 참고해주세요)"
fi

if command -v agy &> /dev/null; then
    echo "   🟢 Google Antigravity CLI 발견됨"
else
    echo "   ⚪ Antigravity CLI 미설치"
fi

echo "=========================================================="
echo "🎉 설치가 성공적으로 완료되었습니다!"
echo "👉 지금 바로 './start.sh' 또는 'Record-Brief.app'을 실행하시면 됩니다."
echo "=========================================================="

# 설치 완료 후 앱 바로 실행
./start.sh
