import os
import sys
import time
import json
import re
import fcntl
import threading
import subprocess
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple

import rumps
import numpy as np
import AppKit
from PyObjCTools import AppHelper
import sounddevice as sd
import soundfile as sf
from audio_capture import OnlineAudioCapture, mix_online_audio

# ---------------------------------------------------------
# 단일 인스턴스 중복 실행 방지 락
# ---------------------------------------------------------
LOCK_FILE = Path.home() / ".record_brief.lock"
_lock_fd = None

def acquire_single_instance_lock() -> bool:
    global _lock_fd
    try:
        _lock_fd = open(LOCK_FILE, "a+")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.seek(0)
        _lock_fd.truncate()
        _lock_fd.write(f"{os.getpid()}\n")
        _lock_fd.flush()
        return True
    except (IOError, BlockingIOError):
        return False

# ---------------------------------------------------------
# 기본 디렉터리 및 상수 설정
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = BASE_DIR / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
PLIST_FILE = Path.home() / "Library" / "LaunchAgents" / "com.recordbrief.menubar.plist"
QUOTA_CACHE_FILE = BASE_DIR / "quota_cache.json"

ICON_TEMPLATE_PATH = str(BASE_DIR / "assets" / "iconTemplate@2x.png")
if not Path(ICON_TEMPLATE_PATH).exists():
    ICON_TEMPLATE_PATH = str(BASE_DIR / "assets" / "iconTemplate.png")

# Whisper 모델 캐시
whisper_models = {}

def get_whisper_model(model_size: str = "large-v3-turbo"):
    global whisper_models
    if model_size not in whisper_models:
        try:
            from faster_whisper import WhisperModel
            whisper_models[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
        except Exception as e:
            print(f"[Whisper] 모델({model_size}) 로딩 실패: {e}", file=sys.stderr)
            return None
    return whisper_models.get(model_size)

def get_cli_env() -> Dict[str, str]:
    """모든 주요 CLI 경로를 아우르는 환경변수 생성"""
    env = os.environ.copy()
    user_home = Path.home()
    
    nvm_node_dir = user_home / ".nvm" / "versions" / "node"
    nvm_bins = []
    if nvm_node_dir.exists():
        nvm_bins = [str(p) for p in nvm_node_dir.glob("*/bin")]
    
    extra_paths = [
        str(user_home / ".local/bin"),
        str(user_home / ".gemini/antigravity/bin"),
        *nvm_bins,
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    current_path = env.get("PATH", "")
    env["PATH"] = ":".join(extra_paths) + ":" + current_path
    return env

def check_codex_status(force_refresh: bool = False) -> Tuple[str, str]:
    """
    Codex의 설치, 로그인, 토큰/쿼터 초과 상태를 감지하여 반환
    반환값: (상태코드, 리셋시간/안내메시지)
    상태코드: 'not_installed' | 'not_logged_in' | 'quota_exceeded' | 'ready'
    """
    env = get_cli_env()
    codex_bin = shutil.which("codex", path=env["PATH"])
    if not codex_bin:
        return "not_installed", "미설치"
        
    # 캐시 확인 (불필요한 반복 쿼터 체크 방지)
    if not force_refresh and QUOTA_CACHE_FILE.exists():
        try:
            cache = json.loads(QUOTA_CACHE_FILE.read_text(encoding="utf-8"))
            if cache.get("quota_exceeded", False):
                expiry = cache.get("expiry_timestamp", 0)
                if time.time() < expiry:
                    return "quota_exceeded", cache.get("reset_time_str", "한도 초과")
        except Exception:
            pass

    # 1. 로그인 상태 확인
    try:
        res = subprocess.run([codex_bin, "login", "status"], env=env, capture_output=True, text=True, timeout=4)
        combined = (res.stdout + res.stderr).strip()
        if "Logged in" not in combined:
            return "not_logged_in", "로그인 필요"
    except Exception:
        return "not_logged_in", "로그인 필요"

    # 2. 토큰 쿼터(사용량 제한) 체크 (드라이런 테스트)
    try:
        p = subprocess.run(
            [codex_bin, "exec", "--skip-git-repo-check", "ping"],
            env=env,
            capture_output=True,
            text=True,
            timeout=6
        )
        combined = (p.stdout + p.stderr).strip()
        if "hit your usage limit" in combined or "usage limit" in combined or "insufficient_quota" in combined:
            m = re.search(r'try again at ([^.]+)\.', combined)
            raw_time = m.group(1).strip() if m else ""
            reset_display = "일시적 한도 초과"
            if raw_time:
                reset_display = raw_time.replace("th", "").replace("st", "").replace("nd", "").replace("rd", "")
                
            cache_data = {
                "quota_exceeded": True,
                "expiry_timestamp": time.time() + 43200,
                "reset_time_str": reset_display
            }
            QUOTA_CACHE_FILE.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return "quota_exceeded", reset_display
    except Exception:
        pass

    return "ready", "정상"

def check_agy_status() -> bool:
    """AGY CLI 설치 및 로그인 상태 검사"""
    env = get_cli_env()
    agy_bin = shutil.which("agy", path=env["PATH"])
    if not agy_bin:
        return False
    try:
        res = subprocess.run([agy_bin, "--version"], env=env, capture_output=True, text=True, timeout=4)
        return res.returncode == 0
    except Exception:
        return False

def run_ai_cli(engine: str, prompt: str, selected_model: Optional[str] = None, reasoning_effort: str = "high") -> str:
    """각 엔진별 고유 파라미터 매핑을 통한 안전한 CLI 호출"""
    env = get_cli_env()
    
    if engine == "codex":
        codex_bin = shutil.which("codex", path=env["PATH"])
        if not codex_bin:
            raise RuntimeError("시스템에서 'codex' 명령어를 찾을 수 없습니다.")
        
        cmd = [codex_bin, "exec", "--skip-git-repo-check"]
        if selected_model and selected_model.strip():
            cmd.extend(["--model", selected_model.strip()])
            
        # Codex 규격 파라미터
        if reasoning_effort in ["low", "medium", "high"]:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        cmd.append(prompt)
        
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240)
        err = proc.stderr.strip() or proc.stdout.strip()
        if proc.returncode != 0:
            if "hit your usage limit" in err or "usage limit" in err:
                raise RuntimeError(f"CODEX_QUOTA_EXCEEDED: {err}")
            raise RuntimeError(f"codex 실행 실패: {err}")
        return proc.stdout.strip()

    elif engine == "agy":
        agy_bin = shutil.which("agy", path=env["PATH"])
        if not agy_bin:
            raise RuntimeError("시스템에서 'agy' 명령어를 찾을 수 없습니다.")
        
        cmd = [agy_bin]
        if selected_model and selected_model.strip():
            cmd.extend(["--model", selected_model.strip()])
            
        # AGY 규격: 모델명 자체에 (High), (Medium), (Low), (Thinking) 등이 있으면 --effort 중복 전달 배제
        has_embedded_effort = any(k in (selected_model or "") for k in ["(High)", "(Medium)", "(Low)", "(Thinking)"])
        if not has_embedded_effort and reasoning_effort in ["low", "medium", "high"]:
            cmd.extend(["--effort", reasoning_effort])
        cmd.extend(["-p", prompt])
        
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240)
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            if "--effort is not supported" in err:
                retry_cmd = [agy_bin]
                if selected_model and selected_model.strip():
                    retry_cmd.extend(["--model", selected_model.strip()])
                retry_cmd.extend(["-p", prompt])
                proc2 = subprocess.run(retry_cmd, env=env, capture_output=True, text=True, timeout=240)
                if proc2.returncode == 0:
                    return proc2.stdout.strip()
            raise RuntimeError(f"agy 실행 실패: {err}")
        return proc.stdout.strip()
    else:
        raise ValueError(f"지원하지 않는 AI 엔진: {engine}")

def transcribe_audio(audio_path: Path, model_size: str = "large-v3-turbo") -> str:
    """Faster-Whisper 고정밀 음성 전사 (환각 방지 및 한국어 최적화)"""
    model = get_whisper_model(model_size)
    if not model:
        raise RuntimeError(f"Whisper 모델({model_size}) 로드 실패")
    
    segments, info = model.transcribe(
        str(audio_path),
        language="ko",
        beam_size=5,
        best_of=5,
        word_timestamps=True,
        condition_on_previous_text=False,  # 연쇄 환각 원천 차단
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=400,    # 자연스러운 호흡/문장 간격 분할
            speech_pad_ms=200               # 어미 보존
        ),
        compression_ratio_threshold=2.4,   # 비정상 반복 텍스트 억제
        no_speech_threshold=0.6,           # 무음/잡음 환각 억제
        initial_prompt="한국어 회의 및 일상 대화 녹취록입니다. 정확한 한국어 맞춤법과 띄어쓰기를 준수하며, 외국어 환각 없이 자연스럽게 전사합니다."
    )
    
    transcript_lines = []
    current_words = []
    current_start = None

    for segment in segments:
        words = getattr(segment, "words", None)
        if not words:
            m, s = divmod(int(segment.start), 60)
            h, m = divmod(m, 60)
            time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
            text = segment.text.strip()
            clean_text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\xad]', '', text)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            if clean_text:
                transcript_lines.append(f"[{time_str}] {clean_text}")
            continue

        for w in words:
            w_text = w.word.strip()
            if not w_text:
                continue
            if current_start is None:
                current_start = w.start

            # 말 사이의 0.6초 이상 자연스러운 쉼(Pause) 감지 시 문장 분할
            if current_words and (w.start - current_words[-1].end >= 0.6):
                m, s = divmod(int(current_start), 60)
                h, m = divmod(m, 60)
                time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
                raw_sentence = " ".join([cw.word.strip() for cw in current_words])
                clean_sentence = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\xad]', '', raw_sentence)
                clean_sentence = re.sub(r'\s+', ' ', clean_sentence).strip()
                if clean_sentence:
                    transcript_lines.append(f"[{time_str}] {clean_sentence}")
                current_words = []
                current_start = w.start

            current_words.append(w)

            # 마침표, 물음표, 느낌표 종결 어미 감지 시 문맥 단위 분할
            if any(w_text.endswith(p) for p in [".", "?", "!"]) and len(current_words) >= 2:
                m, s = divmod(int(current_start), 60)
                h, m = divmod(m, 60)
                time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
                raw_sentence = " ".join([cw.word.strip() for cw in current_words])
                clean_sentence = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\xad]', '', raw_sentence)
                clean_sentence = re.sub(r'\s+', ' ', clean_sentence).strip()
                if clean_sentence:
                    transcript_lines.append(f"[{time_str}] {clean_sentence}")
                current_words = []
                current_start = None

    if current_words and current_start is not None:
        m, s = divmod(int(current_start), 60)
        h, m = divmod(m, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
        raw_sentence = " ".join([cw.word.strip() for cw in current_words])
        clean_sentence = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\xad]', '', raw_sentence)
        clean_sentence = re.sub(r'\s+', ' ', clean_sentence).strip()
        if clean_sentence:
            transcript_lines.append(f"[{time_str}] {clean_sentence}")

    return "\n".join(transcript_lines)

def build_diarization_prompt(transcript: str, date_str: str) -> str:
    return f"""아래 작업은 음성을 분석하는 화자 분리가 아니라 텍스트 문맥에 따른 화자 추정입니다.
아래는 음성인식(STT)을 통해 타임스탬프 단위로 정밀하게 기록된 대화 녹취록입니다.
음성이나 목소리 특징은 제공되지 않았습니다. 화자의 수, 신원, 동일인 여부를 확정하지 마세요.
화자별 구분에 근거가 부족하면 `화자 미확인`으로 표시해주세요.

### 규칙:
1. 타임스탬프와 원문 문장을 그대로 보존하세요. 구분 근거가 있더라도 `화자 1 (추정)`처럼 불확실성을 표시하세요.
2. 질문과 답변의 형태만으로 다른 화자라고 단정하거나 이름과 역할을 만들어내지 마세요.
3. 원문을 교정하거나 합치거나 생략하지 마세요. 불명확한 발화는 원문 그대로 두세요.
4. 불필요한 부연 설명이나 인사말 없이, 아래 마크다운 포맷에 맞춘 문서 내용만 순수하게 출력하세요.

### 문서 포맷:
# 🎙️ 화자 추정 녹취록 (문맥 기반)

> **일시**: {date_str}  
> **안내**: 음성 기반 화자 분리가 아닙니다. 화자 표시는 추정이며 확인이 필요합니다.

---

- [00:00] **화자 미확인**: [발화 내용]
- [00:10] **화자 1 (추정)**: [발화 내용]

---
[원본 타임스탬프 녹취록]
{transcript}
"""

def build_meeting_notes_prompt(transcript: str, date_str: str, style: str = "standard") -> str:
    if style == "brief":
        style_template = f"""
# 📋 회의 요약 브리핑: [회의 핵심 주제]

> **일시**: {date_str}  
> **보고서 성격**: 핵심 요약 및 액션 아이템 중심

---

## 💡 3줄 핵심 요약
1. [핵심 요점 1]
2. [핵심 요점 2]
3. [핵심 요점 3]

---

## ✅ 확정 결정 사항
- [x] [확정된 결정 사항 1]
- [x] [확정된 결정 사항 2]

---

## 🎯 액션 아이템 (Action Items)
| 담당자 | 할 일 (Action) | 기한 (Due Date) |
| :---: | :--- | :---: |
| [담당자] | [구체적 실행 과제] | [기한 또는 미정] |
"""
    elif style == "deep":
        style_template = f"""
# 📋 심층 회의록: [회의의 전략적 핵심 주제]

> **일시**: {date_str}  
> **작성 목적**: 심층 논의 및 발언별 맥락 보존

---

## 🧭 회의 배경 및 총평
[회의 소집 배경 및 전체적인 논의 흐름 총평]

---

## 📌 안건별 심층 논의 및 발언 기록

### 안건 1. [첫 번째 안건 명칭]
- **논의 배경**: 
- **참석자별 주요 발언 및 논거**:
  - [발화자]: [주장 및 발언 상세]
  - [발화자]: [반론 또는 보충 의견]
- **논의 결론 및 시사점**:

---

## ⚖️ 핵심 쟁점 및 리스크 검토
- [쟁점 1]: [상황 및 고려사항]

---

## ✅ 최종 결정 사항
- [x] [결정 사항 1]
- [x] [결정 사항 2]

---

## 🎯 액션 아이템
| 우선순위 | 담당자 | 할 일 (Action) | 기한 |
| :---: | :---: | :--- | :---: |
| [높음/보통] | [담당자] | [과제 내용] | [기한] |
"""
    else:  # standard
        style_template = f"""
# 📋 회의록: [회의의 가장 핵심적인 주제를 포괄하는 명확한 제목]

> **일시**: {date_str}  
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
- **보류/미결 안건**: [추가 확인 필요 사항]
"""

    return f"""당신은 세계 최고의 수석 비서관이자 전략 회의록 전문 작성가입니다.
아래 제공된 [회의 녹취록]을 꼼꼼히 분석하여, Obsidian, Notion 등에서 바로 사용할 수 있는 최고 품질의 마크다운(`.md`) 회의록을 작성해주세요.

### 작성 규칙:
1. 원문에 명시되지 않은 화자, 이름, 직책, 참석자 수를 추정하지 마세요. 발화자와 담당자가 명확하지 않으면 `미확인`으로 표시하세요.
2. 구어체, 잡담, 불필요한 추임새는 정제하고 명료하고 격식 있는 보고서 문체(개조식 ~함, ~음)로 요약하세요.
3. 반드시 다음 마크다운 양식을 엄격히 준수하여 출력하세요. 마크다운 외의 인사말 등은 일절 포함하지 마세요.
4. 불명확한 STT 내용은 `확인 필요`로 표시하세요. 숫자, 고유명사, 결정 사항과 기한을 추측하거나 새로 만들지 마세요.

---
{style_template}
---
[회의 녹취록 원문]
{transcript}
"""

def copy_to_clipboard(text: str):
    try:
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
        proc.communicate(text)
    except Exception as e:
        print(f"클립보드 복사 실패: {e}", file=sys.stderr)


# ---------------------------------------------------------
# macOS Native Menu Bar App (Rumps)
# ---------------------------------------------------------
class RecordBriefApp(rumps.App):
    def __init__(self):
        # 흑백 네이티브 템플릿 아이콘 (기본 title은 None으로 두어 단색 마이크만 표시)
        super().__init__(
            "Record-Brief",
            icon=ICON_TEMPLATE_PATH,
            template=True,
            quit_button=None
        )

        # macOS Dock 아이콘 완전 숨김 (순수 메뉴바 전용 액세서리 앱)
        try:
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

        self.is_recording = False
        self.record_start_time = None
        self.recorded_frames = []
        self.sample_rate = 16000
        self.audio_stream = None
        self.online_capture = None
        self.recording_session_dir = None
        self.active_background_jobs = 0
        
        # 기본 설정값
        self.selected_engine = "agy"  # Codex 한도 초과 시 AGY가 기본 활성화
        self.selected_model = "Gemini 3.8 Flash (High)"
        self.reasoning_effort = "high"
        self.whisper_size = "large-v3-turbo"
        self.meeting_mode = "mixed"     # "mixed" (온라인) | "mic" (대면)
        self.summary_style = "standard" # "standard" | "brief" | "deep"
        self.auto_copy_clipboard = True
        
        self.selected_device_index = None
        self.selected_device_name = "MacBook Pro 마이크"

        # 1. 헤더 상태 및 프로필
        self.status_item = rumps.MenuItem("상태: 🟢 대기 중")
        self.profile_summary_item = rumps.MenuItem("⚙️ 현재: [AGY] Gemini 3.8 Flash (High) | High | Turbo")

        # 2. 실행 액션
        self.toggle_record_item = rumps.MenuItem("🎙️ 실시간 녹음 시작", callback=self.toggle_recording)
        self.process_audio_file_item = rumps.MenuItem("📂 기존 오디오 파일로 회의록 생성...", callback=self.on_process_existing_file)

        # 3. [환경 설정 1] 회의 모드 서브메뉴
        self.menu_meeting_mode = rumps.MenuItem("🎧 회의 모드: 온라인 회의")
        self.mode_online_item = rumps.MenuItem("🎧 온라인 회의 (마이크 + 상대방 소리)", callback=self.on_select_meeting_mode)
        self.mode_mic_item = rumps.MenuItem("🎙️ 대면 회의 (회의실 마이크 단독)", callback=self.on_select_meeting_mode)
        self.mode_online_item.state = True
        self.menu_meeting_mode.update([self.mode_online_item, self.mode_mic_item])

        # 4. [환경 설정 2] 마이크 입력 장치 서브메뉴
        self.device_items = self.build_audio_device_menu_items()
        self.menu_audio_device = rumps.MenuItem(f"🎤 마이크 장치: {self.selected_device_name}")
        self.menu_audio_device.update(self.device_items)

        # 5. [환경 설정 3] 회의록 요약 스타일 서브메뉴
        self.menu_style = rumps.MenuItem("📋 회의록 요약 스타일: 표준 종합형")
        self.style_standard_item = rumps.MenuItem("📑 표준 종합형 (요약+안건+결정+할 일)", callback=self.on_select_style)
        self.style_brief_item = rumps.MenuItem("⚡ 간략 브리핑형 (3줄 핵심 요약+할 일)", callback=self.on_select_style)
        self.style_deep_item = rumps.MenuItem("🔍 심층 분석형 (발언 상세 기록+맥락)", callback=self.on_select_style)
        self.style_standard_item.state = True
        self.menu_style.update([self.style_standard_item, self.style_brief_item, self.style_deep_item])

        # 6. [AI 모델 1] OpenAI Codex 서브메뉴
        self.codex_items = []
        codex_model_defs = [
            ("gpt-6-astra", "GPT-6-Astra (Frontier 최상위)"),
            ("gpt-6-sol", "GPT-6-Sol (업무/코딩 메인)"),
            ("gpt-6-luna", "GPT-6-Luna (경량 고속)"),
            ("gpt-5.6-sol", "GPT-5.6-Sol (복잡한 작업)"),
            ("gpt-5.6-terra", "GPT-5.6-Terra (균형 잡힌 모델)"),
            ("gpt-5.6-luna", "GPT-5.6-Luna (효율적인 모델)"),
            ("gpt-5.5", "GPT-5.5 (레거시 모델)")
        ]
        for slug, label in codex_model_defs:
            it = rumps.MenuItem(label, callback=self.on_select_codex_model)
            it.slug = slug
            self.codex_items.append(it)
        
        self.custom_codex_item = rumps.MenuItem("✏️ 직접 모델명 입력...", callback=self.on_input_custom_codex_model)
        self.codex_items.append(self.custom_codex_item)
        
        self.menu_codex = rumps.MenuItem("🟢 OpenAI Codex")
        self.menu_codex.update(self.codex_items)

        # 7. [AI 모델 2] Google Antigravity (agy) 서브메뉴
        self.agy_items = []
        agy_model_defs = [
            ("Gemini 3.8 Flash (High)", "Gemini 3.8 Flash (초고속 / 기본)"),
            ("Claude Sonnet 4.6 (Thinking)", "Claude Sonnet 4.6 (정밀 분석 / 추론)"),
            ("Claude Opus 4.6 (Thinking)", "Claude Opus 4.6 (최고 성능 플래그십)"),
            ("Gemini 3.1 Pro (High)", "Gemini 3.1 Pro (심층 분석)"),
            ("GPT-OSS 120B (Medium)", "GPT-OSS 120B")
        ]
        for slug, label in agy_model_defs:
            it = rumps.MenuItem(label, callback=self.on_select_agy_model)
            it.slug = slug
            if slug == "Gemini 3.8 Flash (High)":
                it.state = True
            self.agy_items.append(it)
        
        self.custom_agy_item = rumps.MenuItem("✏️ 직접 모델명 입력...", callback=self.on_input_custom_agy_model)
        self.agy_items.append(self.custom_agy_item)
        
        self.menu_agy = rumps.MenuItem("🔵 Google Antigravity (agy)")
        self.menu_agy.update(self.agy_items)

        # 8. [AI 모델 3] AI 추론 가중치 서브메뉴
        self.effort_items = [
            rumps.MenuItem("High (심층 분석 / 최고 품질 / 권장)", callback=self.on_select_effort),
            rumps.MenuItem("Medium (표준 균형)", callback=self.on_select_effort),
            rumps.MenuItem("Low (초고속 요약)", callback=self.on_select_effort)
        ]
        self.effort_items[0].state = True
        self.menu_effort = rumps.MenuItem("⚡ AI 추론 가중치: High")
        self.menu_effort.update(self.effort_items)

        # 9. [AI 모델 4] Whisper STT 품질 서브메뉴
        self.whisper_items = [
            rumps.MenuItem("Large-v3-Turbo (권장: 최신 초고속 고정밀)", callback=self.on_select_whisper),
            rumps.MenuItem("Large-v3 (현존 최고 정확도 끝판왕)", callback=self.on_select_whisper),
            rumps.MenuItem("Medium (769MB 균형)", callback=self.on_select_whisper),
            rumps.MenuItem("Small (244MB 가벼움)", callback=self.on_select_whisper)
        ]
        self.whisper_items[0].state = True
        self.menu_whisper = rumps.MenuItem("🔊 Whisper STT 품질: Large-v3-Turbo")
        self.menu_whisper.update(self.whisper_items)

        # 10. 편의 기능 및 기록
        self.recent_history_items = self.build_recent_history_items()
        self.menu_history = rumps.MenuItem("🕒 최근 생성된 회의록")
        self.menu_history.update(self.recent_history_items)

        self.copy_clipboard_item = rumps.MenuItem("📋 완성 시 클립보드에 회의록 자동 복사", callback=self.toggle_clipboard_copy)
        self.copy_clipboard_item.state = self.auto_copy_clipboard

        self.open_outputs_item = rumps.MenuItem("📁 회의록 저장 폴더 열기", callback=self.open_outputs_folder)
        self.autostart_item = rumps.MenuItem("🚀 Mac 켤 때 자동 시작", callback=self.toggle_autostart)
        self.autostart_item.state = PLIST_FILE.exists()

        self.refresh_login_item = rumps.MenuItem("🔄 AI 로그인 및 토큰 상태 새로고침", callback=self.refresh_login_status)
        self.quit_app_item = rumps.MenuItem("Record-Brief 종료", callback=self.quit_application)

        # 전체 메뉴 계층 등록
        self.menu = [
            self.status_item,
            self.profile_summary_item,
            None,
            self.toggle_record_item,
            self.process_audio_file_item,
            None,
            self.menu_meeting_mode,
            self.menu_audio_device,
            self.menu_style,
            None,
            self.menu_codex,
            self.menu_agy,
            self.menu_effort,
            self.menu_whisper,
            None,
            self.menu_history,
            self.copy_clipboard_item,
            self.open_outputs_item,
            self.autostart_item,
            self.refresh_login_item,
            None,
            self.quit_app_item
        ]

        # 초기 엔진 상태 검사 및 반영
        self.update_engine_login_ui()
        self.update_profile_summary()

    @rumps.timer(0.1)
    def init_status_bar(self, timer):
        """앱 시작 즉시 메뉴바 아이콘 길이를 정사각(NSSquare)으로 고정하여 초기 빈 여백 제거"""
        timer.stop()
        self.set_menubar_title(None)

    # -----------------------------------------------------
    # UI 빌더 및 갱신 메서드
    # -----------------------------------------------------
    def build_audio_device_menu_items(self) -> List[rumps.MenuItem]:
        items = []
        try:
            devices = sd.query_devices()
            default_in = sd.default.device[0]
            for idx, d in enumerate(devices):
                if d.get("max_input_channels", 0) > 0:
                    name = d.get("name", f"장치 {idx}")
                    it = rumps.MenuItem(name, callback=self.on_select_audio_device)
                    it.device_index = idx
                    if idx == default_in:
                        it.state = True
                        self.selected_device_index = idx
                        self.selected_device_name = name
                    items.append(it)
        except Exception as e:
            print(f"오디오 디바이스 조회 실패: {e}")
            fallback = rumps.MenuItem("기본 마이크", callback=self.on_select_audio_device)
            fallback.device_index = None
            fallback.state = True
            items.append(fallback)
        return items

    def build_recent_history_items(self) -> List[rumps.MenuItem]:
        items = []
        try:
            sessions = sorted([d for d in OUTPUTS_DIR.iterdir() if d.is_dir()], reverse=True)
            for s in sessions[:5]:
                label = f"📄 {s.name}"
                it = rumps.MenuItem(label, callback=self.on_open_history_item)
                it.folder_path = s
                items.append(it)
        except Exception:
            pass
            
        if not items:
            dummy = rumps.MenuItem("(생성된 회의록 없음)")
            dummy._menuitem.setEnabled_(False)
            items.append(dummy)
        return items

    def update_engine_login_ui(self, force_refresh: bool = False):
        """사용자 요청: 로그인 상태 및 토큰 한도 초과 시 disabled 처리"""
        codex_status, codex_msg = check_codex_status(force_refresh=force_refresh)
        agy_ready = check_agy_status()

        # 1. Codex 메뉴 라벨 및 비활성화 처리
        if codex_status == "quota_exceeded":
            # 토큰/사용량 초과 시 비활성화
            self.menu_codex.title = f"🟢 OpenAI Codex (토큰 한도 초과: {codex_msg})"
            self.menu_codex._menuitem.setEnabled_(False)
        elif codex_status == "not_logged_in":
            self.menu_codex.title = "🟢 OpenAI Codex (로그인 필요 / 비활성)"
            self.menu_codex._menuitem.setEnabled_(False)
        elif codex_status == "not_installed":
            self.menu_codex.title = "🟢 OpenAI Codex (미설치 / 비활성)"
            self.menu_codex._menuitem.setEnabled_(False)
        else:
            prefix = "🟢 OpenAI Codex (로그인됨 ✓)"
            if self.selected_engine == "codex":
                prefix = "🟢 OpenAI Codex (✓ 선택됨)"
            self.menu_codex.title = prefix
            self.menu_codex._menuitem.setEnabled_(True)

        # 2. AGY 메뉴 라벨 및 활성화
        if agy_ready:
            prefix = "🔵 Google Antigravity (로그인됨 ✓)"
            if self.selected_engine == "agy":
                prefix = "🔵 Google Antigravity (✓ 선택됨)"
            self.menu_agy.title = prefix
            self.menu_agy._menuitem.setEnabled_(True)
        else:
            self.menu_agy.title = "🔵 Google Antigravity (로그인 필요 / 비활성)"
            self.menu_agy._menuitem.setEnabled_(False)

        # 3. 비활성 엔진 선택 시 활성 엔진(AGY)으로 자동 전환
        if self.selected_engine == "codex" and codex_status != "ready":
            self.selected_engine = "agy"
            self.selected_model = "Gemini 3.8 Flash (High)"
            for it in self.agy_items:
                if getattr(it, "slug", "") == "Gemini 3.8 Flash (High)":
                    it.state = True
            for it in self.codex_items:
                it.state = False
        elif self.selected_engine == "agy" and not agy_ready and codex_status == "ready":
            self.selected_engine = "codex"
            self.selected_model = "gpt-6-astra"
            for it in self.codex_items:
                if getattr(it, "slug", "") == "gpt-6-astra":
                    it.state = True
            for it in self.agy_items:
                it.state = False

    def update_profile_summary(self):
        eng_label = "Codex" if self.selected_engine == "codex" else "AGY"
        effort_label = self.reasoning_effort.capitalize()
        whisper_label = "Turbo" if "turbo" in self.whisper_size else self.whisper_size.capitalize()
        self.profile_summary_item.title = f"⚙️ 현재: [{eng_label}] {self.selected_model} | {effort_label} | {whisper_label}"

    # -----------------------------------------------------
    # 이벤트 콜백 핸들러
    # -----------------------------------------------------
    def on_select_meeting_mode(self, sender):
        self.mode_online_item.state = False
        self.mode_mic_item.state = False
        sender.state = True
        if "온라인" in sender.title:
            self.meeting_mode = "mixed"
            self.menu_meeting_mode.title = "🎧 회의 모드: 온라인 회의"
        else:
            self.meeting_mode = "mic"
            self.menu_meeting_mode.title = "🎙️ 회의 모드: 대면 회의"

    def on_select_audio_device(self, sender):
        for it in self.device_items:
            it.state = False
        sender.state = True
        self.selected_device_index = sender.device_index
        self.selected_device_name = sender.title
        self.menu_audio_device.title = f"🎤 마이크 장치: {sender.title}"

    def on_select_style(self, sender):
        for it in [self.style_standard_item, self.style_brief_item, self.style_deep_item]:
            it.state = False
        sender.state = True
        if "간략" in sender.title:
            self.summary_style = "brief"
            self.menu_style.title = "📋 회의록 요약 스타일: 간략 브리핑형"
        elif "심층" in sender.title:
            self.summary_style = "deep"
            self.menu_style.title = "📋 회의록 요약 스타일: 심층 분석형"
        else:
            self.summary_style = "standard"
            self.menu_style.title = "📋 회의록 요약 스타일: 표준 종합형"

    def on_select_codex_model(self, sender):
        for it in self.codex_items:
            it.state = False
        for it in self.agy_items:
            it.state = False
        sender.state = True
        self.selected_engine = "codex"
        self.selected_model = sender.slug
        self.update_engine_login_ui()
        self.update_profile_summary()

    def on_input_custom_codex_model(self, _):
        window = rumps.Window(
            message="사용할 OpenAI Codex 모델명을 입력하세요:\n(예: gpt-6-astra, o3, gpt-4o 등)",
            title="✏️ 커스텀 Codex 모델 직접 입력",
            default_text=self.selected_model if self.selected_engine == "codex" else "",
            ok="확인",
            cancel="취소",
            dimensions=(300, 24)
        )
        response = window.run()
        if response.clicked == 1 and response.text.strip():
            custom_model = response.text.strip()
            for it in self.codex_items:
                it.state = False
            for it in self.agy_items:
                it.state = False
            self.custom_codex_item.state = True
            self.selected_engine = "codex"
            self.selected_model = custom_model
            self.update_engine_login_ui()
            self.update_profile_summary()

    def on_select_agy_model(self, sender):
        for it in self.codex_items:
            it.state = False
        for it in self.agy_items:
            it.state = False
        sender.state = True
        self.selected_engine = "agy"
        self.selected_model = sender.slug
        self.update_engine_login_ui()
        self.update_profile_summary()

    def on_input_custom_agy_model(self, _):
        window = rumps.Window(
            message="사용할 Antigravity(agy) 모델명을 입력하세요:\n(예: Gemini 3.8 Flash (High), claude-sonnet-4-6 등)",
            title="✏️ 커스텀 AGY 모델 직접 입력",
            default_text=self.selected_model if self.selected_engine == "agy" else "",
            ok="확인",
            cancel="취소",
            dimensions=(300, 24)
        )
        response = window.run()
        if response.clicked == 1 and response.text.strip():
            custom_model = response.text.strip()
            for it in self.codex_items:
                it.state = False
            for it in self.agy_items:
                it.state = False
            self.custom_agy_item.state = True
            self.selected_engine = "agy"
            self.selected_model = custom_model
            self.update_engine_login_ui()
            self.update_profile_summary()

    def on_select_effort(self, sender):
        for it in self.effort_items:
            it.state = False
        sender.state = True
        if "High" in sender.title:
            self.reasoning_effort = "high"
            self.menu_effort.title = "⚡ AI 추론 가중치: High"
        elif "Medium" in sender.title:
            self.reasoning_effort = "medium"
            self.menu_effort.title = "⚡ AI 추론 가중치: Medium"
        else:
            self.reasoning_effort = "low"
            self.menu_effort.title = "⚡ AI 추론 가중치: Low"
        self.update_profile_summary()

    def on_select_whisper(self, sender):
        for it in self.whisper_items:
            it.state = False
        sender.state = True
        if "Turbo" in sender.title:
            self.whisper_size = "large-v3-turbo"
            self.menu_whisper.title = "🔊 Whisper STT 품질: Large-v3-Turbo"
        elif "Large-v3" in sender.title:
            self.whisper_size = "large-v3"
            self.menu_whisper.title = "🔊 Whisper STT 품질: Large-v3"
        elif "Medium" in sender.title:
            self.whisper_size = "medium"
            self.menu_whisper.title = "🔊 Whisper STT 품질: Medium"
        else:
            self.whisper_size = "small"
            self.menu_whisper.title = "🔊 Whisper STT 품질: Small"
        self.update_profile_summary()

    def toggle_clipboard_copy(self, sender):
        self.auto_copy_clipboard = not sender.state
        sender.state = self.auto_copy_clipboard

    def refresh_login_status(self, _):
        self.status_item.title = "상태: 🔄 AI 상태 확인 중..."
        threading.Thread(target=self._run_refresh_login, daemon=True).start()

    def _run_refresh_login(self):
        self.update_engine_login_ui(force_refresh=True)
        self.update_profile_summary()
        codex_status, codex_msg = check_codex_status()
        agy_ready = check_agy_status()
        
        codex_desc = "정상 준비됨" if codex_status == "ready" else f"비활성 ({codex_msg})"
        agy_desc = "정상 준비됨" if agy_ready else "로그인 필요"
        msg = f"Codex: {codex_desc}\nAGY: {agy_desc}"
        rumps.notification("Record-Brief", "AI 상태 점검 완료", msg)
        self.status_item.title = "상태: 🟢 대기 중"

    def on_open_history_item(self, sender):
        folder = getattr(sender, "folder_path", None)
        if folder and folder.exists():
            subprocess.run(["open", str(folder)], check=False)

    def set_menubar_title(self, text: Optional[str]):
        """메뉴바 텍스트 설정 (Main Thread 안전 보장 및 빈 여백 완전 제거)"""
        try:
            AppHelper.callAfter(self._update_menubar_ui, text)
        except Exception:
            self._update_menubar_ui(text)

    def _update_menubar_ui(self, text: Optional[str]):
        self.title = text
        try:
            if hasattr(self, "_nsapp") and hasattr(self._nsapp, "nsstatusitem"):
                item = self._nsapp.nsstatusitem
                btn = item.button()
                if btn:
                    if text:
                        item.setLength_(AppKit.NSVariableStatusItemLength)
                        btn.setImagePosition_(AppKit.NSImageLeading)
                        btn.setTitle_(text)
                    else:
                        btn.setTitle_("")
                        btn.setImagePosition_(AppKit.NSImageOnly)
                        item.setLength_(AppKit.NSSquareStatusItemLength)
        except Exception:
            pass

    # -----------------------------------------------------
    # 실시간 녹음 파이프라인 (이모지 모래시계 제거 및 단색 텍스트 전환)
    # -----------------------------------------------------
    def toggle_recording(self, _):
        if not self.is_recording and not self.recorded_frames:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self):
        if self.recorded_frames:
            rumps.alert("저장 재시도 필요", "이전 녹음이 메모리에 남아 있습니다. 먼저 저장을 재시도해주세요.")
            return
        self.is_recording = True
        self.recorded_frames = []
        self.record_start_time = time.time()
        self.set_menubar_title(" 00:00")  # 이모지 없이 미니멀한 텍스트 시간 표시
        
        if self.active_background_jobs > 0:
            self.status_item.title = f"상태: 🔴 녹음 중... [이전 회의 {self.active_background_jobs}건 AI 작성 중]"
        else:
            self.status_item.title = "상태: 🔴 녹음 중..."
        self.toggle_record_item.title = "⏹️ 녹음 중지 및 회의록 생성"

        def audio_callback(indata, frames, time_info, status):
            if self.is_recording:
                self.recorded_frames.append(indata.copy())

        try:
            self.recording_session_dir = OUTPUTS_DIR / datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
            self.recording_session_dir.mkdir(exist_ok=False)
            if self.meeting_mode == "mixed":
                self.online_capture = OnlineAudioCapture(
                    self.recording_session_dir,
                    self.selected_device_name if self.selected_device_index is not None else "",
                )
                self.online_capture.start()
            else:
                self.audio_stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    device=self.selected_device_index,
                    callback=audio_callback
                )
                self.audio_stream.start()
            self.record_start_time = time.time()
        except Exception as e:
            self.online_capture = None
            if self.audio_stream:
                self.audio_stream.close()
                self.audio_stream = None
            rumps.alert("녹음 시작 오류", str(e))
            self.is_recording = False
            if self.active_background_jobs == 0:
                self.set_menubar_title(None)
            self.status_item.title = "상태: 🔴 마이크 오류"
            self.toggle_record_item.title = "🎙️ 실시간 녹음 시작"
            return

        threading.Thread(target=self.update_timer_loop, daemon=True).start()
        rumps.notification("Record-Brief", "녹음 시작됨", f"[{self.selected_device_name}] 회의 녹음 중입니다. 종료 시 중지를 누르세요.")

    def update_timer_loop(self):
        while self.is_recording:
            capture = self.online_capture
            if capture and capture.process.poll() is not None:
                AppHelper.callAfter(self.stop_recording)
                return
            elapsed = int(time.time() - self.record_start_time)
            m, s = divmod(elapsed, 60)
            h, m = divmod(m, 60)
            time_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
            self.set_menubar_title(f" {time_str}")
            if self.active_background_jobs > 0:
                self.status_item.title = f"상태: 🔴 녹음 중 ({time_str}) [이전 회의 {self.active_background_jobs}건 AI 작성 중]"
            else:
                self.status_item.title = f"상태: 🔴 녹음 중 ({time_str})"
            time.sleep(1)

    def stop_recording(self):
        if not self.is_recording and not self.recorded_frames:
            return
        self.is_recording = False
        self.toggle_record_item.title = "🎙️ 실시간 녹음 시작"

        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None

        was_online = self.online_capture is not None
        if was_online:
            try:
                self.online_capture.stop()
            except Exception as error:
                self.status_item.title = "상태: 🔴 녹음 오류 (원본 보존됨)"
                self.set_menubar_title(None)
                rumps.alert("온라인 녹음 오류", f"{error}\n원본 위치: {self.recording_session_dir}")
                return
            finally:
                self.online_capture = None

        # 디스크 저장 성공 후에만 메모리 버퍼를 비운다. 실패 시 다음 클릭으로 재시도한다.
        frames_snapshot = list(self.recorded_frames)
        if frames_snapshot:
            try:
                temporary = self.recording_session_dir / "recording.pending.wav"
                sf.write(str(temporary), np.concatenate(frames_snapshot, axis=0), self.sample_rate)
                temporary.replace(self.recording_session_dir / "recording.wav")
            except Exception as error:
                self.toggle_record_item.title = "💾 녹음 저장 재시도"
                self.status_item.title = "상태: 🔴 저장 실패 (메모리에 녹음 보존 중)"
                rumps.alert("녹음 저장 실패", f"{error}\n앱을 종료하지 말고 저장을 재시도해주세요.")
                return
        self.recorded_frames = []

        if not was_online and not frames_snapshot:
            rumps.notification("Record-Brief", "녹음 오류", "녹음된 오디오 데이터가 없습니다.")
            if self.active_background_jobs == 0:
                self.set_menubar_title(None)
                self.status_item.title = "상태: 🟢 대기 중"
            return

        self.active_background_jobs += 1
        self.set_menubar_title(f" 요약 중({self.active_background_jobs})..." if self.active_background_jobs > 1 else " 요약 중...")
        self.status_item.title = f"상태: ⚙️ AI 회의록 작성 중 ({self.active_background_jobs}건)..."

        # 설정값 스냅샷과 함께 완전 독립 백그라운드 스레드로 실행 (다음 회의 녹음과 동시 진행 가능)
        threading.Thread(
            target=self.process_recording_pipeline,
            args=(
                was_online,
                self.sample_rate,
                self.selected_engine,
                self.selected_model,
                self.reasoning_effort,
                self.whisper_size,
                self.summary_style,
                self.recording_session_dir,
            ),
            daemon=True
        ).start()

    def process_recording_pipeline(self, was_online, sample_rate, engine, model, effort, whisper_size, summary_style, session_dir):
        try:
            now_dt = datetime.now()
            timestamp_str = session_dir.name
            date_display = now_dt.strftime("%Y년 %m월 %d일 %H:%M:%S")
            if was_online:
                audio_path = mix_online_audio(session_dir, sample_rate)
            else:
                audio_path = session_dir / "recording.wav"
            audio_data, _ = sf.read(str(audio_path), dtype="float32")

            # 무음(신호 없음) 감지
            max_amp = float(np.max(np.abs(audio_data)))
            if max_amp < 1e-4:
                rumps.alert(
                    "마이크 무음 감지 (입력 신호 없음)",
                    f"녹음된 오디오가 완전한 무음(신호 크기: 0)입니다.\n\n"
                    "확인 및 조치 방법:\n"
                    "1. 메뉴바의 [🎤 마이크 장치]에서 다른 마이크(예: Brio 100 또는 MacBook Pro 마이크)로 변경해보세요.\n"
                    "2. 웹캠/마이크의 물리 음소거 스위치나 볼륨을 확인해주세요.\n"
                    "3. macOS '시스템 설정 > 개인정보 보호 및 보안 > 마이크' 권한을 확인해주세요."
                )
                return

            self.execute_transcription_and_notes(
                audio_path, session_dir, date_display, timestamp_str,
                engine=engine, model=model, effort=effort, whisper_size=whisper_size, summary_style=summary_style
            )

        except Exception as e:
            rumps.alert("회의록 생성 오류", str(e))
        finally:
            self.active_background_jobs = max(0, self.active_background_jobs - 1)
            # 만약 현재 다른 회의 녹음이 진행 중이지 않다면 메뉴바 상태 복원
            if not self.is_recording:
                if self.active_background_jobs > 0:
                    self.set_menubar_title(f" 요약 중({self.active_background_jobs})...")
                    self.status_item.title = f"상태: ⚙️ AI 회의록 작성 중 ({self.active_background_jobs}건)..."
                else:
                    self.set_menubar_title(None)
                    self.status_item.title = "상태: 🟢 대기 중 (완료됨)"

    # -----------------------------------------------------
    # 기존 파일 처리 파이프라인
    # -----------------------------------------------------
    def on_process_existing_file(self, _):
        if self.is_recording:
            rumps.alert("녹음 중", "실시간 녹음이 진행 중입니다. 녹음을 먼저 완료하세요.")
            return

        script = '''
        set chosenFile to choose file with prompt "회의 음성 파일을 선택하세요 (mp3, m4a, wav 등):" of type {"public.audio", "mp3", "m4a", "wav", "aac", "m4b"}
        return POSIX path of chosenFile
        '''
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout.strip():
            return
            
        file_path = Path(proc.stdout.strip())
        if not file_path.exists():
            return

        self.active_background_jobs += 1
        if not self.is_recording:
            self.set_menubar_title(f" 분석 중({self.active_background_jobs})..." if self.active_background_jobs > 1 else " 분석 중...")
            self.status_item.title = f"상태: ⚙️ 파일 분석 중 ({file_path.name})"

        threading.Thread(
            target=self.process_file_pipeline,
            args=(
                file_path,
                self.selected_engine,
                self.selected_model,
                self.reasoning_effort,
                self.whisper_size,
                self.summary_style
            ),
            daemon=True
        ).start()

    def process_file_pipeline(self, file_path: Path, engine, model, effort, whisper_size, summary_style):
        try:
            now_dt = datetime.now()
            timestamp_str = now_dt.strftime("%Y-%m-%d_%H-%M-%S-%f")
            date_display = now_dt.strftime("%Y년 %m월 %d일 %H:%M:%S")

            session_dir = OUTPUTS_DIR / timestamp_str
            session_dir.mkdir(parents=True, exist_ok=True)

            dest_audio = session_dir / file_path.name
            shutil.copy2(file_path, dest_audio)

            self.execute_transcription_and_notes(
                dest_audio, session_dir, date_display, timestamp_str,
                engine=engine, model=model, effort=effort, whisper_size=whisper_size, summary_style=summary_style
            )

        except Exception as e:
            rumps.alert("파일 회의록 생성 오류", str(e))
        finally:
            self.active_background_jobs = max(0, self.active_background_jobs - 1)
            if not self.is_recording:
                if self.active_background_jobs > 0:
                    self.set_menubar_title(f" 분석 중({self.active_background_jobs})...")
                    self.status_item.title = f"상태: ⚙️ 파일 분석 중 ({self.active_background_jobs}건)..."
                else:
                    self.set_menubar_title(None)
                    self.status_item.title = "상태: 🟢 대기 중 (완료됨)"

    # -----------------------------------------------------
    # 공통 전사 및 회의록 생성 로직 (토큰 한도 초과 시 자동 AGY 폴백)
    # -----------------------------------------------------
    def execute_transcription_and_notes(
        self,
        audio_path: Path,
        session_dir: Path,
        date_display: str,
        timestamp_str: str,
        engine: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        whisper_size: Optional[str] = None,
        summary_style: Optional[str] = None
    ):
        engine = engine or self.selected_engine
        model = model or self.selected_model
        effort = effort or self.reasoning_effort
        whisper_size = whisper_size or self.whisper_size
        summary_style = summary_style or self.summary_style
        rumps.notification("Record-Brief", "음성 전사 중...", f"Whisper({whisper_size})로 텍스트를 추출하고 있습니다.")

        transcript = transcribe_audio(audio_path, model_size=whisper_size)
        if not transcript or not transcript.strip():
            transcript = "음성 내용이 감지되지 않았습니다."
            transcript_md = session_dir / "녹취록.md"
            transcript_md.write_text(f"# 📄 전체 발화 녹취록\n\n- **일시**: {date_display}\n\n---\n\n{transcript}\n", encoding="utf-8")
            notes_file = session_dir / "회의록.md"
            notes_file.write_text(f"# 📋 회의록: [음성 미감지]\n\n> **일시**: {date_display}\n\n녹음된 파일에서 발화 내용이 감지되지 않았습니다.\n마이크 장치 설정 및 입력 볼륨을 확인해주세요.\n", encoding="utf-8")
            rumps.notification("Record-Brief", "음성 미감지 안내", "발화 음성이 감지되지 않아 회의록 생성을 건너뛰었습니다.")
            return

        transcript_md = session_dir / "녹취록.md"
        transcript_md.write_text(f"# 📄 전체 발화 녹취록\n\n- **일시**: {date_display}\n\n---\n\n{transcript}\n", encoding="utf-8")

        current_engine = engine
        current_model = model

        # 문맥 기반 화자 추정본 생성 (원본 전사 및 회의록 근거와 분리)
        diarized_md = ""
        diarize_prompt = build_diarization_prompt(transcript=transcript, date_str=date_display)
        try:
            rumps.notification("Record-Brief", "문맥 기반 화자 추정 중...", "음성 기반 화자 분리가 아니며 결과 확인이 필요합니다.")
            diarized_md = run_ai_cli(current_engine, diarize_prompt, current_model, effort)
        except Exception as e:
            err_msg = str(e)
            if "CODEX_QUOTA_EXCEEDED" in err_msg or "usage limit" in err_msg:
                rumps.notification(
                    "Record-Brief",
                    "Codex 토큰 초과 안내",
                    "OpenAI Codex 사용량 제한으로 인해 Google Antigravity(Gemini)로 자동 전환하여 계속합니다."
                )
                self.update_engine_login_ui(force_refresh=True)
                current_engine = "agy"
                current_model = "Gemini 3.8 Flash (High)"
                try:
                    diarized_md = run_ai_cli(current_engine, diarize_prompt, current_model, effort)
                except Exception as inner_e:
                    print(f"화자 추정 생성 실패: {inner_e}", file=sys.stderr)
            else:
                print(f"화자 추정 생성 실패: {err_msg}", file=sys.stderr)

        if diarized_md and diarized_md.strip():
            diarized_file = session_dir / "녹취록_화자추정.md"
            notice = "> **주의: 문맥 기반 추정** — 음성 기반 화자 분리가 아닙니다. 화자 정보는 확인이 필요합니다.\n\n"
            diarized_file.write_text(notice + diarized_md, encoding="utf-8")

        # 종합 회의록(요약 및 액션 아이템) 생성
        prompt = build_meeting_notes_prompt(transcript=transcript, date_str=date_display, style=summary_style)

        # AI 실행 및 토큰 한도 초과 시 AGY로 자동 전환
        notes_md = ""

        try:
            rumps.notification("Record-Brief", "회의록 작성 중...", f"{current_engine.upper()}({current_model})로 작성하고 있습니다.")
            notes_md = run_ai_cli(current_engine, prompt, current_model, effort)
        except Exception as e:
            err_msg = str(e)
            if "CODEX_QUOTA_EXCEEDED" in err_msg or "usage limit" in err_msg:
                # Codex 토큰 초과 감지 -> 즉시 AGY로 자동 전환하여 작성 진행!
                rumps.notification(
                    "Record-Brief",
                    "Codex 토큰 초과 안내",
                    "OpenAI Codex 사용량 제한으로 인해 Google Antigravity(Gemini)로 자동 전환하여 계속합니다."
                )
                self.update_engine_login_ui(force_refresh=True)
                current_engine = "agy"
                current_model = "Gemini 3.8 Flash (High)"
                notes_md = run_ai_cli(current_engine, prompt, current_model, effort)
            else:
                raise

        notes_file = session_dir / "회의록.md"
        notes_file.write_text(notes_md, encoding="utf-8")

        if self.auto_copy_clipboard:
            copy_to_clipboard(notes_md)

        meta = {
            "timestamp": timestamp_str,
            "created_at": date_display,
            "engine": current_engine,
            "model": current_model,
            "reasoning_effort": effort,
            "whisper_size": whisper_size,
            "meeting_mode": self.meeting_mode,
            "style": summary_style,
            "speaker_labels": "text_context_estimate_unverified",
            "audio_device": self.selected_device_name,
            "folder_path": str(session_dir)
        }
        (session_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        try:
            self.menu_history.clear()
            self.menu_history.update(self.build_recent_history_items())
        except Exception:
            pass

        clip_msg = " (클립보드 복사 완료!)" if self.auto_copy_clipboard else ""
        rumps.notification("Record-Brief", "🎉 회의록 완성!" + clip_msg, f"{timestamp_str} 폴더가 열립니다.")
        subprocess.run(["open", str(session_dir)], check=False)

    # -----------------------------------------------------
    # 기본 환경 제어
    # -----------------------------------------------------
    def open_outputs_folder(self, _):
        subprocess.run(["open", str(OUTPUTS_DIR)], check=False)

    def toggle_autostart(self, sender):
        is_enable = not sender.state
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_file = plist_dir / "com.recordbrief.menubar.plist"
        python_bin = BASE_DIR / "venv" / "bin" / "python3"
        script_file = BASE_DIR / "menu_app.py"

        if is_enable:
            plist_dir.mkdir(parents=True, exist_ok=True)
            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.recordbrief.menubar</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{script_file}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""
            plist_file.write_text(plist_content, encoding="utf-8")
            subprocess.run(["launchctl", "unload", str(plist_file)], check=False)
            subprocess.run(["launchctl", "load", str(plist_file)], check=False)
            sender.state = True
            rumps.notification("Record-Brief", "자동 시작 설정됨", "Mac 부팅 시 상단 메뉴바에 자동 상주합니다.")
        else:
            if plist_file.exists():
                subprocess.run(["launchctl", "unload", str(plist_file)], check=False)
                plist_file.unlink(missing_ok=True)
            sender.state = False
            rumps.notification("Record-Brief", "자동 시작 해제됨", "컴퓨터 시작 시 자동 실행이 해제되었습니다.")

    def quit_application(self, _):
        if self.is_recording or self.recorded_frames:
            rumps.alert("녹음 중에는 종료할 수 없습니다", "먼저 ‘녹음 중지 및 회의록 생성’을 눌러 녹음을 저장해주세요.")
            return
        if self.active_background_jobs:
            rumps.alert("저장 및 회의록 생성 중", "작업이 끝난 뒤 종료해주세요. 처리 중인 녹음과 회의록을 보호하기 위해 종료를 보류합니다.")
            return
        rumps.quit_application()


if __name__ == "__main__":
    if not acquire_single_instance_lock():
        print("Record-Brief가 이미 실행 중입니다. 중복 실행을 방지합니다.", file=sys.stderr)
        sys.exit(0)
    RecordBriefApp().run()
