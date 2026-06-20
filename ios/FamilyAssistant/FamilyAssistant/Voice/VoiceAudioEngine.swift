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
    /// State the input tap touches on the realtime audio thread. The tap, the
    /// MainActor (start/stop/setMuted), and the view model (onCapturedAudio) all
    /// mutate these, so they live behind a lock — reading a bare `converter` or
    /// closure reference on the audio thread while the MainActor reassigns it is a
    /// data race that can crash.
    private struct TapState {
        var muted = false
        var converter: StreamingPCMConverter?
        var onCaptured: (@Sendable (Data) -> Void)?
    }

    var onCapturedAudio: (@Sendable (Data) -> Void)? {
        get { tapState.withLock { $0.onCaptured } }
        set { tapState.withLock { $0.onCaptured = newValue } }
    }

    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private let playbackFormat: AVAudioFormat
    private let tapState = OSAllocatedUnfairLock(initialState: TapState())
    private var isRunning = false
    private var observers: [NSObjectProtocol] = []

    init() {
        playbackFormat = VoiceAudioFormat.pcm16(sampleRate: VoiceAudioFormat.outputSampleRate)
            ?? AVAudioFormat(standardFormatWithSampleRate: VoiceAudioFormat.outputSampleRate, channels: 1)!
    }

    func start() async throws {
        guard !isRunning else { return }
        try configureSession()

        do {
            let input = engine.inputNode
            try? input.setVoiceProcessingEnabled(true)
            let inputFormat = input.outputFormat(forBus: 0)
            guard let converter = StreamingPCMConverter(inputFormat: inputFormat) else {
                throw VoiceAudioError.converterUnavailable
            }
            tapState.withLock { $0.converter = converter }

            engine.attach(playerNode)
            engine.connect(playerNode, to: engine.mainMixerNode, format: playbackFormat)

            input.installTap(onBus: 0, bufferSize: 2048, format: inputFormat) { [weak self] buffer, _ in
                guard let self else { return }
                let (muted, converter, callback) = self.tapState.withLock {
                    ($0.muted, $0.converter, $0.onCaptured)
                }
                guard !muted, let converter, let callback else { return }
                if let data = converter.convertToData(buffer) {
                    callback(data)
                }
            }

            engine.prepare()
            try engine.start()
        } catch {
            // Unwind any partial setup (activated session, attached player,
            // installed tap) so a later attempt starts from a clean state instead
            // of failing on an already-installed tap or a still-active session.
            teardownGraph()
            throw error
        }

        playerNode.play()
        isRunning = true
        registerSessionObservers()
    }

    func stop() {
        guard isRunning else { return }
        isRunning = false
        removeSessionObservers()
        teardownGraph()
    }

    /// Tear down the engine graph and audio session. Safe to call on a partially
    /// constructed graph (each step is a no-op if that step never happened).
    private func teardownGraph() {
        engine.inputNode.removeTap(onBus: 0)
        playerNode.stop()
        engine.stop()
        if playerNode.engine != nil {
            engine.detach(playerNode)
        }
        tapState.withLock { $0.converter = nil }
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
        tapState.withLock { $0.muted = muted }
    }

    // MARK: - Interruptions & resets

    private func registerSessionObservers() {
        let center = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()
        observers.append(center.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: session,
            queue: .main
        ) { [weak self] note in
            self?.handleInterruption(note)
        })
        observers.append(center.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification,
            object: session,
            queue: .main
        ) { [weak self] _ in
            self?.handleMediaServicesReset()
        })
    }

    private func removeSessionObservers() {
        observers.forEach(NotificationCenter.default.removeObserver(_:))
        observers.removeAll()
    }

    private func handleInterruption(_ notification: Notification) {
        guard isRunning,
              let info = notification.userInfo,
              let raw = info[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw)
        else {
            return
        }
        switch type {
        case .began:
            // The system took the audio route (call/Siri); pause and wait.
            engine.pause()
        case .ended:
            let options = (info[AVAudioSessionInterruptionOptionKey] as? UInt)
                .map(AVAudioSession.InterruptionOptions.init(rawValue:))
            if options?.contains(.shouldResume) == true {
                try? AVAudioSession.sharedInstance().setActive(true, options: [])
                try? engine.start()
                playerNode.play()
            }
        @unknown default:
            break
        }
    }

    private func handleMediaServicesReset() {
        guard isRunning else { return }
        // The media server restarted, invalidating the engine. Best-effort
        // reactivation; a full rebuild happens on the next session.
        try? AVAudioSession.sharedInstance().setActive(true, options: [])
        try? engine.start()
        playerNode.play()
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
