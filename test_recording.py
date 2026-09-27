"""Run with ./venv/bin/python3 test_recording.py (no microphone or AI calls)."""
import tempfile
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

import audio_capture
import menu_app as app


def fake_app(directory):
    return SimpleNamespace(
        is_recording=False, recorded_frames=[], audio_stream=None, online_capture=None,
        recording_session_dir=directory, record_start_time=None, active_background_jobs=0,
        sample_rate=16000, meeting_mode="mixed", selected_device_index=None,
        selected_device_name="Default microphone", selected_engine="agy", selected_model="test",
        reasoning_effort="low", whisper_size="small", summary_style="standard",
        status_item=SimpleNamespace(title=""), toggle_record_item=SimpleNamespace(title=""),
        set_menubar_title=Mock(), update_timer_loop=Mock(), process_recording_pipeline=Mock(),
        auto_copy_clipboard=False, menu_history=Mock(), build_recent_history_items=Mock(return_value=[]),
    )


def main():
    with tempfile.TemporaryDirectory() as temporary, \
         patch.object(app.rumps, "alert") as alert, \
         patch.object(app.rumps, "notification"), \
         patch.object(app.rumps, "quit_application") as quit_app, \
         patch.object(app.threading, "Thread") as thread:
        directory = Path(temporary)
        instance = fake_app(directory)

        # Menu exit preserves live recording, pending writes, and unsaved retry buffers.
        for recording, jobs, frames in [(True, 0, []), (False, 1, []), (False, 0, [np.zeros((1, 1))])]:
            instance.is_recording, instance.active_background_jobs, instance.recorded_frames = recording, jobs, frames
            app.RecordBriefApp.quit_application(instance, None)
            quit_app.assert_not_called()
        instance.is_recording, instance.active_background_jobs, instance.recorded_frames = False, 0, []
        app.RecordBriefApp.quit_application(instance, None)
        quit_app.assert_called_once()

        # Online mode actually uses native system+mic capture, never silent mic-only fallback.
        with patch.object(app, "OUTPUTS_DIR", directory), \
             patch.object(app, "OnlineAudioCapture") as online, \
             patch.object(app.sd, "InputStream") as mic:
            app.RecordBriefApp.start_recording(instance)
            online.return_value.start.assert_called_once()
            mic.assert_not_called()
            app.RecordBriefApp.stop_recording(instance)
            online.return_value.stop.assert_called_once()
            assert thread.call_args.kwargs["args"][0] is True

            instance.active_background_jobs = 0
            online.return_value.start.side_effect = RuntimeError("permission denied")
            app.RecordBriefApp.start_recording(instance)
            assert not instance.is_recording and instance.online_capture is None
            mic.assert_not_called()

            instance.meeting_mode = "mic"
            app.RecordBriefApp.start_recording(instance)
            mic.return_value.start.assert_called_once()
            callback = mic.call_args.kwargs["callback"]
            callback(np.full((1600, 1), 0.25, dtype=np.float32), 1600, None, None)
            assert len(instance.recorded_frames) == 1
            # Disk full must retain the buffer, block exit, and allow a successful retry.
            with patch.object(app.sf, "write", side_effect=OSError("disk full")):
                app.RecordBriefApp.stop_recording(instance)
            assert len(instance.recorded_frames) == 1
            previous_quits = quit_app.call_count
            app.RecordBriefApp.quit_application(instance, None)
            assert quit_app.call_count == previous_quits
            app.RecordBriefApp.stop_recording(instance)
            assert not instance.recorded_frames
            assert sf.info(instance.recording_session_dir / "recording.wav").frames == 1600
            assert thread.call_args.kwargs["args"][0] is False

        # Different source rates/channels, unequal durations, and clipping protection.
        mic = np.zeros((48000, 2), dtype=np.float32)
        mic[12000:24000] = 0.8
        system = np.zeros(32000, dtype=np.float32)
        system[4000:8000] = 0.8
        sf.write(directory / "microphone.wav", mic, 48000)
        sf.write(directory / "system.wav", system, 16000)
        result = audio_capture.mix_online_audio(directory)
        mixed, rate = sf.read(result)
        assert rate == 16000 and len(mixed) == 32000
        assert np.max(np.abs(mixed)) <= 1
        assert abs(mixed[2000]) < 0.01 and mixed[6000] > 0.8 and abs(mixed[10000]) < 0.01
        assert (directory / "microphone.wav").exists() and (directory / "system.wav").exists()
        (directory / "system.wav").unlink()
        audio_capture.mix_online_audio(directory)  # no system playback is a valid silent source
        (directory / "microphone.wav").unlink()
        try:
            audio_capture.mix_online_audio(directory)
            raise AssertionError("Missing microphone must fail")
        except RuntimeError:
            pass

        # Estimated labels cannot become the summary's evidence, even when the model invents a name.
        instance = fake_app(directory)
        with patch.object(app, "transcribe_audio", return_value="[00:00] ORIGINAL_UTTERANCE"), \
             patch.object(app, "run_ai_cli", side_effect=["INVENTED_SPEAKER", "# Notes"]) as ai, \
             patch.object(app.subprocess, "run"):
            app.RecordBriefApp.execute_transcription_and_notes(
                instance, directory / "recording.wav", directory, "test date", "test-session",
            )
        prompt = ai.call_args_list[1].args[1]
        assert "ORIGINAL_UTTERANCE" in prompt and "INVENTED_SPEAKER" not in prompt
        estimate = (directory / "녹취록_화자추정.md").read_text()
        assert estimate.startswith("> **주의: 문맥 기반 추정**")
        assert not (directory / "녹취록_화자분리.md").exists()
        assert "미확인" in app.build_meeting_notes_prompt("test", "today")
        assert "원문 문장을 그대로 보존" in app.build_diarization_prompt("test", "today")
        assert alert.called
    root = Path(__file__).resolve().parent
    (root / "work").mkdir(exist_ok=True)
    native_test = root / "work" / "test-capture"
    subprocess.run([
        "xcrun", "swiftc", "-parse-as-library", "-D", "CAPTURE_SELF_TEST",
        str(root / "capture_audio.swift"), str(root / "test_capture.swift"),
        "-module-cache-path", str(root / "work" / "swift-cache"), "-o", str(native_test),
    ], check=True)
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run([str(native_test), temporary], check=True)
        mixed, rate = sf.read(audio_capture.mix_online_audio(Path(temporary)))
        assert rate == 16000 and abs(len(mixed) / rate - 0.6) < 0.001
        assert abs(mixed[int(rate * 0.3)] - 0.5) < 0.002
        assert abs(mixed[int(rate * 0.4)]) < 0.002
    binary = audio_capture.capture_binary()
    subprocess.run(["/usr/bin/codesign", "--verify", str(binary)], check=True)
    print("PASS: online/mic routing, permission failure, exit guards, save retry, native-to-Python mixing, speaker uncertainty, production build")


if __name__ == "__main__":
    main()
