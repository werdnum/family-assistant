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
    /// Put the shared audio session on the category and mode a voice call needs.
    /// Separate from ``start()`` because under CallKit the session is activated
    /// by the system, which happens before anything may start — the category has
    /// to be in place by then or the call comes up with no audio.
    func configureAudioSession() throws
    /// Start capture and begin playback, configuring the audio session first if
    /// nobody else has.
    func start() async throws
    /// Stop capture/playback and deactivate the audio session.
    func stop()
    /// Queue a chunk of assistant audio (24 kHz mono Int16 PCM) for playback.
    func enqueue(_ pcm24k: Data)
    /// Discard any queued/playing assistant audio (used on barge-in/interrupt).
    func flushPlayback()
    /// Mute or unmute the microphone without tearing down the session.
    func setMuted(_ muted: Bool)
    /// Whether assistant audio is playing or its echo tail has yet to pass, which
    /// is when captured audio is turned down (see ``VoiceMicDucking``).
    var isDucked: Bool { get }
    /// Where audio is going right now, and whether echo cancellation is on.
    var routeSnapshot: VoiceAudioRouteSnapshot { get }
    /// Called on the main thread when assistant audio has stopped playing: the
    /// queue drained or was discarded and the echo tail has passed.
    var onPlaybackFinished: (() -> Void)? { get set }
    /// Called on the main thread with breadcrumbs about the audio path (route
    /// changes, interruptions) and with failures that do not end the session
    /// but degrade it, which are passed with their error.
    var onDiagnostic: ((String, [String: String], Error?) -> Void)? { get set }
}

/// Software ducking of captured audio, as the web client does: while assistant
/// audio plays, capture is turned down so what echo cancellation misses of the
/// assistant's own voice is too quiet to register as the user interrupting. Not
/// a mute, so a user who speaks up over the assistant still barges in.
///
/// Driven by playback, not by the server's turn events: Gemini generates audio
/// faster than it plays, so a turn is complete on the wire seconds before the
/// speakers fall silent.
struct VoiceMicDucking {
    static let duckedGain: Float = 0.1
    /// How long capture stays ducked after playback stops, so the tail of the
    /// echo in the room (or car) has died down.
    static let releaseDelay: Duration = .milliseconds(200)

    private(set) var isDucked = false
    private var pendingBuffers = 0
    /// Advanced by a flush, so completions of buffers it discarded are ignored.
    private var generation = 0
    /// Advanced by anything that makes a scheduled release stale.
    private var releaseToken = 0

    /// A buffer was scheduled; returns the generation its completion reports.
    mutating func bufferScheduled() -> Int {
        pendingBuffers += 1
        isDucked = true
        releaseToken += 1
        return generation
    }

    /// A buffer finished playing. Returns a token to release with after
    /// ``releaseDelay`` when that was the last one playing.
    mutating func bufferFinished(generation: Int) -> Int? {
        guard generation == self.generation, pendingBuffers > 0 else { return nil }
        pendingBuffers -= 1
        return pendingBuffers == 0 ? releaseToken : nil
    }

    /// Queued playback was discarded (barge-in). The echo of what already played
    /// still needs its tail, so this also returns a release token.
    mutating func flushed() -> Int? {
        generation += 1
        pendingBuffers = 0
        return isDucked ? releaseToken : nil
    }

    /// Returns whether this ended the ducking; a stale token leaves it alone.
    @discardableResult
    mutating func release(token: Int) -> Bool {
        guard isDucked, token == releaseToken, pendingBuffers == 0 else { return false }
        isDucked = false
        return true
    }

    /// Whether a buffer from `generation` is one a flush has not discarded.
    func isCurrent(generation: Int) -> Bool {
        generation == self.generation
    }

    mutating func reset() {
        generation += 1
        releaseToken += 1
        pendingBuffers = 0
        isDucked = false
    }

    /// Scale 16-bit PCM samples in place.
    static func apply(gain: Float, toPCM16 data: inout Data) {
        guard gain != 1 else { return }
        data.withUnsafeMutableBytes { rawBuffer in
            let samples = rawBuffer.bindMemory(to: Int16.self)
            for index in samples.indices {
                samples[index] = Int16(Float(samples[index]) * gain)
            }
        }
    }
}

/// Linear fade-in for assistant audio that starts after silence. Apple's echo
/// canceller takes about 200 ms to converge on what is playing, and what it has
/// not yet converged on reaches the microphone. Starting quietly keeps that echo
/// small: https://developer.apple.com/forums/thread/829784
struct VoicePlaybackRamp {
    static let frameCount = Int(VoiceAudioFormat.outputSampleRate / 4)

    private var position = Self.frameCount

    mutating func restart() {
        position = 0
    }

    /// Scales samples in place, continuing across the chunks a reply arrives in.
    mutating func apply(to samples: UnsafeMutableBufferPointer<Int16>) {
        for index in samples.indices {
            guard position < Self.frameCount else { return }
            samples[index] = Int16(Float(samples[index]) * Float(position) / Float(Self.frameCount))
            position += 1
        }
    }
}

/// What a session does and measures while the echo canceller converges: the
/// microphone sends silence for ``window`` of the session's first reply, the one
/// the canceller has never heard, and each reply's microphone and played levels
/// are kept apart for ``window`` and for the rest of the reply.
///
/// On the watch speaker the canceller was still letting echo through 1.4 s into
/// a first reply, peaking 10 dB below what was playing, and had converged by the
/// second. There the microphone stays silent for the whole first reply.
///
/// Timed from scheduling the reply's first buffer, which precedes hearing it by
/// the route's output latency. Mic levels are taken before ducking and silencing,
/// because what the canceller let through is the question they answer.
struct VoiceEchoConvergence {
    static let window: Duration = .milliseconds(500)

    let silencesWholeFirstReply: Bool

    init(silencesWholeFirstReply: Bool = false) {
        self.silencesWholeFirstReply = silencesWholeFirstReply
    }

    static func forThisPlatform() -> VoiceEchoConvergence {
        #if os(watchOS)
        VoiceEchoConvergence(silencesWholeFirstReply: true)
        #else
        VoiceEchoConvergence()
        #endif
    }

    private struct Levels {
        var micEnergy = 0.0
        var micFrames = 0
        var micPeak = 0.0
        var playedEnergy = 0.0
        var playedFrames = 0

        mutating func addMic(rms: Double, frames: Int) {
            micEnergy += rms * rms * Double(frames)
            micFrames += frames
            micPeak = max(micPeak, rms)
        }

        mutating func addPlayed(rms: Double, frames: Int) {
            playedEnergy += rms * rms * Double(frames)
            playedFrames += frames
        }

        func fields(suffix: String) -> [String: String] {
            var fields: [String: String] = [:]
            if micFrames > 0 {
                fields["mic_rms_dbfs_\(suffix)"] = Self.dBFS((micEnergy / Double(micFrames)).squareRoot())
                fields["mic_peak_dbfs_\(suffix)"] = Self.dBFS(micPeak)
            }
            if playedFrames > 0 {
                fields["played_rms_dbfs_\(suffix)"] = Self.dBFS((playedEnergy / Double(playedFrames)).squareRoot())
            }
            return fields
        }

        private static func dBFS(_ rms: Double) -> String {
            String(format: "%.1f", 20 * log10(max(rms, 0.00001)))
        }
    }

    private var replyStartedAt: ContinuousClock.Instant?
    private var replyIndex = 0
    private var converging = Levels()
    private var converged = Levels()

    mutating func replyStarted(at now: ContinuousClock.Instant) {
        replyIndex += 1
        replyStartedAt = now
        converging = Levels()
        converged = Levels()
    }

    func silencesMic(at now: ContinuousClock.Instant) -> Bool {
        guard replyIndex == 1, let replyStartedAt else { return false }
        return silencesWholeFirstReply || now - replyStartedAt < Self.window
    }

    mutating func recordCaptured(rms: Double, frames: Int, at now: ContinuousClock.Instant) {
        guard let replyStartedAt else { return }
        if now - replyStartedAt < Self.window {
            converging.addMic(rms: rms, frames: frames)
        } else {
            converged.addMic(rms: rms, frames: frames)
        }
    }

    /// `now` is when the buffer finished playing, so a buffer counts wholly on
    /// the side of ``window`` it ends on.
    mutating func recordPlayed(rms: Double, frames: Int, at now: ContinuousClock.Instant) {
        guard let replyStartedAt else { return }
        if now - replyStartedAt < Self.window {
            converging.addPlayed(rms: rms, frames: frames)
        } else {
            converged.addPlayed(rms: rms, frames: frames)
        }
    }

    /// Ends the reply and returns its levels as telemetry fields.
    mutating func replyFinished() -> [String: String] {
        var fields = ["reply_index": String(replyIndex)]
        fields.merge(converging.fields(suffix: "first_500ms")) { _, new in new }
        fields.merge(converged.fields(suffix: "after_500ms")) { _, new in new }
        replyStartedAt = nil
        return fields
    }

    mutating func reset() {
        self = VoiceEchoConvergence(silencesWholeFirstReply: silencesWholeFirstReply)
    }
}

/// The audio route as telemetry needs it. Port names are recorded, not judged,
/// apart from the one decision a session makes from them.
struct VoiceAudioRouteSnapshot: Equatable {
    var inputs: [String]
    var outputs: [String]
    /// The audio runs through a car (CarPlay), whose microphone hears the
    /// assistant through the cabin speakers.
    var isCarAudio: Bool
    /// Nil where the source has no voice-processing IO to ask.
    var voiceProcessingEnabled: Bool?

    static let unavailable = VoiceAudioRouteSnapshot(inputs: [], outputs: [], isCarAudio: false, voiceProcessingEnabled: nil)

    var telemetryFields: [String: String] {
        var fields = [
            "route_inputs": inputs.joined(separator: ","),
            "route_outputs": outputs.joined(separator: ","),
            "route_is_car_audio": String(isCarAudio),
        ]
        if let voiceProcessingEnabled {
            fields["voice_processing_enabled"] = String(voiceProcessingEnabled)
        }
        return fields
    }
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
        var ducking = VoiceMicDucking()
        var echo = VoiceEchoConvergence.forThisPlatform()
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

    var onPlaybackFinished: (() -> Void)?

    var onDiagnostic: ((String, [String: String], Error?) -> Void)?

    // Recreated on a media-services reset (which invalidates every audio object),
    // so the engine-scoped configuration-change observer must be rebound too.
    private var engine = AVAudioEngine()
    private var playerNode = AVAudioPlayerNode()
    private let playbackFormat: AVAudioFormat
    private var playbackRamp = VoicePlaybackRamp()
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

    private let activation: VoiceAudioActivationPolicy

    init(activation: VoiceAudioActivationPolicy = .selfManaged) {
        self.activation = activation
        playbackFormat = VoiceAudioFormat.pcm16(sampleRate: VoiceAudioFormat.outputSampleRate)
            ?? AVAudioFormat(standardFormatWithSampleRate: VoiceAudioFormat.outputSampleRate, channels: 1)!
    }

    func start() async throws {
        #if os(watchOS)
        try configureAudioSession()
        let activated = try await AVAudioSession.sharedInstance().activate(options: [])
        guard activated else { throw VoiceAudioError.restartFailed("Audio activation was declined.") }
        #endif
        if let signal = activation.externalSignal {
            // Configuring the category belongs to whoever activates the session,
            // because it has to be done before the activation this waits for.
            try await signal.waitForActivation()
        }
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
        try configureAudioSession()

        do {
            // Voice processing must be toggled while the engine is stopped, and
            // the input format must be read only after it, because engaging the
            // voice-processing IO unit changes the hardware format.
            enableVoiceProcessing()
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
            let now = ContinuousClock.now
            let (muted, gain, converter, audioCallback, levelCallback) = self.tapState.withLock { state in
                state.lastCaptureAt = now
                state.captureCount &+= 1
                let gain: Float = state.echo.silencesMic(at: now) ? 0
                    : state.ducking.isDucked ? VoiceMicDucking.duckedGain : 1
                return (state.muted, gain, state.converter, state.onCaptured, state.onInputLevel)
            }
            guard !muted, let converter, var data = converter.convertToData(buffer) else { return }
            let capturedRMS = Self.rms(forPCM16: data)
            self.tapState.withLock {
                $0.echo.recordCaptured(rms: capturedRMS, frames: data.count / MemoryLayout<Int16>.size, at: now)
            }
            VoiceMicDucking.apply(gain: gain, toPCM16: &data)
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
        discardQueuedPlaybackDucking()
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
            $0.ducking.reset()
            $0.echo.reset()
        }
        if activation.isSelfManaged {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    func enqueue(_ pcm24k: Data) {
        guard isRunning,
              let buffer = PCMConverter.makeBuffer(pcm16: pcm24k, sampleRate: VoiceAudioFormat.outputSampleRate),
              let channel = buffer.int16ChannelData
        else {
            return
        }
        let samples = UnsafeMutableBufferPointer(start: channel[0], count: Int(buffer.frameLength))
        // Still ducked means a gap too short for the release delay, which is an
        // underrun inside a reply rather than a new start.
        let now = ContinuousClock.now
        let (generation, startsAfterSilence) = tapState.withLock { state in
            let startsAfterSilence = !state.ducking.isDucked
            if startsAfterSilence {
                state.echo.replyStarted(at: now)
            }
            return (state.ducking.bufferScheduled(), startsAfterSilence)
        }
        if startsAfterSilence {
            playbackRamp.restart()
        }
        playbackRamp.apply(to: samples)
        let playedRMS = Self.rms(of: UnsafeBufferPointer(samples))
        let playedFrames = samples.count
        // `.dataPlayedBack`, not the default: the default fires when the data is
        // consumed, which can precede hearing it by the route's output latency.
        playerNode.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            let token = self.tapState.withLock { state in
                if state.ducking.isCurrent(generation: generation) {
                    state.echo.recordPlayed(rms: playedRMS, frames: playedFrames, at: .now)
                }
                return state.ducking.bufferFinished(generation: generation)
            }
            self.scheduleDuckingRelease(token: token)
        }
        if !playerNode.isPlaying {
            playerNode.play()
        }
    }

    func flushPlayback() {
        guard isRunning else { return }
        // Stopping clears all scheduled buffers; immediately restart so the next
        // assistant turn can play.
        discardQueuedPlaybackDucking()
        playerNode.stop()
        playerNode.play()
    }

    func setMuted(_ muted: Bool) {
        tapState.withLock { $0.muted = muted }
    }

    var isDucked: Bool {
        tapState.withLock { $0.ducking.isDucked }
    }

    /// Queued buffers are about to be dropped (flush, graph rebuild, or a
    /// discarded player) and may never report finishing, so stop waiting on them
    /// and release after the echo tail instead of staying ducked for good.
    private func discardQueuedPlaybackDucking() {
        let token = tapState.withLock { $0.ducking.flushed() }
        scheduleDuckingRelease(token: token)
    }

    private func scheduleDuckingRelease(token: Int?) {
        guard let token else { return }
        Task { [weak self] in
            try? await Task.sleep(for: VoiceMicDucking.releaseDelay)
            guard let self else { return }
            let echoLevels = self.tapState.withLock { state -> [String: String]? in
                state.ducking.release(token: token) ? state.echo.replyFinished() : nil
            }
            guard let echoLevels else { return }
            DispatchQueue.main.async { [weak self] in
                self?.playbackDidFinish(echoLevels: echoLevels)
            }
        }
    }

    private func playbackDidFinish(echoLevels: [String: String]) {
        guard isRunning else { return }
        var fields = echoLevels
        fields["output_volume"] = String(format: "%.2f", AVAudioSession.sharedInstance().outputVolume)
        recordAudioEvent("audio_echo_levels", fields: fields)
        onPlaybackFinished?()
    }

    var routeSnapshot: VoiceAudioRouteSnapshot {
        let route = AVAudioSession.sharedInstance().currentRoute
        let inputs = route.inputs.map(\.portType)
        let outputs = route.outputs.map(\.portType)
        #if os(iOS)
        let isCarAudio = inputs.contains(.carAudio) || outputs.contains(.carAudio)
        #else
        let isCarAudio = false
        #endif
        return VoiceAudioRouteSnapshot(
            inputs: inputs.map(\.rawValue),
            outputs: outputs.map(\.rawValue),
            isCarAudio: isCarAudio,
            voiceProcessingEnabled: engine.inputNode.isVoiceProcessingEnabled
        )
    }

    /// Echo cancellation is what keeps the assistant's own speech out of the
    /// microphone. Without it the session still works, but the model hears
    /// itself and cuts itself off, so the failure is reported rather than
    /// swallowed.
    private func enableVoiceProcessing() {
        do {
            try engine.inputNode.setVoiceProcessingEnabled(true)
        } catch {
            logger.error("Could not enable voice processing: \(error.localizedDescription, privacy: .public)")
            onDiagnostic?("voice_processing_failed", routeSnapshot.telemetryFields, error)
        }
    }

    private func recordAudioEvent(_ event: String, fields: [String: String] = [:]) {
        onDiagnostic?(event, routeSnapshot.telemetryFields.merging(fields) { _, new in new }, nil)
    }

    private static func normalizedLevel(forPCM16 data: Data) -> Double {
        min(1.0, rms(forPCM16: data) * 8.0)
    }

    private static func rms(forPCM16 data: Data) -> Double {
        data.withUnsafeBytes { rms(of: $0.bindMemory(to: Int16.self)) }
    }

    private static func rms(of samples: UnsafeBufferPointer<Int16>) -> Double {
        guard !samples.isEmpty else { return 0 }
        var total = 0.0
        for sample in samples {
            let value = Double(sample) / Double(Int16.max)
            total += value * value
        }
        return sqrt(total / Double(samples.count))
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
        recordAudioEvent("audio_configuration_change")
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
            recordAudioEvent("audio_interruption_began")
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
            if activation.isSelfManaged {
                try? AVAudioSession.sharedInstance().setActive(true, options: [])
            }
            // The interruption may have changed the route/formats; a plain
            // start can silently come back with a dead graph, so rebuild.
            restartOrFail()
            recordAudioEvent("audio_interruption_ended")
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
        onDiagnostic?("media_services_reset", [:], nil)
        do {
            removeObservers()
            discardQueuedPlaybackDucking()
            engine = AVAudioEngine()
            playerNode = AVAudioPlayerNode()
            try configureAudioSession()
            enableVoiceProcessing()
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

    func configureAudioSession() throws {
        let session = AVAudioSession.sharedInstance()
        #if os(watchOS)
        try session.setCategory(.playAndRecord, mode: .voiceChat)
        #else
        try session.setCategory(
            .playAndRecord,
            mode: .voiceChat,
            options: [.allowBluetoothHFP, .allowBluetoothA2DP, .defaultToSpeaker]
        )
        if activation.isSelfManaged {
            try session.setActive(true, options: [])
        }
        #endif
    }
}

#if DEBUG && targetEnvironment(simulator) && os(iOS)
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

    var onPlaybackFinished: (() -> Void)?

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

    /// Scripted audio needs no real audio session.
    func configureAudioSession() throws {}

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

    /// Scripted prompts are not a microphone, so there is no echo to duck.
    var isDucked: Bool { false }

    var routeSnapshot: VoiceAudioRouteSnapshot { .unavailable }

    var onDiagnostic: ((String, [String: String], Error?) -> Void)?

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
