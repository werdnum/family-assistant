import AVFoundation
import Foundation
import os

/// Microphone + speaker I/O for a live voice session. Abstracted so the session
/// view model can be unit-tested with a fake while the real implementation drives
/// `AVAudioEngine`.
protocol VoiceAudioIO: AnyObject {
    /// Called on an audio thread with each captured 16 kHz mono Int16 PCM chunk.
    var onCapturedAudio: (@Sendable (Data) -> Void)? { get set }
    /// Configure the audio session, start capture, and begin playback.
    func start() async throws
    /// Stop capture/playback and deactivate the audio session.
    func stop()
    /// Queue a chunk of assistant audio (24 kHz mono Int16 PCM) for playback.
    func enqueue(_ pcm24k: Data)
    /// Discard any queued/playing assistant audio (used on barge-in/interrupt).
    func flushPlayback()
    /// Mute or unmute the microphone without tearing down the session.
    func setMuted(_ muted: Bool)
}

/// Requests microphone permission. Wrapped in a protocol so tests can inject a
/// deterministic decision.
protocol VoiceMicrophonePermission: Sendable {
    func requestAccess() async -> Bool
}

struct SystemMicrophonePermission: VoiceMicrophonePermission {
    func requestAccess() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }
}

enum VoiceAudioError: LocalizedError {
    case converterUnavailable

    var errorDescription: String? {
        switch self {
        case .converterUnavailable:
            "Could not initialize audio conversion."
        }
    }
}

/// `AVAudioEngine`-backed full-duplex audio for voice mode.
///
/// Capture runs through the engine's voice-processing IO (hardware acoustic echo
/// cancellation) so the assistant's own speech is not fed back into the
/// microphone, and resamples to the 16 kHz mono Int16 the Live API requires.
/// Assistant audio (24 kHz) is scheduled on a player node; ``flushPlayback()``
/// drops queued audio for instant barge-in.
final class VoiceAudioEngine: VoiceAudioIO {
    var onCapturedAudio: (@Sendable (Data) -> Void)?

    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private let playbackFormat: AVAudioFormat
    private let muted = OSAllocatedUnfairLock(initialState: false)
    private var converter: StreamingPCMConverter?
    private var isRunning = false

    init() {
        playbackFormat = VoiceAudioFormat.pcm16(sampleRate: VoiceAudioFormat.outputSampleRate)
            ?? AVAudioFormat(standardFormatWithSampleRate: VoiceAudioFormat.outputSampleRate, channels: 1)!
    }

    func start() async throws {
        guard !isRunning else { return }
        try configureSession()

        let input = engine.inputNode
        try? input.setVoiceProcessingEnabled(true)
        let inputFormat = input.outputFormat(forBus: 0)
        guard let converter = StreamingPCMConverter(inputFormat: inputFormat) else {
            throw VoiceAudioError.converterUnavailable
        }
        self.converter = converter

        engine.attach(playerNode)
        engine.connect(playerNode, to: engine.mainMixerNode, format: playbackFormat)

        input.installTap(onBus: 0, bufferSize: 2048, format: inputFormat) { [weak self] buffer, _ in
            guard let self, !self.muted.withLock({ $0 }) else { return }
            if let data = self.converter?.convertToData(buffer), let callback = self.onCapturedAudio {
                callback(data)
            }
        }

        engine.prepare()
        try engine.start()
        playerNode.play()
        isRunning = true
    }

    func stop() {
        guard isRunning else { return }
        isRunning = false
        engine.inputNode.removeTap(onBus: 0)
        playerNode.stop()
        engine.stop()
        engine.detach(playerNode)
        converter = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    func enqueue(_ pcm24k: Data) {
        guard isRunning,
              let buffer = PCMConverter.makeBuffer(pcm16: pcm24k, sampleRate: VoiceAudioFormat.outputSampleRate)
        else {
            return
        }
        playerNode.scheduleBuffer(buffer, completionHandler: nil)
        if !playerNode.isPlaying {
            playerNode.play()
        }
    }

    func flushPlayback() {
        guard isRunning else { return }
        // Stopping clears all scheduled buffers; immediately restart so the next
        // assistant turn can play.
        playerNode.stop()
        playerNode.play()
    }

    func setMuted(_ muted: Bool) {
        self.muted.withLock { $0 = muted }
    }

    private func configureSession() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(
            .playAndRecord,
            mode: .voiceChat,
            options: [.allowBluetooth, .allowBluetoothA2DP, .defaultToSpeaker]
        )
        try session.setActive(true, options: [])
    }
}
