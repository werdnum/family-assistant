import Foundation

/// Abstraction over ``GeminiLiveClient`` so the session view model can be tested
/// with a fake live session.
@MainActor
protocol VoiceLiveSession: AnyObject {
    var events: AsyncStream<GeminiLiveServerEvent> { get }
    var lastError: Error? { get }
    func connect(token: EphemeralToken, activityDetection: VoiceActivityDetectionConfig) async throws
    func sendAudio(_ pcm16: Data) async throws
    func endAudioStream() async throws
    func sendToolResponses(_ responses: [GeminiFunctionResponse]) async throws
    func close()
}

extension GeminiLiveClient: VoiceLiveSession {}

/// Supplies the Gemini ephemeral token. Abstracts ``ChatAPIClient``.
@MainActor
protocol VoiceTokenProviding {
    func fetchEphemeralToken(profileID: String?) async throws -> EphemeralToken
}

extension ChatAPIClient: VoiceTokenProviding {}

/// Persists a finished voice session as its own conversation. Abstracts
/// ``ChatAPIClient``.
@MainActor
protocol VoiceTranscriptStoring {
    @discardableResult
    func saveVoiceSession(
        turns: [VoiceTranscriptEntry],
        conversationID: String?,
        profileID: String?
    ) async throws -> String
}

extension ChatAPIClient: VoiceTranscriptStoring {}

/// Orchestrates a native voice conversation: permission → token → connect →
/// stream audio both ways → tools → teardown. Owns the conversation state the
/// ``VoiceView`` renders.
@MainActor
@Observable
final class VoiceSessionViewModel {
    /// High-level lifecycle the UI renders.
    enum Phase: Equatable {
        case idle
        case requestingPermission
        case permissionDenied
        case connecting
        case active
        case finished
        case failed(String)
    }

    private(set) var phase: Phase = .idle
    private(set) var transcript = VoiceTranscript()
    private(set) var isAssistantSpeaking = false
    private(set) var inputLevel = 0.0
    private(set) var lastInputLevelAt: Date?
    var isMuted = false {
        didSet { audio.setMuted(isMuted) }
    }

    private let tokenProvider: VoiceTokenProviding
    private let toolRunner: VoiceToolRunner
    private let transcriptStore: VoiceTranscriptStoring?
    private let audio: VoiceAudioIO
    private let permission: VoiceMicrophonePermission
    private let sessionFactory: @MainActor () -> VoiceLiveSession
    /// The profile the session asks for. Nil means "whatever the backend calls
    /// default", which is only resolved to a name once the token comes back.
    private let requestedProfileID: String?
    /// The profile the session actually runs under, known from the token. The
    /// transcript is filed under it so reopening the conversation in text lands on
    /// the profile that holds its history.
    private var resolvedProfileID: String?
    private let reportError: @MainActor (Error) -> Void
    private let sessionTimeoutOverride: Duration?
    private let diagnostics: VoiceConnectionDiagnostics
    private var startupStage = "permission"
    private let connectionTimeout: Duration
    private(set) var connectionTimeoutTask: Task<Void, Never>?

    private var session: VoiceLiveSession?
    private var eventTask: Task<Void, Never>?
    private var audioPumpTask: Task<Void, Never>?
    private var timeoutTask: Task<Void, Never>?
    private var toolTasks: [String: Task<Void, Never>] = [:]
    private var toolExecutionTail: Task<Void, Never>?
    private var audioOut: AsyncStream<Data>.Continuation?
    private var didStart = false
    private var didPersist = false
    /// When the assistant's current reply started playing, so an interruption
    /// can be told apart as a real barge-in or the model hearing itself.
    private var assistantSpeechStartedAt: ContinuousClock.Instant?
    /// When tool results last went back to Gemini, until it next speaks or asks
    /// for another tool. The gap is the model's own silence, which is invisible
    /// to the backend and indistinguishable from a dropped call on the user's end.
    private var toolResultsSentAt: ContinuousClock.Instant?
    /// When the silence the user hears was last broken -- by assistant speech,
    /// by the user speaking, or by a reminder already sent. The model has no
    /// clock, so this is what decides whether its next tool result should carry
    /// one; see ``voiceReminder``.
    private var silenceBrokenAt = ContinuousClock.now
    /// The key and threshold the backend served with the token, or nil when it
    /// served neither and its instruction therefore asks for no reminder.
    private var voiceReminder: (key: String, afterSeconds: Int)?
    private var interruptionCount = 0
    private var activityDetectionProfile = "default"

    var pendingToolCallIDs: Set<String> {
        Set(toolTasks.keys)
    }

    init(
        tokenProvider: VoiceTokenProviding,
        toolExecutor: VoiceToolExecuting,
        transcriptStore: VoiceTranscriptStoring? = nil,
        audio: VoiceAudioIO = VoiceAudioEngine(),
        permission: VoiceMicrophonePermission = SystemMicrophonePermission(),
        profileID: String? = nil,
        sessionFactory: (@MainActor () -> VoiceLiveSession)? = nil,
        sessionTimeoutOverride: Duration? = nil,
        connectionTimeout: Duration = .seconds(30),
        diagnostics: VoiceConnectionDiagnostics = VoiceConnectionDiagnostics(),
        reportError: @escaping @MainActor (Error) -> Void = { _ in }
    ) {
        self.tokenProvider = tokenProvider
        toolRunner = VoiceToolRunner(executor: toolExecutor, profileID: profileID)
        self.transcriptStore = transcriptStore
        self.audio = audio
        self.permission = permission
        requestedProfileID = profileID
        resolvedProfileID = profileID
        self.diagnostics = diagnostics
        self.connectionTimeout = connectionTimeout
        self.sessionFactory = sessionFactory ?? { GeminiLiveClient(diagnostics: diagnostics) }
        self.sessionTimeoutOverride = sessionTimeoutOverride
        self.reportError = reportError
        audio.onInputLevel = { [weak self] level in
            Task { @MainActor in
                guard let self else { return }
                self.inputLevel = level
                self.lastInputLevelAt = Date()
            }
        }
        audio.onEngineFailure = { [weak self] error in
            Task { @MainActor in
                self?.fail(error)
            }
        }
        audio.onDiagnostic = { [diagnostics] event, fields, error in
            diagnostics.record(event, fields: fields, error: error)
        }
    }

    /// Whether the session has reached a terminal phase.
    var isTerminal: Bool {
        switch phase {
        case .finished, .failed, .permissionDenied:
            true
        default:
            false
        }
    }

    var hasRecentInputLevel: Bool {
        guard let lastInputLevelAt else { return false }
        return Date().timeIntervalSince(lastInputLevelAt) < 2
    }

    /// Begin the session. Safe to call once; subsequent calls are ignored.
    ///
    /// Each `await` is followed by an `isTerminal` check: the user can dismiss the
    /// screen (calling ``end()``) while we are suspended — most likely while the
    /// system microphone-permission prompt is up — and a closed screen must not go
    /// on to open a network session or the microphone in the background.
    func start() async {
        guard !didStart, !isTerminal else { return }
        didStart = true
        diagnostics.record("permission_start")
        Task { await ErrorReporter.shared.flushPersisted() }

        phase = .requestingPermission
        let granted = await permission.requestAccess()
        guard !isTerminal else { return }
        guard granted else {
            diagnostics.record("permission_denied")
            phase = .permissionDenied
            return
        }

        phase = .connecting
        startupStage = "token"
        diagnostics.record("token_start")
        connectionTimeoutTask = Task { [weak self, connectionTimeout] in
            try? await Task.sleep(for: connectionTimeout)
            guard !Task.isCancelled else { return }
            self?.fail(VoiceConnectionTimeout())
        }
        let token: EphemeralToken
        do {
            token = try await tokenProvider.fetchEphemeralToken(profileID: requestedProfileID)
        } catch {
            fail(error)
            return
        }
        guard !isTerminal else { return }

        // A server that predates `profile_id` reports none; it resolves the
        // requested profile the same way we asked for it, so keep that.
        resolvedProfileID = token.profileID ?? requestedProfileID
        toolRunner.profileID = resolvedProfileID
        if let key = token.voiceReminderKey, let afterSeconds = token.voiceReminderAfterSeconds {
            voiceReminder = (key: key, afterSeconds: afterSeconds)
        } else {
            voiceReminder = nil
        }
        silenceBrokenAt = .now

        diagnostics.record("token_received", fields: ["function_count": String(token.tools.reduce(0) {
            $0 + ($1["functionDeclarations"]?.arrayValue?.count ?? 0)
        })])
        let session = sessionFactory()
        self.session = session
        startEventLoop(session: session)
        // Bound audio activation and socket setup as well as the conversation.
        startTimeout(token: token)

        // watchOS requires an active audio session before opening its socket.
        // Start the audio engine now so assistant playback is ready immediately,
        // but do NOT forward microphone audio yet: the Live API expects clients to
        // wait for `setupComplete` before sending realtime input, so the capture
        // pump is started from the setupComplete handler.
        do {
            startupStage = "audio"
            diagnostics.record("audio_start")
            try await audio.start()
        } catch {
            fail(error)
            return
        }
        guard !isTerminal else {
            audio.stop()
            session.close()
            return
        }
        let route = audio.routeSnapshot
        diagnostics.record("audio_ready", fields: route.telemetryFields)
        let activityDetection = selectActivityDetection(config: token.config, route: route)

        do {
            startupStage = "setup"
            try await session.connect(token: token, activityDetection: activityDetection)
        } catch {
            fail(error)
            return
        }
        guard !isTerminal else {
            session.close()
            return
        }
    }

    /// A car's microphone hears the assistant through the cabin speakers, which
    /// default sensitivity takes for the user interrupting, so car audio gets
    /// its own block. The choice and anything in it that cannot be honoured are
    /// recorded, the latter as failures.
    private func selectActivityDetection(
        config: VoiceLiveConfig,
        route: VoiceAudioRouteSnapshot
    ) -> VoiceActivityDetectionConfig {
        let selected = route.isCarAudio ? config.carAudioActivityDetection : config.activityDetection
        activityDetectionProfile = route.isCarAudio ? "car_audio" : "default"
        let (wire, issues) = GeminiLiveCodec.automaticActivityDetection(for: selected)
        var fields = ["vad_profile": activityDetectionProfile]
        for (key, value) in wire {
            switch value {
            case .string(let text): fields["vad_\(key)"] = text
            case .number(let number): fields["vad_\(key)"] = String(Int(number))
            default: break
            }
        }
        diagnostics.record("vad_selected", fields: fields)
        for issue in issues {
            diagnostics.record("vad_config_invalid", fields: [
                "vad_profile": activityDetectionProfile,
                "issue": issue.telemetryName,
            ], error: issue)
        }
        return selected
    }

    /// Fields every terminal breadcrumb carries, so one record summarises how
    /// often the session was cut off.
    private var sessionSummaryFields: [String: String] {
        ["stage": startupStage, "interruption_count": String(interruptionCount), "vad_profile": activityDetectionProfile]
    }

    /// End the session at the user's request.
    func end() {
        guard !isTerminal else { return }
        diagnostics.record("ended", fields: sessionSummaryFields)
        phase = .finished
        teardown()
    }

    // MARK: - Event handling

    private func startEventLoop(session: VoiceLiveSession) {
        eventTask = Task { [weak self] in
            for await event in session.events {
                self?.handle(event, session: session)
            }
            self?.handleDisconnect(session: session)
        }
    }

    private func handle(_ event: GeminiLiveServerEvent, session: VoiceLiveSession) {
        switch event {
        case .setupComplete:
            if phase == .connecting {
                phase = .active
                connectionTimeoutTask?.cancel()
                connectionTimeoutTask = nil
                startupStage = "active"
                diagnostics.record("setup_complete")
                // The Live API is ready; begin forwarding captured microphone audio.
                startAudioPump(session: session)
            }
        case let .audio(data):
            if let toolResultsSentAt {
                diagnostics.record("audio_after_tool_results", fields: [
                    "silence_ms": Self.milliseconds(since: toolResultsSentAt)
                ])
                self.toolResultsSentAt = nil
            }
            if !isAssistantSpeaking {
                assistantSpeechStartedAt = .now
            }
            isAssistantSpeaking = true
            silenceBrokenAt = .now
            audio.enqueue(data)
        case let .outputTranscription(text):
            transcript.appendAssistant(text)
        case let .inputTranscription(text):
            // While the user is talking there is no dead air to apologise for.
            silenceBrokenAt = .now
            transcript.appendUser(text)
        case .turnComplete, .generationComplete:
            isAssistantSpeaking = false
        case .interrupted:
            recordInterruption()
            isAssistantSpeaking = false
            audio.flushPlayback()
        case let .toolCall(calls):
            handleToolCalls(calls, session: session)
        case let .toolCallCancellation(ids):
            cancelToolCalls(ids)
        case .goAway:
            // goAway warns that the server will close soon; it is not the close
            // itself. Let the in-flight turn finish — the subsequent socket close
            // drives teardown via handleDisconnect — rather than cutting it short.
            break
        }
    }

    private func recordInterruption() {
        interruptionCount += 1
        var fields = [
            "interruption_index": String(interruptionCount),
            "vad_profile": activityDetectionProfile,
            "assistant_was_speaking": String(isAssistantSpeaking),
            "mic_ducked": String(audio.isDucked),
        ]
        if isAssistantSpeaking, let assistantSpeechStartedAt {
            let elapsed = ContinuousClock.now - assistantSpeechStartedAt
            fields["assistant_speech_ms"] = String(Int(elapsed / .milliseconds(1)))
        }
        diagnostics.record("interrupted", fields: fields.merging(audio.routeSnapshot.telemetryFields) { current, _ in current })
    }

    private static func milliseconds(since instant: ContinuousClock.Instant) -> String {
        String(Int((ContinuousClock.now - instant) / .milliseconds(1)))
    }

    /// Tool names only. `call_tool`'s target is named in its `name` argument;
    /// any other tool's `name` argument is user content, and nothing else from
    /// the arguments is recorded.
    private static func toolNames(_ calls: [GeminiFunctionCall]) -> String {
        calls.map { call in
            if call.name == "call_tool", case .string(let inner) = call.args["name"], inner.count <= 64 {
                return "\(call.name):\(inner)"
            }
            return call.name
        }.joined(separator: ",")
    }

    /// Tells the model, on its way back from a tool, that its silence has run
    /// long enough to owe the user another word.
    ///
    /// A tool result is the one channel that reaches a model already waiting on
    /// one, so this is where the verdict is delivered. The reminder rides on the
    /// first result only, so a batch of calls does not repeat it once per call,
    /// and it is added beside the payload rather than replacing it.
    private func applyingSilenceReminder(
        to responses: [GeminiFunctionResponse]
    ) -> [GeminiFunctionResponse] {
        guard let voiceReminder, let first = responses.first,
              case .object(var payload) = first.response
        else {
            return responses
        }
        let silentFor = Int((ContinuousClock.now - silenceBrokenAt) / .seconds(1))
        guard silentFor >= voiceReminder.afterSeconds else { return responses }
        // Re-arm from here rather than from the assistant's next word: if it
        // ignores the reminder, the user is owed another one an interval later.
        silenceBrokenAt = .now
        payload[voiceReminder.key] = .string(
            "You have been silent for about \(silentFor) seconds. Say a few words "
                + "to the user so they know you are still working, then carry on."
        )
        var updated = responses
        updated[0] = GeminiFunctionResponse(id: first.id, name: first.name, response: .object(payload))
        return updated
    }

    private func handleToolCalls(_ calls: [GeminiFunctionCall], session: VoiceLiveSession) {
        guard !calls.isEmpty else { return }
        let keys = calls.map { $0.id ?? UUID().uuidString }
        let previousTask = toolExecutionTail
        let receivedAt = ContinuousClock.now
        var receivedFields = [
            "call_count": String(calls.count),
            "tools": Self.toolNames(calls),
            // A finished batch leaves `toolExecutionTail` set; only a batch still
            // holding entries in `toolTasks` actually delays this one.
            "queued_behind_batch": String(!toolTasks.isEmpty),
        ]
        if let toolResultsSentAt {
            receivedFields["since_tool_results_ms"] = Self.milliseconds(since: toolResultsSentAt)
            self.toolResultsSentAt = nil
        }
        diagnostics.record("tool_call_received", fields: receivedFields)
        let task = Task { [weak self] in
            guard let self else { return }
            defer {
                for key in keys {
                    self.toolTasks[key] = nil
                }
            }
            await previousTask?.value
            guard !Task.isCancelled else { return }
            let startedAt = ContinuousClock.now
            let responses = await self.toolRunner.run(calls)
            guard !Task.isCancelled else { return }
            var fields = [
                "call_count": String(responses.count),
                "error_count": String(responses.filter { $0.response["error"] != nil }.count),
                "queue_ms": String(Int((startedAt - receivedAt) / .milliseconds(1))),
                "execution_ms": Self.milliseconds(since: startedAt),
            ]
            let outgoing = self.applyingSilenceReminder(to: responses)
            do {
                try await session.sendToolResponses(outgoing)
            } catch {
                // Hanging up cancels this task and closes the socket under it;
                // that is an ordinary end, not a failure to report.
                guard !Task.isCancelled, !self.isTerminal else { return }
                // Gemini waits on these results; without them the conversation
                // goes silent for good, so end it visibly instead.
                fields["stage"] = "tool_response"
                self.diagnostics.record("tool_results_send_failed", fields: fields, error: error)
                self.fail(error)
                return
            }
            self.diagnostics.record("tool_results_sent", fields: fields)
            self.toolResultsSentAt = .now
        }
        // A Gemini tool-call event is one ordered batch. Map every call ID to
        // the shared task so cancelling any member suppresses the whole batch
        // before a response can leak stale session taint.
        for key in keys {
            toolTasks[key] = task
        }
        toolExecutionTail = task
    }

    private func cancelToolCalls(_ ids: [String]) {
        diagnostics.record("tool_call_cancelled", fields: [
            "call_count": String(ids.count),
            "pending_count": String(ids.filter { toolTasks[$0] != nil }.count),
        ])
        for id in ids {
            toolTasks[id]?.cancel()
            toolTasks[id] = nil
        }
    }

    private func handleDisconnect(session: VoiceLiveSession) {
        guard !isTerminal else { return }
        if let error = session.lastError {
            fail(error)
        } else {
            diagnostics.record("disconnected", fields: sessionSummaryFields)
            phase = .finished
            teardown()
        }
    }

    // MARK: - Audio plumbing

    private func startAudioPump(session: VoiceLiveSession) {
        let (stream, continuation) = AsyncStream.makeStream(of: Data.self)
        audioOut = continuation
        // Captured on an audio thread; the stream preserves ordering so chunks are
        // sent in capture order by the single consumer below.
        audio.onCapturedAudio = { data in
            continuation.yield(data)
        }
        audioPumpTask = Task {
            for await chunk in stream {
                try? await session.sendAudio(chunk)
            }
        }
    }

    private func startTimeout(token: EphemeralToken) {
        let duration = sessionTimeoutOverride
            ?? .seconds(max(1, token.config.maxSessionMinutes) * 60)
        timeoutTask = Task { [weak self] in
            try? await Task.sleep(for: duration)
            guard !Task.isCancelled else { return }
            self?.diagnostics.record("session_timeout", fields: ["stage": self?.startupStage ?? "unknown"])
            self?.end()
        }
    }

    // MARK: - Teardown

    private func fail(_ error: Error) {
        guard !isTerminal else { return }
        var fields = sessionSummaryFields
        fields["failure_kind"] = error is VoiceConnectionTimeout ? "startup_timeout" : "operation"
        diagnostics.record("failed", fields: fields, error: error)
        reportError(error)
        phase = .failed(error.localizedDescription)
        teardown()
    }

    /// Persist the conversation transcript as its own conversation, once, on the
    /// first terminal transition. The save runs after the screen is dismissed, so
    /// a failure can't be shown inline — it is reported so the only copy of the
    /// transcript isn't lost without a trace.
    private func persistTranscriptIfNeeded() {
        guard !didPersist, !transcript.isEmpty, let transcriptStore else { return }
        didPersist = true
        let turns = transcript.entries
        let diagnostics = diagnostics
        let reportError = reportError
        let profileID = resolvedProfileID
        Task {
            do {
                _ = try await transcriptStore.saveVoiceSession(
                    turns: turns,
                    conversationID: nil,
                    profileID: profileID
                )
            } catch {
                diagnostics.record("transcript_save_failed", error: error)
                reportError(error)
            }
        }
    }

    private func teardown() {
        connectionTimeoutTask?.cancel()
        connectionTimeoutTask = nil
        persistTranscriptIfNeeded()
        isAssistantSpeaking = false
        inputLevel = 0
        lastInputLevelAt = nil
        for task in toolTasks.values {
            task.cancel()
        }
        toolTasks.removeAll()
        toolExecutionTail?.cancel()
        toolExecutionTail = nil
        timeoutTask?.cancel()
        timeoutTask = nil
        audio.onCapturedAudio = nil
        audio.onInputLevel = nil
        audio.onEngineFailure = nil
        audio.onDiagnostic = nil
        toolResultsSentAt = nil
        audioOut?.finish()
        audioOut = nil
        audioPumpTask?.cancel()
        audioPumpTask = nil
        audio.stop()
        // close() cancels the socket, which signals end-of-session to the server;
        // a separate audioStreamEnd frame would race close() and never be sent.
        session?.close()
        session = nil
        eventTask?.cancel()
        eventTask = nil
    }
}

private struct VoiceConnectionTimeout: LocalizedError {
    var errorDescription: String? {
        "Voice connection timed out. Please try again."
    }
}
