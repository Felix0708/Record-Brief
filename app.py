import os
import sys
import subprocess
import shutil
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# 프로젝트 기본 경로
BASE_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = BASE_DIR / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Record-Brief API", version="1.0.0")

# CORS 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 정적 파일 서빙
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/api/engine-status")
async def get_engine_status():
    """사용자 환경에서 codex 및 agy 설치 및 로그인 상태 확인"""
    env = get_cli_env()
    
    # 1. Codex 검사
    codex_bin = shutil.which("codex", path=env["PATH"])
    codex_status = {"available": bool(codex_bin), "logged_in": False, "message": "미설치"}
    if codex_bin:
        try:
            res = subprocess.run([codex_bin, "login", "status"], env=env, capture_output=True, text=True, timeout=5)
            output = (res.stdout + res.stderr).strip()
            if "Logged in" in output:
                codex_status["logged_in"] = True
                codex_status["message"] = "정상 연결됨 (ChatGPT/OpenAI)"
            else:
                codex_status["message"] = "로그인 필요 ('codex login' 필요)"
        except Exception as e:
            codex_status["message"] = f"확인 실패: {e}"

    # 2. AGY 검사
    agy_bin = shutil.which("agy", path=env["PATH"])
    agy_status = {"available": bool(agy_bin), "logged_in": False, "message": "미설치"}
    if agy_bin:
        try:
            res = subprocess.run([agy_bin, "models"], env=env, capture_output=True, text=True, timeout=8)
            if res.returncode == 0:
                agy_status["logged_in"] = True
                agy_status["message"] = "정상 연결됨"
            else:
                agy_status["message"] = "인증 필요"
        except Exception as e:
            agy_status["message"] = f"확인 실패: {e}"

    return JSONResponse({"codex": codex_status, "agy": agy_status})

# whisper 모델 캐시 (사이즈별)
whisper_models = {}

def get_whisper_model(model_size: str = "small"):
    global whisper_models
    if model_size not in whisper_models:
        try:
            from faster_whisper import WhisperModel
            # Apple Silicon CPU/int8은 매우 빠르고 안정적
            whisper_models[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
        except Exception as e:
            print(f"[Whisper] 모델({model_size}) 로딩 실패: {e}", file=sys.stderr)
            return None
    return whisper_models.get(model_size)

def get_cli_env():
    """사용자 CLI(claude, codex, agy)가 위치한 PATH 환경 변수 구성"""
    env = os.environ.copy()
    user_home = Path.home()
    nvm_bins = [str(p) for p in (user_home / ".nvm/versions/node").glob("*/bin")]
    extra_paths = [
        *nvm_bins,
        str(user_home / ".local/bin"),
        str(user_home / ".gemini/antigravity/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    current_path = env.get("PATH", "")
    env["PATH"] = ":".join(extra_paths) + ":" + current_path
    return env

def run_ai_cli(engine: str, prompt: str, selected_model: Optional[str] = None, reasoning_effort: str = "medium") -> str:
    """사용자가 선택한 CLI 엔진(agy 또는 codex), 세부 모델, 추론 가중치를 실행하여 텍스트 결과 반환"""
    env = get_cli_env()
    
    # 1. agy (Antigravity CLI)
    if engine == "agy":
        agy_bin = shutil.which("agy", path=env["PATH"])
        if not agy_bin:
            raise RuntimeError("시스템에서 'agy' 명령어를 찾을 수 없습니다.")
        
        cmd = [agy_bin]
        if selected_model and selected_model.strip():
            cmd.extend(["--model", selected_model.strip()])
        if reasoning_effort in ["low", "medium", "high"]:
            cmd.extend(["--effort", reasoning_effort])
        cmd.extend(["-p", prompt])
        
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"agy 실행 실패: {err}")
        return proc.stdout.strip()

    # 2. codex (OpenAI Codex CLI)
    elif engine == "codex":
        codex_bin = shutil.which("codex", path=env["PATH"])
        if not codex_bin:
            raise RuntimeError("시스템에서 'codex' 명령어를 찾을 수 없습니다.")
        
        cmd = [codex_bin, "exec", "--skip-git-repo-check"]
        if selected_model and selected_model.strip():
            cmd.extend(["--model", selected_model.strip()])
        if reasoning_effort in ["low", "medium", "high"]:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        cmd.append(prompt)
        
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"codex 실행 실패: {err}")
        return proc.stdout.strip()

    else:
        raise ValueError(f"지원하지 않는 AI 엔진입니다: {engine}")

def transcribe_audio(audio_path: Path, model_size: str = "small") -> str:
    """오디오 파일을 faster-whisper로 한국어 전사"""
    model = get_whisper_model(model_size)
    if not model:
        raise RuntimeError(f"Whisper 모델({model_size})을 초기화할 수 없습니다.")
    
    segments, info = model.transcribe(
        str(audio_path),
        language="ko",
        beam_size=5,
        vad_filter=True, # 목소리 있는 구간만 정밀 감지
        vad_parameters=dict(min_silence_duration_ms=500)
    )
    
    transcript_lines = []
    for segment in segments:
        m, s = divmod(int(segment.start), 60)
        h, m = divmod(m, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
        transcript_lines.append(f"[{time_str}] {segment.text.strip()}")
        
    return "\n".join(transcript_lines)

def build_meeting_notes_prompt(transcript: str, participants: str, date_str: str) -> str:
    """고품질 회의록 생성을 위한 프롬프트 구성"""
    participants_guide = f"사용자가 제공한 참석자 목록 (발화자 매핑은 미확인): {participants}" if participants else "참석자 목록: 미확인"
    
    return f"""당신은 세계 최고의 수석 비서관이자 전략 회의록 전문 작성가입니다.
아래 제공된 [회의 녹취록]을 꼼꼼히 분석하여, Obsidian, Notion 등에서 바로 사용할 수 있는 최고 품질의 마크다운(`.md`) 회의록을 작성해주세요.

### 회의 메타데이터
- 일시: {date_str}
- {participants_guide}

---
### 작성 규칙:
1. 음성은 제공되지 않았습니다. 원문에 명시되지 않은 화자, 이름, 직책, 참석자 수를 추정하지 마세요. 발화자와 담당자가 명확하지 않으면 `미확인`으로 표시하세요. 결정 사항, 숫자, 기한도 추측하지 마세요.
2. 구어체, 잡담, 불필요한 추임새는 정제하고 명료하고 격식 있는 보고서 문체(개조식 ~함, ~음 또는 정중한 어조)로 요약하세요.
3. 반드시 다음 마크다운 양식을 엄격히 준수하여 출력하세요. 마크다운 외의 다른 말(예: "네, 작성해 드렸습니다" 등)은 일절 포함하지 마세요.

---

# 📋 회의록: [회의의 가장 핵심적인 주제를 포괄하는 명확한 제목]

> **일시**: {date_str}  
> **참석자**: [참석자 명단]  
> **회의 목적**: [회의 목적 1줄 요약]

---

## 💡 3줄 핵심 요약
1. [핵심 요약 1]
2. [핵심 요약 2]
3. [핵심 요약 3]

---

## 📌 주요 논의 내용

### 안건 1. [첫 번째 안건 명칭]
- **논의 배경 및 상황**: 
- **주요 발언 및 의견**:
  - [발화자]: [핵심 발언 내용]
- **논의 결과**:

### 안건 2. [두 번째 안건 명칭] (필요 시 계속 추가)
- ...

---

## ✅ 결정 사항 (Decisions)
- [x] [확정된 결정 사항 1]
- [x] [확정된 결정 사항 2]

---

## 🎯 액션 아이템 (Action Items)
| 상태 | 담당자 | 할 일 (Action) | 기한 (Due Date) |
| :---: | :---: | :--- | :---: |
| [ ] | [담당자] | [구체적 실행 과제] | [기한 또는 미정] |

---

## 📅 차기 일정 및 후속 조치
- **차기 회의**: [일시 또는 추후 협의]
- **보류/미결 안건**: [추가 확인이 필요한 사항]

---
[회의 녹취록 원문]
{transcript}
"""

@app.get("/", response_class=HTMLResponse)
async def read_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Record-Brief</h1><p>UI 로딩 중...</p>")
    return HTMLResponse(index_file.read_text(encoding="utf-8"))

@app.post("/api/process")
async def process_meeting(
    audio: Optional[UploadFile] = File(None),
    transcript_text: Optional[str] = Form(None),
    participants: Optional[str] = Form(""),
    engine: str = Form("agy"),
    selected_model: Optional[str] = Form(""),
    reasoning_effort: Optional[str] = Form("medium"),
    whisper_size: Optional[str] = Form("small"),
    meeting_type: Optional[str] = Form("mixed")
):
    try:
        now_dt = datetime.now()
        timestamp_str = now_dt.strftime("%Y-%m-%d_%H-%M-%S")
        date_display = now_dt.strftime("%Y년 %m월 %d일 %H:%M:%S")
        
        # 1. 출력 폴더 생성
        session_dir = OUTPUTS_DIR / timestamp_str
        session_dir.mkdir(parents=True, exist_ok=True)
        
        # 2. 오디오 파일 저장 (있는 경우)
        audio_saved_path = None
        if audio and audio.filename:
            file_ext = Path(audio.filename).suffix or ".webm"
            audio_saved_path = session_dir / f"audio{file_ext}"
            content = await audio.read()
            audio_saved_path.write_bytes(content)
        
        # 3. 텍스트 녹취록 확보 (브라우저 실시간 전사 or Whisper 전사)
        final_transcript = ""
        
        # 브라우저 전사가 충분히 있는 경우 우선 활용
        if transcript_text and len(transcript_text.strip()) > 20:
            final_transcript = transcript_text.strip()
        elif audio_saved_path and audio_saved_path.exists():
            # Whisper STT 실행 (선택된 모델 크기: base, small, medium, large-v3-turbo, large-v3)
            final_transcript = transcribe_audio(audio_saved_path, model_size=whisper_size or "small")
        
        if not final_transcript:
            final_transcript = "음성 내용이 감지되지 않았거나 녹음 데이터가 비어 있습니다."
        
        # 4. 녹취록.md 저장
        transcript_md_path = session_dir / "녹취록.md"
        transcript_md_content = f"# 📄 전체 발화 녹취록\n\n- **기록 일시**: {date_display}\n- **참석자 힌트**: {participants or '미지정'}\n\n---\n\n{final_transcript}\n"
        transcript_md_path.write_text(transcript_md_content, encoding="utf-8")
        
        # 5. AI CLI를 통한 회의록.md 생성
        prompt = build_meeting_notes_prompt(final_transcript, participants, date_display)
        
        try:
            meeting_notes_md = run_ai_cli(engine, prompt, selected_model, reasoning_effort or "medium")
        except Exception as cli_err:
            # 선택한 CLI 실패 시 agy로 자동 폴백 시도
            if engine != "agy":
                print(f"[{engine}] 실패로 'agy'로 자동 폴백합니다: {cli_err}", file=sys.stderr)
                meeting_notes_md = run_ai_cli("agy", prompt, None, "medium")
            else:
                raise cli_err

        # 회의록.md 파일 저장
        meeting_notes_path = session_dir / "회의록.md"
        meeting_notes_path.write_text(meeting_notes_md, encoding="utf-8")
        
        # 6. 메타데이터 json 저장
        meta_data = {
            "timestamp": timestamp_str,
            "created_at": date_display,
            "engine": engine,
            "selected_model": selected_model,
            "reasoning_effort": reasoning_effort,
            "whisper_size": whisper_size,
            "participants": participants,
            "meeting_type": meeting_type,
            "audio_file": str(audio_saved_path.name) if audio_saved_path else None,
            "folder_path": str(session_dir)
        }
        (session_dir / "meta.json").write_text(json.dumps(meta_data, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # 7. Mac Finder에서 해당 폴더 자동 열기
        try:
            subprocess.run(["open", str(session_dir)], check=False)
        except Exception:
            pass
        
        return JSONResponse({
            "status": "success",
            "folder_path": str(session_dir),
            "folder_name": timestamp_str,
            "meeting_notes": meeting_notes_md,
            "transcript": final_transcript,
            "audio_saved": bool(audio_saved_path)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history")
async def get_history():
    """이전 생성된 회의록 폴더 목록 조회"""
    items = []
    if OUTPUTS_DIR.exists():
        for d in sorted(OUTPUTS_DIR.iterdir(), reverse=True):
            if d.is_dir():
                meta_file = d / "meta.json"
                meeting_file = d / "회의록.md"
                meta = {}
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                
                title = d.name
                if meeting_file.exists():
                    lines = meeting_file.read_text(encoding="utf-8").splitlines()
                    for line in lines:
                        if line.startswith("# 📋 회의록:"):
                            title = line.replace("# 📋 회의록:", "").strip()
                            break

                items.append({
                    "folder_name": d.name,
                    "folder_path": str(d),
                    "title": title,
                    "created_at": meta.get("created_at", d.name),
                    "engine": meta.get("engine", "unknown")
                })
    return JSONResponse({"history": items})

@app.post("/api/open-folder")
async def open_folder(folder_path: str = Form(...)):
    """지정된 폴더를 Mac Finder에서 오픈"""
    path = Path(folder_path)
    if path.exists() and path.is_dir():
        subprocess.run(["open", str(path)], check=False)
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="폴더를 찾을 수 없습니다.")

@app.get("/api/autostart")
async def get_autostart_status():
    """맥 로그인 시 자동 켜짐 상태 확인"""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.recordbrief.app.plist"
    return {"enabled": plist_path.exists()}

@app.post("/api/autostart")
async def set_autostart_status(enable: bool = Form(...)):
    """맥 로그인 시 자동 켜짐 활성화 / 비활성화"""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_file = plist_dir / "com.recordbrief.app.plist"
    script_path = BASE_DIR / "start.sh"

    try:
        if enable:
            plist_dir.mkdir(parents=True, exist_ok=True)
            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.recordbrief.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{script_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardErrorPath</key>
    <string>/tmp/com.recordbrief.app.err</string>
    <key>StandardOutPath</key>
    <string>/tmp/com.recordbrief.app.out</string>
</dict>
</plist>
"""
            plist_file.write_text(plist_content, encoding="utf-8")
            subprocess.run(["launchctl", "unload", str(plist_file)], check=False, capture_output=True)
            subprocess.run(["launchctl", "load", str(plist_file)], check=False, capture_output=True)
            return {"status": "ok", "enabled": True}
        else:
            if plist_file.exists():
                subprocess.run(["launchctl", "unload", str(plist_file)], check=False, capture_output=True)
                plist_file.unlink(missing_ok=True)
            return {"status": "ok", "enabled": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # 기본 포트 8765로 실행
    uvicorn.run("app:app", host="127.0.0.1", port=8765, reload=True)
