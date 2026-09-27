// Record-Brief 프론트엔드 핵심 로직

let currentMode = 'mixed'; // 'mixed' | 'mic' | 'file'
let mediaRecorder = null;
let recordedChunks = [];
let audioContext = null;
let audioDestination = null;
let activeStreams = [];
let timerInterval = null;
let secondsElapsed = 0;
let visualizerAnimationId = null;
let speechRecognition = null;
let accumulatedTranscript = '';
let selectedFile = null;
let currentResultFolderPath = '';

// DOM 요소 캐싱
const modeMixedBtn = document.getElementById('modeMixedBtn');
const modeMicBtn = document.getElementById('modeMicBtn');
const modeFileBtn = document.getElementById('modeFileBtn');
const recordingBox = document.getElementById('recordingBox');
const fileUploadBox = document.getElementById('fileUploadBox');
const timerDisplay = document.getElementById('timerDisplay');
const startRecordBtn = document.getElementById('startRecordBtn');
const stopRecordBtn = document.getElementById('stopRecordBtn');
const liveStatusIndicator = document.getElementById('liveStatusIndicator');
const liveTranscriptText = document.getElementById('liveTranscriptText');
const visualizerCanvas = document.getElementById('visualizerCanvas');
const canvasCtx = visualizerCanvas.getContext('2d');
const aiEngineSelect = document.getElementById('aiEngine');
const aiModelSelect = document.getElementById('aiModel');
const customModelGroup = document.getElementById('customModelGroup');
const customModelInput = document.getElementById('customModelInput');
const reasoningEffortSelect = document.getElementById('reasoningEffort');
const whisperSizeSelect = document.getElementById('whisperSize');
const autostartToggle = document.getElementById('autostartToggle');
const micGainSlider = document.getElementById('micGainSlider');
const sysGainSlider = document.getElementById('sysGainSlider');
const micGainVal = document.getElementById('micGainVal');
const sysGainVal = document.getElementById('sysGainVal');
const participantsInput = document.getElementById('participantsInput');
const dropZone = document.getElementById('dropZone');
const audioFileInput = document.getElementById('audioFileInput');
const selectedFileInfo = document.getElementById('selectedFileInfo');
const selectedFileName = document.getElementById('selectedFileName');
const removeFileBtn = document.getElementById('removeFileBtn');
const processFileBtn = document.getElementById('processFileBtn');
const loadingOverlay = document.getElementById('loadingOverlay');
const loadingTitle = document.getElementById('loadingTitle');
const resultSection = document.getElementById('resultSection');
const resultFolderPath = document.getElementById('resultFolderPath');
const openFolderBtn = document.getElementById('openFolderBtn');
const copyMarkdownBtn = document.getElementById('copyMarkdownBtn');
const meetingNotesViewer = document.getElementById('meetingNotesViewer');
const transcriptViewer = document.getElementById('transcriptViewer');
const historyList = document.getElementById('historyList');
const refreshHistoryBtn = document.getElementById('refreshHistoryBtn');

// 활성 GainNode 참조 (실시간 볼륨 가중치 조절용)
let activeMicGainNode = null;
let activeSysGainNode = null;

// 오디오 볼륨 가중치 슬라이더 이벤트
if (micGainSlider && micGainVal) {
  micGainSlider.addEventListener('input', (e) => {
    const val = e.target.value;
    micGainVal.textContent = `${val}%`;
    if (activeMicGainNode) activeMicGainNode.gain.value = val / 100;
  });
}

if (sysGainSlider && sysGainVal) {
  sysGainSlider.addEventListener('input', (e) => {
    const val = e.target.value;
    sysGainVal.textContent = `${val}%`;
    if (activeSysGainNode) activeSysGainNode.gain.value = val / 100;
  });
}

// 엔진별 사용 가능한 최신 모델 정의 (캡처 화면의 Codex 모델 100% 반영)
const ENGINE_MODELS = {
  codex: [
    { value: '', label: '기본 모델 (GPT-6-Astra)' },
    { value: 'gpt-6-astra', label: '1. GPT-6-Astra (Frontier 최상위 지능)' },
    { value: 'gpt-6-sol', label: '2. GPT-6-Sol (업무/코딩 메인 모델)' },
    { value: 'gpt-6-luna', label: '3. GPT-6-Luna (빠르고 경제적인 모델)' },
    { value: 'gpt-5.6-sol', label: '4. GPT-5.6-Sol (복잡한 작업용)' },
    { value: 'gpt-5.6-terra', label: '5. GPT-5.6-Terra (균형 잡힌 모델)' },
    { value: 'gpt-5.6-luna', label: '6. GPT-5.6-Luna (효율적인 고속 모델)' },
    { value: 'gpt-5.5', label: '7. GPT-5.5 (레거시 모델)' },
    { value: '__custom__', label: '✏️ 직접 입력 (Custom Model Name)' }
  ],
  agy: [
    { value: '', label: '기본 모델 (Gemini 3.8 Flash)' },
    { value: 'Gemini 3.8 Flash (High)', label: 'Gemini 3.8 Flash (초고속 / 기본)' },
    { value: 'Claude Sonnet 4.6 (Thinking)', label: 'Claude Sonnet 4.6 (정밀 분석/추론)' },
    { value: 'Claude Opus 4.6 (Thinking)', label: 'Claude Opus 4.6 (최고 성능)' },
    { value: 'Gemini 3.1 Pro (High)', label: 'Gemini 3.1 Pro (심층 분석)' },
    { value: 'Gemini 3.7 Flash (High)', label: 'Gemini 3.7 Flash (High)' },
    { value: 'GPT-OSS 120B (Medium)', label: 'GPT-OSS 120B (Medium)' },
    { value: '__custom__', label: '✏️ 직접 입력 (Custom Model Name)' }
  ]
};

function updateModelDropdown() {
  const engine = aiEngineSelect.value;
  const models = ENGINE_MODELS[engine] || [];
  aiModelSelect.innerHTML = models.map(m => `<option value="${m.value}">${m.label}</option>`).join('');
  checkCustomModelVisibility();
}

function checkCustomModelVisibility() {
  if (aiModelSelect.value === '__custom__') {
    customModelGroup.classList.remove('hidden');
  } else {
    customModelGroup.classList.add('hidden');
  }
}

// 엔진 로그인 상태 확인 및 비활성화(disabled) 처리
const engineAlertBanner = document.getElementById('engineAlertBanner');
const engineAlertText = document.getElementById('engineAlertText');

async function checkEngineStatus() {
  try {
    const res = await fetch('/api/engine-status');
    if (!res.ok) return;
    const data = await res.json();
    
    const codexOpt = aiEngineSelect.querySelector('option[value="codex"]');
    const agyOpt = aiEngineSelect.querySelector('option[value="agy"]');

    let codexReady = data.codex && data.codex.logged_in;
    let agyReady = data.agy && data.agy.logged_in;

    if (codexOpt) {
      if (!codexReady) {
        codexOpt.disabled = true;
        codexOpt.textContent = `OpenAI Codex (${data.codex.message || '로그인 필요'})`;
      } else {
        codexOpt.disabled = false;
        codexOpt.textContent = "OpenAI Codex CLI (정상 연결됨)";
      }
    }

    if (agyOpt) {
      if (!agyReady) {
        agyOpt.disabled = true;
        agyOpt.textContent = `Antigravity CLI (${data.agy.message || '인증 필요'})`;
      } else {
        agyOpt.disabled = false;
        agyOpt.textContent = "Antigravity CLI (정상 연결됨)";
      }
    }

    // 현재 선택된 옵션이 disabled면 사용 가능한 옵션으로 자동 변경
    if (aiEngineSelect.selectedOptions.length === 0 || aiEngineSelect.selectedOptions[0].disabled) {
      if (codexReady) {
        aiEngineSelect.value = 'codex';
      } else if (agyReady) {
        aiEngineSelect.value = 'agy';
      }
    }

    // 둘 다 로그인 안 되어 있을 때 경고 배너
    if (!codexReady && !agyReady) {
      if (engineAlertBanner && engineAlertText) {
        engineAlertBanner.classList.remove('hidden');
        engineAlertText.textContent = "⚠️ 사용 가능한 AI 엔진이 없습니다. 터미널에서 'codex login'을 실행하거나 agy를 설정해주세요.";
      }
    } else {
      if (engineAlertBanner) engineAlertBanner.classList.add('hidden');
    }

    updateModelDropdown();
  } catch (err) {
    console.warn("엔진 상태 확인 실패:", err);
  }
}

aiEngineSelect.addEventListener('change', updateModelDropdown);
aiModelSelect.addEventListener('change', checkCustomModelVisibility);

// Mac 로그인 시 자동 켜짐 토글 관리
async function initAutostartToggle() {
  if (!autostartToggle) return;
  try {
    const res = await fetch('/api/autostart');
    if (res.ok) {
      const data = await res.json();
      autostartToggle.checked = Boolean(data.enabled);
    }
  } catch (e) {
    console.warn("자동 켜짐 상태 조회 실패:", e);
  }

  autostartToggle.addEventListener('change', async () => {
    const isEnable = autostartToggle.checked;
    const formData = new FormData();
    formData.append('enable', isEnable);
    try {
      const res = await fetch('/api/autostart', { method: 'POST', body: formData });
      if (res.ok) {
        alert(isEnable ? "✅ Mac 켤 때 자동 실행이 활성화되었습니다!" : "ℹ️ Mac 켤 때 자동 실행이 해제되었습니다.");
      } else {
        alert("자동 켜짐 설정 변경에 실패했습니다.");
        autostartToggle.checked = !isEnable;
      }
    } catch (err) {
      alert("오류 발생: " + err.message);
      autostartToggle.checked = !isEnable;
    }
  });
}

// 1. 모드 전환
function setMode(mode) {
  currentMode = mode;
  [modeMixedBtn, modeMicBtn, modeFileBtn].forEach(btn => btn.classList.remove('active'));

  if (mode === 'mixed') {
    modeMixedBtn.classList.add('active');
    recordingBox.classList.remove('hidden');
    fileUploadBox.classList.add('hidden');
  } else if (mode === 'mic') {
    modeMicBtn.classList.add('active');
    recordingBox.classList.remove('hidden');
    fileUploadBox.classList.add('hidden');
  } else if (mode === 'file') {
    modeFileBtn.classList.add('active');
    recordingBox.classList.add('hidden');
    fileUploadBox.classList.remove('hidden');
  }
}

modeMixedBtn.addEventListener('click', () => setMode('mixed'));
modeMicBtn.addEventListener('click', () => setMode('mic'));
modeFileBtn.addEventListener('click', () => setMode('file'));

// 2. 타이머
function startTimer() {
  secondsElapsed = 0;
  timerDisplay.textContent = '00:00:00';
  clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    secondsElapsed++;
    const h = String(Math.floor(secondsElapsed / 3600)).padStart(2, '0');
    const m = String(Math.floor((secondsElapsed % 3600) / 60)).padStart(2, '0');
    const s = String(secondsElapsed % 60).padStart(2, '0');
    timerDisplay.textContent = `${h}:${m}:${s}`;
  }, 1000);
}

function stopTimer() {
  clearInterval(timerInterval);
}

// 3. 실시간 파형 비주얼라이저
function setupVisualizer(sourceNode, ctx) {
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 64;
  sourceNode.connect(analyser);

  const bufferLength = analyser.frequencyBinCount;
  const dataArray = new Uint8Array(bufferLength);

  function draw() {
    visualizerAnimationId = requestAnimationFrame(draw);
    analyser.getByteFrequencyData(dataArray);

    canvasCtx.fillStyle = '#12151e';
    canvasCtx.fillRect(0, 0, visualizerCanvas.width, visualizerCanvas.height);

    const barWidth = (visualizerCanvas.width / bufferLength) * 1.8;
    let x = 0;

    for (let i = 0; i < bufferLength; i++) {
      const barHeight = (dataArray[i] / 255) * visualizerCanvas.height * 0.85;
      
      const gradient = canvasCtx.createLinearGradient(0, visualizerCanvas.height, 0, 0);
      gradient.addColorStop(0, '#2563eb');
      gradient.addColorStop(1, '#60a5fa');

      canvasCtx.fillStyle = gradient;
      canvasCtx.fillRect(x, visualizerCanvas.height - barHeight - 4, barWidth, barHeight + 4);

      x += barWidth + 2;
    }
  }

  draw();
}

function stopVisualizer() {
  if (visualizerAnimationId) {
    cancelAnimationFrame(visualizerAnimationId);
    visualizerAnimationId = null;
  }
  canvasCtx.clearRect(0, 0, visualizerCanvas.width, visualizerCanvas.height);
}

// 4. Web Speech API (실시간 자막 & 타임스탬프)
function setupSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    liveTranscriptText.textContent = "브라우저 내장 실시간 STT 미지원 (서버의 고정밀 Whisper로 전사됩니다).";
    return;
  }

  speechRecognition = new SpeechRecognition();
  speechRecognition.continuous = true;
  speechRecognition.interimResults = true;
  speechRecognition.lang = 'ko-KR';

  accumulatedTranscript = '';
  liveTranscriptText.textContent = '말씀하시면 실시간으로 여기에 텍스트가 표시됩니다...';

  speechRecognition.onresult = (event) => {
    let interimText = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        const m = String(Math.floor(secondsElapsed / 60)).padStart(2, '0');
        const s = String(secondsElapsed % 60).padStart(2, '0');
        const line = `[${m}:${s}] ${transcript.trim()}\n`;
        accumulatedTranscript += line;
      } else {
        interimText += transcript;
      }
    }
    liveTranscriptText.textContent = (accumulatedTranscript + (interimText ? `(인식 중...) ${interimText}` : '')).trim() || '음성을 듣고 있습니다...';
    liveTranscriptText.scrollTop = liveTranscriptText.scrollHeight;
  };

  speechRecognition.onerror = (e) => {
    console.warn("SpeechRecognition 오류:", e);
  };

  speechRecognition.onend = () => {
    // 녹음 중이면 자동 재시작
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      try { speechRecognition.start(); } catch (e) {}
    }
  };

  try {
    speechRecognition.start();
  } catch (e) {
    console.warn("SpeechRecognition 시작 실패:", e);
  }
}

// 5. 오디오 녹음 시작 (대면 or 온라인 믹싱)
async function startRecording() {
  try {
    recordedChunks = [];
    activeStreams = [];
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    audioDestination = audioContext.createMediaStreamDestination();

    // 마이크 스트림
    const micStream = await navigator.mediaDevices.getUserMedia({ 
      audio: { echoCancellation: true, noiseSuppression: true } 
    });
    activeStreams.push(micStream);

    const micSource = audioContext.createMediaStreamSource(micStream);
    const micGain = audioContext.createGain();
    activeMicGainNode = micGain;
    const initialMicVal = micGainSlider ? parseFloat(micGainSlider.value) / 100 : 1.0;
    micGain.gain.value = initialMicVal;
    micSource.connect(micGain);
    micGain.connect(audioDestination);

    // 온라인 회의 모드: 시스템 오디오(스피커 소리) 추가 믹싱
    if (currentMode === 'mixed') {
      try {
        const sysStream = await navigator.mediaDevices.getDisplayMedia({
          video: true,
          audio: true
        });
        
        // 시스템 오디오 트랙이 포함되어 있는지 확인
        const audioTracks = sysStream.getAudioTracks();
        if (audioTracks.length > 0) {
          activeStreams.push(sysStream);
          const sysSource = audioContext.createMediaStreamSource(sysStream);
          const sysGain = audioContext.createGain();
          activeSysGainNode = sysGain;
          const initialSysVal = sysGainSlider ? parseFloat(sysGainSlider.value) / 100 : 1.2;
          sysGain.gain.value = initialSysVal;
          sysSource.connect(sysGain);
          sysGain.connect(audioDestination);
        } else {
          alert("주의: 화면 공유 창에서 '오디오 공유(시스템 오디오)'를 체크하지 않으셨습니다. 마이크 소리만 녹음됩니다.");
        }
      } catch (err) {
        console.warn("시스템 오디오 캡처 취소/실패:", err);
        // 취소하더라도 마이크 녹음은 계속 진행
      }
    }

    // 비주얼라이저 연결
    setupVisualizer(audioDestination, audioContext);

    // MediaRecorder 설정
    const mimeTypes = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/wav'];
    let selectedMime = '';
    for (const mime of mimeTypes) {
      if (MediaRecorder.isTypeSupported(mime)) {
        selectedMime = mime;
        break;
      }
    }

    mediaRecorder = new MediaRecorder(audioDestination.stream, selectedMime ? { mimeType: selectedMime } : undefined);

    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) {
        recordedChunks.push(e.data);
      }
    };

    mediaRecorder.onstop = async () => {
      stopTimer();
      stopVisualizer();
      liveStatusIndicator.classList.remove('recording');

      const blobType = selectedMime || 'audio/webm';
      const audioBlob = new Blob(recordedChunks, { type: blobType });
      await sendMeetingData(audioBlob, accumulatedTranscript);
    };

    mediaRecorder.start(1000); // 1초 단위 청크
    startTimer();
    setupSpeechRecognition();

    startRecordBtn.disabled = true;
    stopRecordBtn.disabled = false;
    liveStatusIndicator.classList.add('recording');

  } catch (err) {
    console.error("녹음 시작 실패:", err);
    alert(`마이크 권한을 허용해주세요: ${err.message}`);
  }
}

// 6. 녹음 종료
function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  }
  if (speechRecognition) {
    try { speechRecognition.stop(); } catch (e) {}
  }
  activeStreams.forEach(stream => stream.getTracks().forEach(t => t.stop()));
  if (audioContext && audioContext.state !== 'closed') {
    audioContext.close();
  }
  startRecordBtn.disabled = false;
  stopRecordBtn.disabled = true;
}

startRecordBtn.addEventListener('click', startRecording);
stopRecordBtn.addEventListener('click', stopRecording);

// 7. 파일 업로드 처리
dropZone.addEventListener('click', () => audioFileInput.click());

dropZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));

dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    handleFileSelected(e.dataTransfer.files[0]);
  }
});

audioFileInput.addEventListener('change', (e) => {
  if (e.target.files && e.target.files.length > 0) {
    handleFileSelected(e.target.files[0]);
  }
});

function handleFileSelected(file) {
  selectedFile = file;
  selectedFileName.textContent = `${file.name} (${(file.size / (1024 * 1024)).toFixed(1)} MB)`;
  selectedFileInfo.classList.remove('hidden');
  processFileBtn.disabled = false;
}

removeFileBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  selectedFile = null;
  audioFileInput.value = '';
  selectedFileInfo.classList.add('hidden');
  processFileBtn.disabled = true;
});

processFileBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  await sendMeetingData(selectedFile, "");
});

// 8. 서버로 전송 및 처리
async function sendMeetingData(audioFileOrBlob, transcriptText) {
  loadingOverlay.classList.remove('hidden');
  loadingTitle.textContent = "AI 회의록을 생성하고 있습니다...";
  resultSection.classList.add('hidden');

  try {
    const formData = new FormData();
    if (audioFileOrBlob instanceof File) {
      formData.append('audio', audioFileOrBlob, audioFileOrBlob.name);
    } else if (audioFileOrBlob instanceof Blob) {
      const ext = audioFileOrBlob.type.includes('mp4') ? 'm4a' : 'webm';
      formData.append('audio', audioFileOrBlob, `recording.${ext}`);
    }

    formData.append('transcript_text', transcriptText || '');
    formData.append('participants', participantsInput.value.trim());
    formData.append('engine', aiEngineSelect.value);
    
    let modelVal = aiModelSelect.value;
    if (modelVal === '__custom__') {
      modelVal = customModelInput.value.trim();
    }
    formData.append('selected_model', modelVal);
    formData.append('reasoning_effort', reasoningEffortSelect ? reasoningEffortSelect.value : 'medium');
    formData.append('whisper_size', whisperSizeSelect.value);
    formData.append('meeting_type', currentMode);

    const response = await fetch('/api/process', {
      method: 'POST',
      body: formData
    });

    if (!response.ok) {
      const errData = await response.json();
      throw new Error(errData.detail || "회의록 생성 실패");
    }

    const data = await response.json();
    renderResults(data);
    loadHistory();

  } catch (err) {
    console.error("처리 중 오류:", err);
    alert(`오류가 발생했습니다: ${err.message}`);
  } finally {
    loadingOverlay.classList.add('hidden');
  }
}

// 9. 결과 렌더링
function renderResults(data) {
  currentResultFolderPath = data.folder_path;
  resultFolderPath.textContent = data.folder_path;
  meetingNotesViewer.innerHTML = marked.parse(data.meeting_notes || '');
  transcriptViewer.textContent = data.transcript || '';
  
  resultSection.classList.remove('hidden');
  resultSection.scrollIntoView({ behavior: 'smooth' });
}

// 탭 전환
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

    btn.classList.add('active');
    const tabName = btn.getAttribute('data-tab');
    if (tabName === 'meeting-notes') {
      document.getElementById('tabMeetingNotes').classList.add('active');
    } else {
      document.getElementById('tabTranscript').classList.add('active');
    }
  });
});

// 폴더 열기 & 마크다운 복사
openFolderBtn.addEventListener('click', async () => {
  if (!currentResultFolderPath) return;
  const formData = new FormData();
  formData.append('folder_path', currentResultFolderPath);
  await fetch('/api/open-folder', { method: 'POST', body: formData });
});

copyMarkdownBtn.addEventListener('click', () => {
  const text = meetingNotesViewer.innerText;
  navigator.clipboard.writeText(text).then(() => {
    const originalText = copyMarkdownBtn.innerHTML;
    copyMarkdownBtn.innerHTML = `<i data-lucide="check"></i> 복사 완료!`;
    lucide.createIcons();
    setTimeout(() => {
      copyMarkdownBtn.innerHTML = originalText;
      lucide.createIcons();
    }, 2000);
  });
});

// 10. 히스토리 로드
async function loadHistory() {
  try {
    const res = await fetch('/api/history');
    if (!res.ok) return;
    const data = await res.json();
    const items = data.history || [];

    if (items.length === 0) {
      historyList.innerHTML = '<div class="empty-history">아직 저장된 회의록이 없습니다.</div>';
      return;
    }

    historyList.innerHTML = items.map(item => `
      <div class="history-item" onclick="openHistoryFolder('${item.folder_path}')">
        <div class="history-item-title" title="${item.title}">${item.title}</div>
        <div class="history-item-meta">
          <span>${item.folder_name}</span>
          <span>${item.engine}</span>
        </div>
      </div>
    `).join('');

  } catch (err) {
    console.warn("히스토리 로드 실패:", err);
  }
}

window.openHistoryFolder = async (folderPath) => {
  const formData = new FormData();
  formData.append('folder_path', folderPath);
  await fetch('/api/open-folder', { method: 'POST', body: formData });
};

refreshHistoryBtn.addEventListener('click', loadHistory);

// 초기화
document.addEventListener('DOMContentLoaded', () => {
  checkEngineStatus();
  initAutostartToggle();
  loadHistory();
  lucide.createIcons();
});
