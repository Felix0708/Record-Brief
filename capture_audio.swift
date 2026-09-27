import AVFoundation
import CoreMedia
import ScreenCaptureKit

// Only audio outputs are registered; no screen images are written.
@available(macOS 15.0, *)
final class AudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    let directory: URL
    let origin = CMClockGetTime(CMClockGetHostTimeClock()).seconds
    let queue = DispatchQueue(label: "recordbrief.audio")
    var files: [SCStreamOutputType: AVAudioFile] = [:]
    var writtenFrames: [SCStreamOutputType: AVAudioFramePosition] = [:]
    var failure: Error?

    init(directory: URL) { self.directory = directory }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("Audio capture stopped: \(error.localizedDescription)\n", stderr)
        queue.sync { files.removeAll() }
        exit(1)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer,
                of type: SCStreamOutputType) {
        write(sample, type: type)
    }

    func write(_ sample: CMSampleBuffer, type: SCStreamOutputType) {
        guard failure == nil, sample.isValid, sample.numSamples > 0,
              type == .audio || type == .microphone,
              let description = sample.formatDescription else { return }
        do {
            guard let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description),
                  let format = AVAudioFormat(streamDescription: asbd) else {
                throw captureError("지원하지 않는 오디오 형식입니다.")
            }
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format,
                                               frameCapacity: AVAudioFrameCount(sample.numSamples)) else {
                throw captureError("오디오 버퍼를 만들 수 없습니다.")
            }
            buffer.frameLength = buffer.frameCapacity
            let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
                sample, at: 0, frameCount: Int32(sample.numSamples), into: buffer.mutableAudioBufferList)
            guard status == noErr else { throw captureError("오디오 복사 실패: \(status)") }
            if files[type] == nil {
                let name = type == .audio ? "system.wav" : "microphone.wav"
                var settings = format.settings
                settings[AVLinearPCMIsNonInterleaved] = false
                files[type] = try AVAudioFile(forWriting: directory.appendingPathComponent(name),
                    settings: settings, commonFormat: format.commonFormat,
                    interleaved: format.isInterleaved)
            }
            let file = files[type]!
            // Both sources use the host clock. Preserve pauses and their relative start times.
            let elapsed = sample.presentationTimeStamp.seconds - origin
            guard elapsed.isFinite, abs(elapsed) < 7 * 24 * 3600 else {
                throw captureError("오디오 타임스탬프가 올바르지 않습니다.")
            }
            let target = AVAudioFramePosition(max(0, elapsed) * format.sampleRate)
            // AVAudioFile.framePosition can lag while its converter buffers stereo PCM.
            // Count accepted input frames, so converter buffering cannot shift either track.
            var written = writtenFrames[type, default: 0]
            if target > written {
                let silence = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4096)!
                silence.frameLength = silence.frameCapacity
                for audioBuffer in UnsafeMutableAudioBufferListPointer(silence.mutableAudioBufferList) {
                    if let data = audioBuffer.mData { memset(data, 0, Int(audioBuffer.mDataByteSize)) }
                }
                while written < target {
                    silence.frameLength = AVAudioFrameCount(min(4096, target - written))
                    try file.write(from: silence)
                    written += AVAudioFramePosition(silence.frameLength)
                }
            }
            try file.write(from: buffer)
            writtenFrames[type] = written + AVAudioFramePosition(buffer.frameLength)
        } catch { failure = error }
    }
}

func captureError(_ message: String) -> NSError {
    NSError(domain: "RecordBrief.Audio", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
}

#if !CAPTURE_SELF_TEST
@main
struct Main {
    static func main() async {
        do {
            guard #available(macOS 15.0, *) else { throw captureError("온라인 녹음은 macOS 15 이상이 필요합니다.") }
            guard CommandLine.arguments.count == 3 else { throw captureError("녹음 폴더와 마이크 이름이 필요합니다.") }
            guard await AVCaptureDevice.requestAccess(for: .audio) else {
                throw captureError("시스템 설정에서 Record-Brief Audio의 마이크 권한을 허용해주세요.")
            }
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first else { throw captureError("사용 가능한 디스플레이가 없습니다.") }
            let config = SCStreamConfiguration()
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            config.capturesAudio = true
            config.captureMicrophone = true
            config.sampleRate = 16000
            config.channelCount = 1
            let name = CommandLine.arguments[2]
            if !name.isEmpty {
                let devices = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external],
                    mediaType: .audio, position: .unspecified).devices
                guard let device = devices.first(where: { $0.localizedName == name }) else {
                    throw captureError("선택한 마이크를 찾을 수 없습니다: \(name)")
                }
                config.microphoneCaptureDeviceID = device.uniqueID
            }
            let capture = AudioCapture(directory: URL(fileURLWithPath: CommandLine.arguments[1]))
            let stream = SCStream(filter: SCContentFilter(display: display, excludingWindows: []),
                                  configuration: config, delegate: capture)
            try stream.addStreamOutput(capture, type: .audio, sampleHandlerQueue: capture.queue)
            try stream.addStreamOutput(capture, type: .microphone, sampleHandlerQueue: capture.queue)
            try await stream.startCapture()
            print("READY")
            fflush(stdout)
            // EOF also stops capture if the parent app disappears.
            _ = await Task.detached { readLine() }.value
            try await stream.stopCapture()
            let failure = capture.queue.sync { () -> Error? in
                let error = capture.failure
                let hasMic = capture.files[.microphone] != nil
                capture.files.removeAll() // finalize WAV headers before reporting completion
                return error ?? (hasMic ? nil : captureError("마이크 데이터가 수신되지 않았습니다."))
            }
            if let failure { throw failure }
        } catch {
            fputs("\(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}
#endif
