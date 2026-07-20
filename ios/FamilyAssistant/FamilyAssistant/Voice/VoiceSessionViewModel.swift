import Foundation

/// Abstraction over ``GeminiLiveClient`` so the session view model can be tested
/// with a fake live session.
@MainActor
protocol VoiceLiveSession: AnyObject {
    var events: AsyncStream<GeminiLiveServerEvent> { get }
    var lastError: Error? { get }
    func connect(token: EphemeralToken) async throws
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
    func saveVoiceSession(turns: [VoiceTranscriptEntry], conversationID: String?) async throws -> String
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
    private let profileID: String?
    private let reportError: @MainActor (Error) -> Void
    private let sessionTimeoutOverride: Duration?

    private var session: VoiceLiveSession?
    private var eventTask: Task<Void, Never>?
    private var audioPumpTask: Task<Void, Never>?
    private var timeoutTask: Task<Void, Never>?
    private var toolTasks: [String: Task<Void, Never>] = [:]
    private var toolExecutionTail: Task<Void, Never>?
    private var audioOut: AsyncStream<Data>.Continuation?
    private var didStart = false
    private var didPersist = false

    var pendingToolCallIDs: Set<String> { Set(toolTasks.keys) }

    init(
        tokenProvider: VoiceTokenProviding,
        toolExecutor: VoiceToolExecuting,
        transcriptStore: VoiceTranscriptStoring? = nil,
        audio: VoiceAudioIO = VoiceAudioEngine(),
        permission: VoiceMicrophonePermission = SystemMicrophonePermission(),
        profileID: String? = nil,
        sessionFactory: @escaping @MainActor () -> VoiceLiveSession = { GeminiLiveClient() },
        sessionTimeoutOverride: Duration? = nil,
        reportError: @escaping @MainActor (Error) -> Void = { ErrorReporter.shared.report($0, component: "Voice") }
    ) {
        self.tokenProvider = tokenProvider
        self.toolRunner = VoiceToolRunner(executor: toolExecutor, profileID: profileID)
        self.transcriptStore = transcriptStore
        self.audio = audio
        self.permission = permission
        self.profileID = profileID
        self.sessionFactory = sessionFactory
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
        guard !didStart else { return }
        didStart = true

        phase = .requestingPermission
        let granted = await permission.requestAccess()
        guard !isTerminal else { return }
        guard granted else {
            phase = .permissionDenied
            return
        }

        phase = .connecting
        let token: EphemeralToken
        do {
            token = try await tokenProvider.fetchEphemeralToken(profileID: profileID)
        } catch {
            fail(error)
            return
        }
        guard !isTerminal else { return }

        let session = sessionFactory()
        self.session = session
        startEventLoop(session: session)

        do {
            try await session.connect(token: token)
        } catch {
            fail(error)
            return
        }
        guard !isTerminal else {
            session.close()
            return
        }

        // Start the audio engine now so assistant playback is ready immediately,
        // but do NOT forward microphone audio yet: the Live API expects clients to
        // wait for `setupComplete` before sending realtime input, so the capture
        // pump is started from the setupComplete handler.
        do {
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

        // Start the session-duration cap now so a handshake that never completes
        // still ends.
        startTimeout(token: token)
    }

    /// End the session at the user's request.
    func end() {
        guard !isTerminal else { return }
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
                // The Live API is ready; begin forwarding captured microphone audio.
                startAudioPump(session: session)
            }
        case .audio(let data):
            isAssistantSpeaking = true
            audio.enqueue(data)
        case .outputTranscription(let text):
            transcript.appendAssistant(text)
        case .inputTranscription(let text):
            transcript.appendUser(text)
        case .turnComplete, .generationComplete:
            isAssistantSpeaking = false
        case .interrupted:
            isAssistantSpeaking = false
            audio.flushPlayback()
        case .toolCall(let calls):
            handleToolCalls(calls, session: session)
        case .toolCallCancellation(let ids):
            cancelToolCalls(ids)
        case .goAway:
            // goAway warns that the server will close soon; it is not the close
            // itself. Let the in-flight turn finish — the subsequent socket close
            // drives teardown via handleDisconnect — rather than cutting it short.
            break
        }
    }

    private func handleToolCalls(_ calls: [GeminiFunctionCall], session: VoiceLiveSession) {
        guard !calls.isEmpty else { return }
        let keys = calls.map { $0.id ?? UUID().uuidString }
        let previousTask = toolExecutionTail
        let task = Task { [weak self] in
            guard let self else { return }
            defer {
                for key in keys {
                    self.toolTasks[key] = nil
                }
            }
            await previousTask?.value
            guard !Task.isCancelled else { return }
            let responses = await self.toolRunner.run(calls)
            guard !Task.isCancelled else { return }
            try? await session.sendToolResponses(responses)
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
            self?.end()
        }
    }

    // MARK: - Teardown

    private func fail(_ error: Error) {
        guard !isTerminal else { return }
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
        Task { [weak self] in
            do {
                _ = try await transcriptStore.saveVoiceSession(turns: turns, conversationID: nil)
            } catch {
                self?.reportError(error)
            }
        }
    }

    private func teardown() {
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
