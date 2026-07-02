import AVFoundation
import Foundation
import os

/// Microphone + speaker I/O for a live voice session. Abstracted so the session
/// view model can be unit-tested with a fake while the real implementation drives
/// `AVAudioEngine`.
protocol VoiceAudioIO: AnyObject {
    /// Called on an audio thread with each captured 16 kHz mono Int16 PCM chunk.
    var onCapturedAudio: (@Sendable (Data) -> Void)? { get set }
    /// Called on an audio thread with normalized microphone activity from 0...1.
    var onInputLevel: (@Sendable (Double) -> Void)? { get set }
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
    case simulatorLiveInputDisabled

    var errorDescription: String? {
        switch self {
        case .converterUnavailable:
            "Could not initialize audio conversion."
        case .simulatorLiveInputDisabled:
            "Live microphone input is disabled in the iOS Simulator. Set FA_ALLOW_SIMULATOR_MIC=1 to try the simulator microphone."
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
        var onInputLevel: (@Sendable (Double) -> Void)?
    }

    var onCapturedAudio: (@Sendable (Data) -> Void)? {
        get { tapState.withLock { $0.onCaptured } }
        set { tapState.withLock { $0.onCaptured = newValue } }
    }

    var onInputLevel: (@Sendable (Double) -> Void)? {
        get { tapState.withLock { $0.onInputLevel } }
        set { tapState.withLock { $0.onInputLevel = newValue } }
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
        #if targetEnvironment(simulator)
            guard ProcessInfo.processInfo.environment["FA_ALLOW_SIMULATOR_MIC"] == "1" else {
                throw VoiceAudioError.simulatorLiveInputDisabled
            }
        #endif
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
                let (muted, converter, audioCallback, levelCallback) = self.tapState.withLock {
                    ($0.muted, $0.converter, $0.onCaptured, $0.onInputLevel)
                }
                guard !muted, let converter, let data = converter.convertToData(buffer) else { return }
                levelCallback?(Self.normalizedLevel(forPCM16: data))
                audioCallback?(data)
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

    private static func normalizedLevel(forPCM16 data: Data) -> Double {
        guard data.count >= MemoryLayout<Int16>.size else { return 0 }
        let sampleCount = data.count / MemoryLayout<Int16>.size
        let sumSquares = data.withUnsafeBytes { rawBuffer -> Double in
            guard let samples = rawBuffer.bindMemory(to: Int16.self).baseAddress else { return 0 }
            var total = 0.0
            for index in 0 ..< sampleCount {
                let sample = Double(samples[index]) / Double(Int16.max)
                total += sample * sample
            }
            return total
        }
        let rms = sqrt(sumSquares / Double(sampleCount))
        return min(1.0, rms * 8.0)
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
            options: [.allowBluetoothHFP, .allowBluetoothA2DP, .defaultToSpeaker]
        )
        try session.setActive(true, options: [])
    }
}

#if DEBUG && targetEnvironment(simulator)
/// Simulator-only audio source for live backend smoke tests.
///
/// Some simulator/CoreAudio configurations abort inside `AVAudioEngine.inputNode`
/// before Swift can catch an error. Live UI tests need to exercise the native
/// token, WebSocket, and realtime-input path without depending on host audio
/// hardware, so this source emits scripted 16 kHz PCM speech and level updates
/// while leaving production and normal simulator launches on
/// ``VoiceAudioEngine``.
final class SimulatorVoiceAudioIO: VoiceAudioIO {
    private struct State {
        var muted = false
        var onCaptured: (@Sendable (Data) -> Void)?
        var onInputLevel: (@Sendable (Double) -> Void)?
    }

    var onCapturedAudio: (@Sendable (Data) -> Void)? {
        get { state.withLock { $0.onCaptured } }
        set { state.withLock { $0.onCaptured = newValue } }
    }

    var onInputLevel: (@Sendable (Double) -> Void)? {
        get { state.withLock { $0.onInputLevel } }
        set { state.withLock { $0.onInputLevel = newValue } }
    }

    private let state = OSAllocatedUnfairLock(initialState: State())
    private var captureTask: Task<Void, Never>?
    private let prompts: [String]
    private var synthesizer: AVSpeechSynthesizer?

    init(prompt: String = ProcessInfo.processInfo.environment["FAMILY_ASSISTANT_LIVE_AUDIO_PROMPT"] ?? "Say the word banana.") {
        let script = ProcessInfo.processInfo.environment["FAMILY_ASSISTANT_LIVE_AUDIO_SCRIPT"]
        prompts = Self.prompts(fromScript: script, fallback: prompt)
    }

    static func prompts(fromScript script: String?, fallback prompt: String) -> [String] {
        guard let script else { return [prompt] }
        let scriptedPrompts = script
            .components(separatedBy: "||")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return scriptedPrompts.isEmpty ? [prompt] : scriptedPrompts
    }

    func start() async throws {
        guard captureTask == nil else { return }
        captureTask = Task { [weak self] in
            guard let self else { return }
            await self.waitForCaptureSink()
            for (index, prompt) in self.prompts.enumerated() {
                guard !Task.isCancelled else { return }
                await self.emitSpeechPrompt(prompt)
                guard !Task.isCancelled else { return }
                await self.emitSilence(duration: .seconds(5))
                guard !Task.isCancelled else { return }
                if index < self.prompts.count - 1 {
                    do {
                        try await Task.sleep(for: .seconds(8))
                    } catch {
                        return
                    }
                }
            }
        }
    }

    func stop() {
        captureTask?.cancel()
        synthesizer?.stopSpeaking(at: .immediate)
        synthesizer = nil
        captureTask = nil
    }

    func enqueue(_ pcm24k: Data) {}

    func flushPlayback() {}

    func setMuted(_ muted: Bool) {
        state.withLock { $0.muted = muted }
    }

    private func waitForCaptureSink() async {
        while !Task.isCancelled {
            let isReady = state.withLock { $0.onCaptured != nil && !$0.muted }
            if isReady { return }
            try? await Task.sleep(for: .milliseconds(25))
        }
    }

    private func emitSpeechPrompt(_ prompt: String) async {
        await withCheckedContinuation { continuation in
            let synthesizer = AVSpeechSynthesizer()
            self.synthesizer = synthesizer
            let utterance = AVSpeechUtterance(string: prompt)
            utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
            utterance.rate = AVSpeechUtteranceDefaultSpeechRate
            var converter: StreamingPCMConverter?
            var didResume = false

            synthesizer.write(utterance) { [weak self] buffer in
                guard let self else { return }
                guard self.synthesizer === synthesizer else {
                    guard !didResume else { return }
                    didResume = true
                    continuation.resume()
                    return
                }
                guard let pcmBuffer = buffer as? AVAudioPCMBuffer else { return }
                guard pcmBuffer.frameLength > 0 else {
                    guard !didResume else { return }
                    didResume = true
                    self.synthesizer = nil
                    continuation.resume()
                    return
                }
                if converter == nil {
                    converter = StreamingPCMConverter(inputFormat: pcmBuffer.format)
                }
                guard let data = converter?.convertToData(pcmBuffer), !data.isEmpty else { return }
                self.emit(data, inputLevel: 0.55)
            }
        }
    }

    private func emitSilence(duration: Duration) async {
        let frame = Data(count: Int(VoiceAudioFormat.inputSampleRate / 20) * MemoryLayout<Int16>.size)
        let end = ContinuousClock.now.advanced(by: duration)
        while ContinuousClock.now < end, !Task.isCancelled {
            emit(frame, inputLevel: 0)
            try? await Task.sleep(for: .milliseconds(50))
        }
    }

    private func emit(_ data: Data, inputLevel: Double) {
        let (muted, captured, level) = state.withLock {
            ($0.muted, $0.onCaptured, $0.onInputLevel)
        }
        guard !muted else { return }
        captured?(data)
        level?(inputLevel)
    }
}
#endif
