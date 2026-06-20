import AVFoundation
import Foundation

/// Sample rates fixed by the Gemini Live API contract.
enum VoiceAudioFormat {
    /// Microphone audio the Live API accepts.
    static let inputSampleRate: Double = 16000
    /// Assistant audio the Live API emits.
    static let outputSampleRate: Double = 24000

    /// Mono, interleaved, signed 16-bit PCM at the given rate.
    static func pcm16(sampleRate: Double) -> AVAudioFormat? {
        AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: true
        )
    }
}

/// Stateless helpers for moving between `AVAudioPCMBuffer` and the raw little-endian
/// Int16 PCM bytes used on the wire.
enum PCMConverter {
    /// Extracts mono Int16 samples from a buffer as little-endian bytes.
    static func int16Data(from buffer: AVAudioPCMBuffer) -> Data {
        guard let channel = buffer.int16ChannelData, buffer.frameLength > 0 else {
            return Data()
        }
        return Data(bytes: channel[0], count: Int(buffer.frameLength) * MemoryLayout<Int16>.size)
    }

    /// Wraps raw Int16 PCM bytes (e.g. assistant audio from Gemini) in a playable
    /// buffer at `sampleRate`. Returns `nil` for empty or odd-length input.
    static func makeBuffer(pcm16 data: Data, sampleRate: Double) -> AVAudioPCMBuffer? {
        guard data.count >= MemoryLayout<Int16>.size,
              let format = VoiceAudioFormat.pcm16(sampleRate: sampleRate)
        else {
            return nil
        }
        let frameCount = AVAudioFrameCount(data.count / MemoryLayout<Int16>.size)
        guard frameCount > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount),
              let destination = buffer.int16ChannelData
        else {
            return nil
        }
        buffer.frameLength = frameCount
        data.withUnsafeBytes { raw in
            if let base = raw.baseAddress {
                memcpy(destination[0], base, Int(frameCount) * MemoryLayout<Int16>.size)
            }
        }
        return buffer
    }
}

/// Resamples streaming microphone buffers to 16 kHz mono Int16, preserving
/// converter state across calls so sample-rate conversion stays continuous (no
/// clicks at chunk boundaries).
final class StreamingPCMConverter {
    private let converter: AVAudioConverter
    let outputFormat: AVAudioFormat

    init?(inputFormat: AVAudioFormat, outputSampleRate: Double = VoiceAudioFormat.inputSampleRate) {
        guard let outputFormat = VoiceAudioFormat.pcm16(sampleRate: outputSampleRate),
              let converter = AVAudioConverter(from: inputFormat, to: outputFormat)
        else {
            return nil
        }
        self.converter = converter
        self.outputFormat = outputFormat
    }

    /// Convert one input buffer, returning its 16 kHz Int16 bytes (or `nil` if the
    /// conversion produced no frames).
    func convertToData(_ input: AVAudioPCMBuffer) -> Data? {
        let ratio = outputFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 1024
        guard input.frameLength > 0,
              let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity)
        else {
            return nil
        }

        var consumed = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
            if consumed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            inputStatus.pointee = .haveData
            return input
        }

        guard status != .error, conversionError == nil, output.frameLength > 0 else {
            return nil
        }
        return PCMConverter.int16Data(from: output)
    }
}
