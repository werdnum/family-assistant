import AVFoundation
import Foundation
import XCTest

@testable import FamilyAssistant

final class PCMConverterTests: XCTestCase {
    // MARK: - makeBuffer / int16Data

    func testMakeBufferRoundTripsInt16Data() throws {
        let samples: [Int16] = [1, -1, 1000, -1000, 32767, -32768]
        let data = samples.withUnsafeBytes { Data($0) }

        let buffer = try XCTUnwrap(PCMConverter.makeBuffer(pcm16: data, sampleRate: 24000))
        XCTAssertEqual(buffer.frameLength, 6)
        XCTAssertEqual(buffer.format.sampleRate, 24000)
        XCTAssertEqual(buffer.format.channelCount, 1)
        XCTAssertEqual(PCMConverter.int16Data(from: buffer), data)
    }

    func testMakeBufferRejectsEmptyOrOddLengthData() {
        XCTAssertNil(PCMConverter.makeBuffer(pcm16: Data(), sampleRate: 24000))
        XCTAssertNil(PCMConverter.makeBuffer(pcm16: Data([0x01]), sampleRate: 24000))
    }

    func testInt16DataEmptyForZeroLengthBuffer() throws {
        let format = try XCTUnwrap(VoiceAudioFormat.pcm16(sampleRate: 16000))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16))
        buffer.frameLength = 0
        XCTAssertTrue(PCMConverter.int16Data(from: buffer).isEmpty)
    }

    // MARK: - StreamingPCMConverter

    func testStreamingConverterResamples48kFloatTo16kInt16() throws {
        let inputFormat = try XCTUnwrap(
            AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 1, interleaved: false)
        )
        let converter = try XCTUnwrap(StreamingPCMConverter(inputFormat: inputFormat))
        XCTAssertEqual(converter.outputFormat.sampleRate, 16000)
        XCTAssertEqual(converter.outputFormat.commonFormat, .pcmFormatInt16)

        let inputFrames: AVAudioFrameCount = 4800 // 0.1s at 48 kHz
        let input = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: inputFrames))
        input.frameLength = inputFrames
        if let channel = input.floatChannelData {
            for frame in 0 ..< Int(inputFrames) {
                channel[0][frame] = sinf(2.0 * .pi * 220.0 * Float(frame) / 48000.0)
            }
        }

        let data = try XCTUnwrap(converter.convertToData(input))
        let outputFrames = data.count / MemoryLayout<Int16>.size
        // 0.1s at 16 kHz ≈ 1600 frames, less the samples the resampler retains in
        // its filter for continuity with the next chunk. Assert it is roughly a
        // third of the input (i.e. resampling happened) rather than an exact count.
        XCTAssertGreaterThan(outputFrames, 1200)
        XCTAssertLessThan(outputFrames, 1700)
    }

    func testStreamingConverterPassthroughSameRate() throws {
        let inputFormat = try XCTUnwrap(VoiceAudioFormat.pcm16(sampleRate: 16000))
        let converter = try XCTUnwrap(StreamingPCMConverter(inputFormat: inputFormat))

        let inputFrames: AVAudioFrameCount = 1600
        let input = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: inputFrames))
        input.frameLength = inputFrames
        if let channel = input.int16ChannelData {
            for frame in 0 ..< Int(inputFrames) {
                channel[0][frame] = Int16(truncatingIfNeeded: frame)
            }
        }

        let data = try XCTUnwrap(converter.convertToData(input))
        let outputFrames = data.count / MemoryLayout<Int16>.size
        XCTAssertEqual(outputFrames, 1600)
    }
}
