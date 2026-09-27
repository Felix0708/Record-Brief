// Compile with -D CAPTURE_SELF_TEST alongside capture_audio.swift; uses synthetic audio only.
import AVFoundation
import CoreMedia
import ScreenCaptureKit

@available(macOS 15.0, *)
func sample(rate: Double, channels: AVAudioChannelCount, time: Double) throws -> CMSampleBuffer {
    let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: channels)!
    let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(rate / 10))!
    pcm.frameLength = pcm.frameCapacity
    for channel in 0..<Int(channels) {
        for frame in 0..<Int(pcm.frameLength) { pcm.floatChannelData![channel][frame] = 0.25 }
    }
    var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: Int32(rate)),
        presentationTimeStamp: CMTime(seconds: time, preferredTimescale: 1_000_000_000),
        decodeTimeStamp: .invalid)
    var result: CMSampleBuffer?
    let created = CMSampleBufferCreate(allocator: kCFAllocatorDefault, dataBuffer: nil,
        dataReady: false, makeDataReadyCallback: nil, refcon: nil,
        formatDescription: format.formatDescription, sampleCount: Int(pcm.frameLength),
        sampleTimingEntryCount: 1, sampleTimingArray: &timing,
        sampleSizeEntryCount: 0, sampleSizeArray: nil, sampleBufferOut: &result)
    precondition(created == noErr)
    let copied = CMSampleBufferSetDataBufferFromAudioBufferList(result!, blockBufferAllocator: kCFAllocatorDefault,
        blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0, bufferList: pcm.audioBufferList)
    precondition(copied == noErr)
    precondition(CMSampleBufferSetDataReady(result!) == noErr)
    return result!
}

@main
struct Check {
    static func main() throws {
        if #available(macOS 15.0, *) {
            let directory = URL(fileURLWithPath: CommandLine.arguments[1])
            let capture = AudioCapture(directory: directory)
            // No SCStream is started, and no permissions or real devices are accessed.
            for (type, rate, channels) in [(SCStreamOutputType.microphone, 48000.0, 2), (.audio, 16000.0, 1)] {
                capture.write(try sample(rate: rate,
                    channels: AVAudioChannelCount(channels), time: capture.origin + 0.25), type: type)
                capture.write(try sample(rate: rate,
                    channels: AVAudioChannelCount(channels), time: capture.origin + 0.5), type: type)
            }
            if let error = capture.failure { throw error }
            capture.files.removeAll()
            for name in ["microphone.wav", "system.wav"] {
                let file = try AVAudioFile(forReading: directory.appendingPathComponent(name))
                let duration = Double(file.length) / file.processingFormat.sampleRate
                guard abs(duration - 0.6) < 0.001 else {
                    throw captureError("Timestamp alignment failed: \(name), frames=\(file.length), rate=\(file.processingFormat.sampleRate), duration=\(duration)")
                }
                let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length))!
                try file.read(into: buffer)
                let rate = file.processingFormat.sampleRate
                for (time, expected): (Double, Float) in [(0.1, 0), (0.3, 0.25), (0.4, 0), (0.55, 0.25)] {
                    precondition(abs(buffer.floatChannelData![0][Int(rate * time)] - expected) < 0.001)
                }
            }
            print("PASS: native PCM copying, channel formats, shared clock alignment, pause preservation, WAV finalization")
        }
    }
}
