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
    /// Called on the main thread when audio I/O has died and could not be
    /// revived. The session must fail visibly rather than keep showing an active
    /// conversation with a dead microphone.
    var onEngineFailure: ((Error) -> Void)? { get set }
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
    case captureStalled
    case interruptionEndedWithoutResume
    case restartFailed(String)

    var errorDescription: String? {
        switch self {
        case .converterUnavailable:
            "Could not initialize audio conversion."
        case .simulatorLiveInputDisabled:
            "Live microphone input is disabled in the iOS Simulator. Set FA_ALLOW_SIMULATOR_MIC=1 to try the simulator microphone."
        case .captureStalled:
            "The microphone stopped delivering audio."
        case .interruptionEndedWithoutResume:
            "The voice session was interrupted and could not be resumed."
        case .restartFailed(let reason):
            "The audio engine could not be restarted: \(reason)"
        }
    }
}

/// Decides how the capture-liveness watchdog reacts to a stalled microphone.
/// Pure so the escalation policy is unit-testable without audio hardware.
enum CaptureStallAction: Equatable {
    /// Capture is healthy (or paused for a system interruption); do nothing.
    case wait
    /// Capture stalled; try rebuilding and restarting the engine.
    case restart
    /// Capture stalled again after a restart already ran; give up and surface
    /// the failure.
    case fail

    static func decide(
        sinceLastCapture: Duration,
        stallThreshold: Duration,
        isInterrupted: Bool,
        didAlreadyRestartForThisStall: Bool
    ) -> CaptureStallAction {
        guard !isInterrupted, sinceLastCapture >= stallThreshold else {
            return .wait
        }
        return didAlreadyRestartForThisStall ? .fail : .restart
    }
}

/// Tracks the watchdog's stall escalation across ticks: stall once → restart,
/// stall again without a real capture in between → fail. Pure so the sequence is
/// unit-testable without audio hardware or real timers.
///
/// The subtlety it encodes: `buildGraphAndStart()` seeds `lastCaptureAt` on every
/// rebuild, so a restarted-but-still-dead engine looks momentarily "fresh". The
/// escalation flag must therefore only reset when the *capture counter* advances
/// (a genuine tap callback), never merely because `sinceLastCapture` dipped below
/// the threshold — otherwise a restart that never revives capture would loop
/// forever instead of failing on the next stall.
struct StallEscalation {
    private var didRestartForThisStall = false
    private var lastObservedCaptureCount: UInt64

    init(captureCount: UInt64) {
        lastObservedCaptureCount = captureCount
    }

    mutating func step(
        sinceLastCapture: Duration,
        stallThreshold: Duration,
        isInterrupted: Bool,
        captureCount: UInt64
    ) -> CaptureStallAction {
        let action = CaptureStallAction.decide(
            sinceLastCapture: sinceLastCapture,
            stallThreshold: stallThreshold,
            isInterrupted: isInterrupted,
            didAlreadyRestartForThisStall: didRestartForThisStall
        )
        switch action {
        case .wait:
            if captureCount != lastObservedCaptureCount {
                didRestartForThisStall = false
            }
        case .restart:
            didRestartForThisStall = true
        case .fail:
            break
        }
        lastObservedCaptureCount = captureCount
        return action
    }
}

/// `AVAudioEngine`-backed full-duplex audio for voice mode.
///
/// Capture runs through the engine's voice-processing IO (hardware acoustic echo
/// cancellation) so the assistant's own speech is not fed back into the
/// microphone, and resamples to the 16 kHz mono Int16 the Live API requires.
/// Assistant audio (24 kHz) is scheduled on a player node; ``flushPlayback()``
/// drops queued audio for instant barge-in.
///
/// Resilience: engaging voice-processing IO makes the engine stop itself right
/// after `start()` and post `AVAudioEngineConfigurationChangeNotification`
/// (`isRunning` silently flips to false — no error is thrown), so that
/// notification is observed from before the first start and answered by
/// rebuilding the graph with freshly-read formats and restarting, per Apple's
/// guidance (WWDC19 session 510, AVEchoTouch sample). A capture-liveness
/// watchdog backstops any stop we fail to observe: a session that looks alive
/// but has a dead microphone must fail visibly instead.
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
        var lastCaptureAt: ContinuousClock.Instant?
        /// Incremented only by a real tap callback. The watchdog uses this to
        /// tell a genuine capture from the `lastCaptureAt` it seeds on a rebuild,
        /// so a restart that never revives capture still escalates to a failure.
        var captureCount: UInt64 = 0
    }

    var onCapturedAudio: (@Sendable (Data) -> Void)? {
        get { tapState.withLock { $0.onCaptured } }
        set { tapState.withLock { $0.onCaptured = newValue } }
    }

    var onInputLevel: (@Sendable (Double) -> Void)? {
        get { tapState.withLock { $0.onInputLevel } }
        set { tapState.withLock { $0.onInputLevel = newValue } }
    }

    var onEngineFailure: ((Error) -> Void)?

    // Recreated on a media-services reset (which invalidates every audio object),
    // so the engine-scoped configuration-change observer must be rebound too.
    private var engine = AVAudioEngine()
    private var playerNode = AVAudioPlayerNode()
    private let playbackFormat: AVAudioFormat
    private let tapState = OSAllocatedUnfairLock(initialState: TapState())
    private let logger = Logger(subsystem: "com.familyassistant.app", category: "voice-audio")
    private var isRunning = false
    private var isInterrupted = false
    private var didFail = false
    private var observers: [NSObjectProtocol] = []
    private var watchdogTask: Task<Void, Never>?

    /// No capture callback for this long means the engine is dead even though no
    /// notification said so. Generous: a healthy tap fires many times per second.
    private static let stallThreshold: Duration = .seconds(10)
    private static let watchdogInterval: Duration = .seconds(5)

    init() {
        playbackFormat = VoiceAudioFormat.pcm16(sampleRate: VoiceAudioFormat.outputSampleRate)
            ?? AVAudioFormat(standardFormatWithSampleRate: VoiceAudioFormat.outputSampleRate, channels: 1)!
    }

    func start() async throws {
        // All control-plane state (flags, observers, graph) is confined to the
        // main thread: the notification handlers and watchdog run there, and the
        // configuration-change notification can fire while this method is still
        // mid-start, so the start body must be serialized with them.
        try await MainActor.run { try startOnMainThread() }
    }

    private func startOnMainThread() throws {
        guard !isRunning else { return }
        #if targetEnvironment(simulator)
            guard ProcessInfo.processInfo.environment["FA_ALLOW_SIMULATOR_MIC"] == "1" else {
                throw VoiceAudioError.simulatorLiveInputDisabled
            }
        #endif
        try configureSession()

        do {
            // Voice processing must be toggled while the engine is stopped, and
            // the input format must be read only after it, because engaging the
            // voice-processing IO unit changes the hardware format.
            try? engine.inputNode.setVoiceProcessingEnabled(true)
            // Observe from before the first start: engaging voice processing
            // makes the engine stop itself and post a configuration change
            // immediately after `engine.start()` returns.
            registerObservers()
            try buildGraphAndStart()
        } catch {
            // Unwind any partial setup (activated session, attached player,
            // installed tap, registered observers) so a later attempt starts
            // from a clean state instead of failing on an already-installed tap
            // or a still-active session.
            removeObservers()
            teardownGraph()
            throw error
        }

        isRunning = true
        didFail = false
        startWatchdog()
    }

    func stop() {
        guard isRunning else { return }
        isRunning = false
        watchdogTask?.cancel()
        watchdogTask = nil
        removeObservers()
        teardownGraph()
    }

    /// Attach the player, install the capture tap using the current hardware
    /// input format, and start the engine. The graph must be (re)built with
    /// freshly-read formats every time: a configuration change invalidates both
    /// the input format and the player node's connection.
    private func buildGraphAndStart() throws {
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard let converter = StreamingPCMConverter(inputFormat: inputFormat) else {
            throw VoiceAudioError.converterUnavailable
        }
        tapState.withLock {
            $0.converter = converter
            $0.lastCaptureAt = .now
        }

        engine.attach(playerNode)
        engine.connect(playerNode, to: engine.mainMixerNode, format: playbackFormat)

        input.installTap(onBus: 0, bufferSize: 2048, format: inputFormat) { [weak self] buffer, _ in
            guard let self else { return }
            let (muted, converter, audioCallback, levelCallback) = self.tapState.withLock {
                $0.lastCaptureAt = .now
                $0.captureCount &+= 1
                return ($0.muted, $0.converter, $0.onCaptured, $0.onInputLevel)
            }
            guard !muted, let converter, let data = converter.convertToData(buffer) else { return }
            levelCallback?(Self.normalizedLevel(forPCM16: data))
            audioCallback?(data)
        }

        engine.prepare()
        try engine.start()
        playerNode.play()
    }

    /// Tear down the current graph (engine stopped first — manipulating nodes on
    /// a running engine can trap) and rebuild it from freshly-read formats.
    private func rebuildGraphAndRestart() throws {
        playerNode.stop()
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        if playerNode.engine != nil {
            engine.detach(playerNode)
        }
        try buildGraphAndStart()
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
        tapState.withLock {
            $0.converter = nil
            $0.lastCaptureAt = nil
        }
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

    // MARK: - Configuration changes, interruptions & resets

    private func registerObservers() {
        let center = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()
        observers.append(center.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self] _ in
            self?.handleConfigurationChange()
        })
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

    private func removeObservers() {
        observers.forEach(NotificationCenter.default.removeObserver(_:))
        observers.removeAll()
    }

    /// The engine observed an I/O configuration change (voice processing
    /// engaging, route change, sample-rate change), stopped itself, and
    /// uninitialized its graph. Rebuild and restart.
    private func handleConfigurationChange() {
        guard isRunning, !isInterrupted else { return }
        logger.info("Audio engine configuration change; rebuilding graph")
        restartOrFail()
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
            isInterrupted = true
            engine.pause()
        case .ended:
            let options = (info[AVAudioSessionInterruptionOptionKey] as? UInt)
                .map(AVAudioSession.InterruptionOptions.init(rawValue:))
            guard options?.contains(.shouldResume) == true else {
                // The system won't let us resume (e.g. another app kept the audio
                // route). We can't revive the microphone, so fail visibly rather
                // than clear `isInterrupted` and leave a paused engine that the
                // watchdog would then try to restart against the system's
                // instruction — or sit forever on a dead "Listening…" mic.
                // `isInterrupted` stays true so the watchdog is suppressed until
                // the session tears this engine down.
                failEngine(VoiceAudioError.interruptionEndedWithoutResume)
                return
            }
            isInterrupted = false
            try? AVAudioSession.sharedInstance().setActive(true, options: [])
            // The interruption may have changed the route/formats; a plain
            // start can silently come back with a dead graph, so rebuild.
            restartOrFail()
        @unknown default:
            break
        }
    }

    private func handleMediaServicesReset() {
        guard isRunning else { return }
        // The media server restarted, invalidating every audio object. Reusing
        // the old engine/player can fail or crash, so discard them, rebind the
        // engine-scoped observer to the fresh engine, and rebuild the whole graph.
        logger.info("Media services were reset; recreating audio engine")
        do {
            removeObservers()
            engine = AVAudioEngine()
            playerNode = AVAudioPlayerNode()
            try configureSession()
            try? engine.inputNode.setVoiceProcessingEnabled(true)
            registerObservers()
            try buildGraphAndStart()
        } catch {
            failEngine(error)
        }
    }

    private func restartOrFail() {
        do {
            try rebuildGraphAndRestart()
        } catch {
            failEngine(VoiceAudioError.restartFailed(error.localizedDescription))
        }
    }

    /// Surface a fatal engine failure to the session (once). The session view
    /// model reacts by failing the conversation, which reports the error and
    /// tears this engine down via ``stop()``.
    private func failEngine(_ error: Error) {
        guard isRunning, !didFail else { return }
        didFail = true
        logger.error("Voice audio engine failed: \(error.localizedDescription, privacy: .public)")
        let callback = onEngineFailure
        DispatchQueue.main.async {
            callback?(error)
        }
    }

    // MARK: - Capture-liveness watchdog

    /// Backstop for engine stops nothing notified us about: if the tap goes
    /// silent, first rebuild the graph, and if that doesn't revive capture,
    /// fail the session. Without this a dead microphone leaves the UI saying
    /// "Listening…" forever.
    private func startWatchdog() {
        watchdogTask?.cancel()
        watchdogTask = Task { @MainActor [weak self] in
            guard let initialCaptureCount = self?.tapState.withLock({ $0.captureCount }) else { return }
            var escalation = StallEscalation(captureCount: initialCaptureCount)
            while !Task.isCancelled {
                try? await Task.sleep(for: Self.watchdogInterval)
                guard let self, self.isRunning, !Task.isCancelled else { return }
                let (lastCaptureAt, captureCount) = self.tapState.withLock { ($0.lastCaptureAt, $0.captureCount) }
                let sinceLast = lastCaptureAt.map { ContinuousClock.now - $0 } ?? .zero
                let action = escalation.step(
                    sinceLastCapture: sinceLast,
                    stallThreshold: Self.stallThreshold,
                    isInterrupted: self.isInterrupted,
                    captureCount: captureCount
                )
                switch action {
                case .wait:
                    break
                case .restart:
                    self.logger.warning("Voice capture stalled; attempting engine restart")
                    self.restartOrFail()
                case .fail:
                    self.failEngine(VoiceAudioError.captureStalled)
                    return
                }
            }
        }
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

    var onEngineFailure: ((Error) -> Void)?

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
