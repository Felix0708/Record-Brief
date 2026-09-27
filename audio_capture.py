"""Native online audio capture; keeps both source WAVs if processing fails."""
import plistlib
import selectors
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

BASE_DIR = Path(__file__).resolve().parent


def capture_binary():
    source = BASE_DIR / "capture_audio.swift"
    work = BASE_DIR / "work"
    work.mkdir(exist_ok=True)
    binary = work / "record-brief-capture"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        info = work / "capture-info.plist"
        info.write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.recordbrief.audio",
            "CFBundleName": "Record-Brief Audio",
            "NSMicrophoneUsageDescription": "온라인 회의에서 내 목소리를 녹음합니다.",
        }))
        built = subprocess.run([
            "xcrun", "swiftc", "-parse-as-library", str(source),
            "-module-cache-path", str(work / "swift-cache"), "-o", str(binary),
            "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
            "-Xlinker", "__info_plist", "-Xlinker", str(info),
        ], capture_output=True, text=True, timeout=120)
        if built.returncode:
            raise RuntimeError("온라인 녹음 도구 빌드 실패 (Xcode Command Line Tools 필요): " + built.stderr)
    return binary


class OnlineAudioCapture:
    def __init__(self, directory, microphone_name):
        self.directory = Path(directory)
        self.microphone_name = microphone_name
        self.process = None
        self.log = None

    def start(self):
        binary = capture_binary()
        self.log = (self.directory / "capture.log").open("w+")
        try:
            self.process = subprocess.Popen(
                [str(binary), str(self.directory), self.microphone_name],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                ready = selector.select(timeout=45)
                if not ready or self.process.stdout.readline().strip() != "READY":
                    raise RuntimeError("온라인 녹음 시작 실패. 마이크 및 화면·시스템 오디오 기록 권한을 확인해주세요.")
        except Exception as error:
            self._terminate()
            self.log.seek(0)
            detail = self.log.read().strip()
            self.log.close()
            raise RuntimeError(f"{error}\n{detail}") from error

    def _terminate(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def stop(self):
        try:
            if self.process.poll() is None:
                self.process.communicate("stop\n", timeout=15)
            if self.process.returncode:
                self.log.seek(0)
                raise RuntimeError("온라인 녹음 오류: " + self.log.read().strip())
        except subprocess.TimeoutExpired as error:
            self._terminate()
            raise RuntimeError("온라인 녹음 종료 시간이 초과되었습니다. 원본 파일은 녹음 폴더에 보존됩니다.") from error
        finally:
            self.log.close()


def mix_online_audio(directory, sample_rate=16000):
    from faster_whisper.audio import decode_audio

    directory = Path(directory)
    mic = directory / "microphone.wav"
    if not mic.exists() or sf.info(mic).frames == 0:
        raise RuntimeError("마이크 녹음 데이터가 없습니다. 원본 폴더를 확인해주세요.")
    tracks = [decode_audio(str(mic), sampling_rate=sample_rate)]
    system = directory / "system.wav"
    if system.exists() and sf.info(system).frames > 0:
        tracks.append(decode_audio(str(system), sampling_rate=sample_rate))
    # ponytail: fixed equal gains; add per-source gain controls if real recordings need calibration.
    mixed = np.zeros(max(map(len, tracks)), dtype=np.float32)
    for track in tracks:
        mixed[:len(track)] += track
    peak = float(np.max(np.abs(mixed))) if len(mixed) else 0
    if peak > 1:
        mixed /= peak
    destination = directory / "recording.wav"
    sf.write(str(destination), mixed, sample_rate)
    return destination
