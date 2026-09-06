@testable import FamilyAssistant
import Foundation
import XCTest

// MARK: - Fakes

@MainActor
private final class FakeVoiceLiveSession: VoiceLiveSession {
    let events: AsyncStream<GeminiLiveServerEvent>
    private let continuation: AsyncStream<GeminiLiveServerEvent>.Continuation
    var lastError: Error?
    var connectError: Error?
    var onConnect: (() -> Void)?
    private(set) var connected = false
    private(set) var closed = false
    private(set) var sentAudio: [Data] = []
    private(set) var sentToolResponses: [[GeminiFunctionResponse]] = []

    init() {
        (events, continuation) = AsyncStream.makeStream(of: GeminiLiveServerEvent.self)
    }

    func connect(token _: EphemeralToken) async throws {
        onConnect?()
        if let connectError { throw connectError }
        connected = true
    }

    func sendAudio(_ pcm16: Data) async throws {
        sentAudio.append(pcm16)
    }

    func endAudioStream() async throws {}
    func sendToolResponses(_ responses: [GeminiFunctionResponse]) async throws {
        sentToolResponses.append(responses)
    }

    func close() {
        closed = true
        continuation.finish()
    }

    func emit(_ event: GeminiLiveServerEvent) {
        continuation.yield(event)
    }

    func finish(withError error: Error?) {
        lastError = error
        continuation.finish()
    }
}

private final class FakeAudioIO: VoiceAudioIO {
    var onCapturedAudio: (@Sendable (Data) -> Void)?
    var onInputLevel: (@Sendable (Double) -> Void)?
    var onEngineFailure: ((Error) -> Void)?
    var startError: Error?
    private(set) var started = false
    private(set) var stopped = false
    private(set) var enqueued: [Data] = []
    private(set) var flushCount = 0
    private(set) var muted = false

    func start() async throws {
        if let startError { throw startError }
        started = true
    }

    func stop() {
        stopped = true
    }

    func enqueue(_ pcm24k: Data) {
        enqueued.append(pcm24k)
    }

    func flushPlayback() {
        flushCount += 1
    }

    func setMuted(_ muted: Bool) {
        self.muted = muted
    }
}

private struct FakePermission: VoiceMicrophonePermission {
    let granted: Bool
    func requestAccess() async -> Bool {
        granted
    }
}

/// A permission whose `requestAccess()` suspends until the test resumes it,
/// modeling the system prompt being on screen while the user dismisses.
private final class ControllablePermission: VoiceMicrophonePermission, @unchecked Sendable {
    private(set) var isWaiting = false
    private var continuation: CheckedContinuation<Bool, Never>?

    func requestAccess() async -> Bool {
        await withCheckedContinuation { continuation in
            self.continuation = continuation
            self.isWaiting = true
        }
    }

    func resume(granted: Bool) {
        isWaiting = false
        continuation?.resume(returning: granted)
        continuation = nil
    }
}

@MainActor
private final class FakeTokenProvider: VoiceTokenProviding {
    var error: Error?
    var beforeFetch: (() async -> Void)?
    var maxSessionMinutes = 15

    func fetchEphemeralToken(profileID _: String?) async throws -> EphemeralToken {
        await beforeFetch?()
        if let error { throw error }
        return EphemeralToken(
            token: "auth_tokens/test",
            expiresAt: nil,
            model: "gemini-test",
            systemInstruction: "sys",
            tools: [],
            config: VoiceLiveConfig(
                voiceName: "Puck",
                maxSessionMinutes: maxSessionMinutes,
                inputTranscriptionEnabled: true,
                outputTranscriptionEnabled: true
            )
        )
    }
}

@MainActor
private final class FakeToolExecutor: VoiceToolExecuting {
    var handler: (String, JSONValue) async throws -> JSONValue = { _, _ in .null }
    func executeTool(
        name: String,
        arguments: JSONValue,
        profileID: String?,
        taintMetadata: JSONValue
    ) async throws -> JSONValue {
        _ = profileID
        _ = taintMetadata
        return try await handler(name, arguments)
    }
}

@MainActor
private final class FakeTranscriptStore: VoiceTranscriptStoring {
    var error: Error?
    private(set) var saved: [[VoiceTranscriptEntry]] = []
    func saveVoiceSession(turns: [VoiceTranscriptEntry], conversationID _: String?) async throws -> String {
        if let error { throw error }
        saved.append(turns)
        return "web_conv_test"
    }
}

private struct SampleError: LocalizedError {
    var errorDescription: String? {
        "boom"
    }
}

// MARK: - Tests

@MainActor
final class VoiceSessionViewModelTests: XCTestCase {
    private var session: FakeVoiceLiveSession!
    private var audio: FakeAudioIO!
    private var tokenProvider: FakeTokenProvider!
    private var toolExecutor: FakeToolExecutor!
    private var store: FakeTranscriptStore!
    private var reportedErrors: [Error] = []

    override func setUp() {
        super.setUp()
        session = FakeVoiceLiveSession()
        audio = FakeAudioIO()
        tokenProvider = FakeTokenProvider()
        toolExecutor = FakeToolExecutor()
        store = FakeTranscriptStore()
        reportedErrors = []
    }

    private func makeModel(
        permissionGranted: Bool = true,
        timeout: Duration? = nil,
        connectionTimeout: Duration = .seconds(30),
        diagnostics: VoiceConnectionDiagnostics = VoiceConnectionDiagnostics(sink: { _, _, _ in })
    ) -> VoiceSessionViewModel {
        VoiceSessionViewModel(
            tokenProvider: tokenProvider,
            toolExecutor: toolExecutor,
            transcriptStore: store,
            audio: audio,
            permission: FakePermission(granted: permissionGranted),
            profileID: nil,
            sessionFactory: { [session] in session! },
            sessionTimeoutOverride: timeout,
            connectionTimeout: connectionTimeout,
            diagnostics: diagnostics,
            reportError: { [weak self] error in self?.reportedErrors.append(error) }
        )
    }

    private func waitUntil(
        timeout: TimeInterval = 2,
        _ condition: @MainActor () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() > deadline {
                XCTFail("Condition not met before timeout.")
                return
            }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
    }

    func testPermissionDeniedStopsBeforeConnecting() async {
        let model = makeModel(permissionGranted: false)
        await model.start()
        XCTAssertEqual(model.phase, .permissionDenied)
        XCTAssertFalse(session.connected)
        XCTAssertFalse(audio.started)
    }

    func testStartupDiagnosticsCarryOneAttemptThroughFailure() async throws {
        let recorder = VoiceDiagnosticRecorder()
        session.connectError = NSError(domain: NSPOSIXErrorDomain, code: 57)
        let model = makeModel(diagnostics: recorder.diagnostics)
        await model.start()
        XCTAssertEqual(recorder.records.map { $0.0 }, ["permission_start", "token_start", "token_received", "audio_start", "audio_ready", "failed"])
        XCTAssertEqual(Set(recorder.records.compactMap { $0.1["attempt_id"] }).count, 1)
        let failure = try XCTUnwrap(recorder.records.last)
        XCTAssertTrue(failure.2)
        XCTAssertEqual(failure.1["stage"], "setup")
        XCTAssertEqual(failure.1["error_code"], "57")
    }

    func testMissingSetupAcknowledgementTimesOutAndReportsStage() async throws {
        let recorder = VoiceDiagnosticRecorder()
        let model = makeModel(connectionTimeout: .milliseconds(20), diagnostics: recorder.diagnostics)
        await model.start()
        try await waitUntil { model.isTerminal }
        XCTAssertEqual(model.phase, .failed("Voice connection timed out. Please try again."))
        XCTAssertTrue(session.closed)
        XCTAssertTrue(audio.stopped)
        XCTAssertEqual(recorder.records.last?.1["stage"], "setup")
        XCTAssertEqual(reportedErrors.count, 1)
    }

    func testSetupCompleteCancelsConnectionDeadline() async throws {
        let model = makeModel()
        await model.start()
        let deadline = try XCTUnwrap(model.connectionTimeoutTask)
        session.emit(.setupComplete)
        try await waitUntil { model.phase == .active }
        XCTAssertTrue(deadline.isCancelled)
        XCTAssertNil(model.connectionTimeoutTask)
        model.end()
    }

    func testTokenWaitTimesOutWithoutStartingAudioAfterLateResponse() async throws {
        var continuation: CheckedContinuation<Void, Never>?
        tokenProvider.beforeFetch = {
            await withCheckedContinuation { continuation = $0 }
        }
        let recorder = VoiceDiagnosticRecorder()
        let model = makeModel(connectionTimeout: .milliseconds(20), diagnostics: recorder.diagnostics)
        let start = Task { await model.start() }
        try await waitUntil { model.isTerminal }
        XCTAssertEqual(recorder.records.last?.1["stage"], "token")
        XCTAssertEqual(recorder.records.last?.1["failure_kind"], "startup_timeout")
        continuation?.resume()
        await start.value
        XCTAssertFalse(audio.started)
        XCTAssertFalse(session.connected)
    }

    func testTokenFetchFailureReportsAndFails() async {
        tokenProvider.error = SampleError()
        let model = makeModel()
        await model.start()
        XCTAssertEqual(model.phase, .failed("boom"))
        XCTAssertEqual(reportedErrors.count, 1)
        XCTAssertFalse(audio.started)
    }

    func testConnectFailureFails() async {
        session.connectError = SampleError()
        let model = makeModel()
        await model.start()
        XCTAssertEqual(model.phase, .failed("boom"))
        XCTAssertTrue(session.connected == false)
    }

    func testAudioStartFailureReportsAndFails() async {
        audio.startError = VoiceAudioError.simulatorLiveInputDisabled
        let model = makeModel()

        await model.start()

        XCTAssertEqual(
            model.phase,
            .failed("Live microphone input is disabled in the iOS Simulator. Set FA_ALLOW_SIMULATOR_MIC=1 to try the simulator microphone.")
        )
        XCTAssertEqual(reportedErrors.count, 1)
        XCTAssertFalse(session.connected)
    }

    func testAudioIsRunningBeforeOpeningStreamingConnection() async {
        var wasRunningAtConnect = false
        session.onConnect = { wasRunningAtConnect = self.audio.started }
        let model = makeModel()

        await model.start()

        XCTAssertTrue(wasRunningAtConnect)
        model.end()
    }

    func testEndBeforeScheduledStartDoesNotOpenMicrophoneOrConnection() async {
        let model = makeModel()
        model.end()

        await model.start()

        XCTAssertEqual(model.phase, .finished)
        XCTAssertFalse(audio.started)
        XCTAssertFalse(session.connected)
    }

    func testHappyPathConnectsThenActivatesOnSetupComplete() async throws {
        let model = makeModel()
        await model.start()
        XCTAssertTrue(session.connected)
        XCTAssertTrue(audio.started)
        XCTAssertEqual(model.phase, .connecting)

        session.emit(.setupComplete)
        try await waitUntil { model.phase == .active }
    }

    func testMicrophoneForwardingIsDeferredUntilSetupComplete() async throws {
        let model = makeModel()
        await model.start()
        // The engine runs so assistant playback is ready, but captured microphone
        // audio is not forwarded until the Live API acknowledges setup.
        XCTAssertTrue(audio.started)
        XCTAssertNil(audio.onCapturedAudio)

        session.emit(.setupComplete)
        try await waitUntil { self.audio.onCapturedAudio != nil }
        XCTAssertEqual(model.phase, .active)
    }

    func testDismissDuringPermissionPromptDoesNotStartSession() async throws {
        let permission = ControllablePermission()
        let model = VoiceSessionViewModel(
            tokenProvider: tokenProvider,
            toolExecutor: toolExecutor,
            transcriptStore: store,
            audio: audio,
            permission: permission,
            sessionFactory: { [session] in session! },
            reportError: { [weak self] error in self?.reportedErrors.append(error) }
        )
        let startTask = Task { await model.start() }
        try await waitUntil { permission.isWaiting }

        // User closes the screen while the system prompt is up, then grants.
        model.end()
        permission.resume(granted: true)
        await startTask.value

        XCTAssertEqual(model.phase, .finished)
        XCTAssertFalse(session.connected)
        XCTAssertFalse(audio.started)
    }

    func testAudioEventEnqueuesAndTracksSpeaking() async throws {
        let model = makeModel()
        await model.start()
        let chunk = Data([0x01, 0x02])
        session.emit(.audio(chunk))
        try await waitUntil { self.audio.enqueued == [chunk] }
        XCTAssertTrue(model.isAssistantSpeaking)

        session.emit(.turnComplete)
        try await waitUntil { model.isAssistantSpeaking == false }
    }

    func testInterruptedFlushesPlayback() async throws {
        let model = makeModel()
        await model.start()
        session.emit(.audio(Data([0x01])))
        try await waitUntil { model.isAssistantSpeaking }
        session.emit(.interrupted)
        try await waitUntil { self.audio.flushCount == 1 }
        XCTAssertFalse(model.isAssistantSpeaking)
    }

    func testTranscriptionAccumulates() async throws {
        let model = makeModel()
        await model.start()
        session.emit(.inputTranscription("hi"))
        session.emit(.outputTranscription("hello"))
        try await waitUntil { model.transcript.entries.count == 2 }
        XCTAssertEqual(model.transcript.entries.map(\.speaker), [.user, .assistant])
        XCTAssertEqual(model.transcript.entries.map(\.text), ["hi", "hello"])
    }

    func testToolCallExecutesAndRespondsToSession() async throws {
        toolExecutor.handler = { _, _ in
            .object(["result": .object(["ok": .bool(true)])])
        }
        let model = makeModel()
        await model.start()
        session.emit(.toolCall([GeminiFunctionCall(id: "c1", name: "noop", args: .object([:]))]))
        try await waitUntil { self.session.sentToolResponses.isEmpty == false }
        XCTAssertEqual(
            session.sentToolResponses.first?.first?.response,
            .object(["result": .object(["ok": .bool(true)])])
        )
    }

    func testCapturedAudioIsForwardedInOrder() async throws {
        let model = makeModel()
        await model.start()
        session.emit(.setupComplete)
        try await waitUntil { self.audio.onCapturedAudio != nil }

        let d1 = Data([0x01])
        let d2 = Data([0x02])
        audio.onCapturedAudio?(d1)
        audio.onCapturedAudio?(d2)
        try await waitUntil { self.session.sentAudio == [d1, d2] }
    }

    func testInputLevelUpdatesVisibleState() async throws {
        let model = makeModel()
        await model.start()
        session.emit(.setupComplete)
        try await waitUntil { model.phase == .active }

        audio.onInputLevel?(0.42)

        try await waitUntil { model.inputLevel == 0.42 }
        XCTAssertTrue(model.hasRecentInputLevel)
    }

    func testEndTearsDownResources() async {
        let model = makeModel()
        await model.start()
        model.end()
        XCTAssertEqual(model.phase, .finished)
        XCTAssertTrue(audio.stopped)
        XCTAssertTrue(session.closed)
    }

    func testDisconnectWithErrorFails() async throws {
        let model = makeModel()
        await model.start()
        session.finish(withError: SampleError())
        try await waitUntil { model.phase == .failed("boom") }
        XCTAssertEqual(reportedErrors.count, 1)
    }

    func testCleanDisconnectFinishes() async throws {
        let model = makeModel()
        await model.start()
        session.finish(withError: nil)
        try await waitUntil { model.phase == .finished }
        XCTAssertTrue(reportedErrors.isEmpty)
    }

    func testGoAwayDoesNotEndUntilSocketCloses() async throws {
        let model = makeModel()
        await model.start()
        session.emit(.setupComplete)
        try await waitUntil { model.phase == .active }

        // goAway is a warning, not the close: the in-flight turn keeps going.
        session.emit(.goAway(timeLeft: "1s"))
        await Task.yield()
        XCTAssertEqual(model.phase, .active)
        XCTAssertFalse(session.closed)

        // The subsequent socket close drives a clean finish.
        session.finish(withError: nil)
        try await waitUntil { model.phase == .finished }
        XCTAssertTrue(session.closed)
    }

    func testSessionTimeoutEndsSession() async throws {
        let model = makeModel(timeout: .milliseconds(20))
        await model.start()
        try await waitUntil { model.phase == .finished }
    }

    func testMuteForwardsToAudio() async {
        let model = makeModel()
        await model.start()
        model.isMuted = true
        XCTAssertTrue(audio.muted)
    }

    func testEndPersistsTranscriptOnce() async throws {
        let model = makeModel()
        await model.start()
        session.emit(.inputTranscription("hi"))
        session.emit(.outputTranscription("hello"))
        try await waitUntil { model.transcript.entries.count == 2 }

        model.end()
        try await waitUntil { self.store.saved.isEmpty == false }
        XCTAssertEqual(store.saved.count, 1)
        XCTAssertEqual(store.saved.first?.map(\.text), ["hi", "hello"])
    }

    func testEmptyTranscriptIsNotPersisted() async {
        let model = makeModel()
        await model.start()
        model.end()
        XCTAssertTrue(store.saved.isEmpty)
    }

    func testFailedTranscriptSaveIsReported() async throws {
        store.error = SampleError()
        let model = makeModel()
        await model.start()
        session.emit(.inputTranscription("hi"))
        try await waitUntil { model.transcript.entries.isEmpty == false }

        model.end()
        try await waitUntil { self.reportedErrors.isEmpty == false }
    }

    func testEngineFailureFailsSessionAndReports() async throws {
        let model = makeModel()
        await model.start()
        session.emit(.setupComplete)
        try await waitUntil { model.phase == .active }

        audio.onEngineFailure?(VoiceAudioError.captureStalled)

        try await waitUntil { model.isTerminal }
        XCTAssertEqual(model.phase, .failed("The microphone stopped delivering audio."))
        XCTAssertEqual(reportedErrors.count, 1)
        XCTAssertTrue(audio.stopped)
        XCTAssertTrue(session.closed)
    }

    func testToolCallCancellationSuppressesResponse() async throws {
        var toolStarted = false
        var toolReturned = false
        var gate: CheckedContinuation<Void, Never>?
        toolExecutor.handler = { _, _ in
            toolStarted = true
            await withCheckedContinuation { gate = $0 }
            toolReturned = true
            return .object([:])
        }
        let model = makeModel()
        await model.start()

        session.emit(.toolCall([GeminiFunctionCall(id: "c1", name: "noop", args: .object([:]))]))
        try await waitUntil { toolStarted }

        // Cancel while the tool is in flight, then let it finish.
        session.emit(.toolCallCancellation(["c1"]))
        try await waitUntil { model.pendingToolCallIDs.contains("c1") == false }
        gate?.resume()
        try await waitUntil { toolReturned }
        await Task.yield()

        XCTAssertTrue(session.sentToolResponses.isEmpty)
    }
}

/// The capture-liveness watchdog's escalation policy (pure logic; the engine
/// itself needs audio hardware and is exercised on-device).
final class CaptureStallActionTests: XCTestCase {
    func testHealthyCaptureDoesNothing() {
        XCTAssertEqual(
            CaptureStallAction.decide(
                sinceLastCapture: .seconds(1),
                stallThreshold: .seconds(10),
                isInterrupted: false,
                didAlreadyRestartForThisStall: false
            ),
            .wait
        )
    }

    func testStallTriggersRestartFirst() {
        XCTAssertEqual(
            CaptureStallAction.decide(
                sinceLastCapture: .seconds(11),
                stallThreshold: .seconds(10),
                isInterrupted: false,
                didAlreadyRestartForThisStall: false
            ),
            .restart
        )
    }

    func testStallAfterRestartFails() {
        XCTAssertEqual(
            CaptureStallAction.decide(
                sinceLastCapture: .seconds(11),
                stallThreshold: .seconds(10),
                isInterrupted: false,
                didAlreadyRestartForThisStall: true
            ),
            .fail
        )
    }

    func testInterruptionSuppressesTheWatchdog() {
        XCTAssertEqual(
            CaptureStallAction.decide(
                sinceLastCapture: .seconds(60),
                stallThreshold: .seconds(10),
                isInterrupted: true,
                didAlreadyRestartForThisStall: true
            ),
            .wait
        )
    }
}

final class StallEscalationTests: XCTestCase {
    private let threshold: Duration = .seconds(10)

    /// A restart that never revives capture must escalate to a failure on the
    /// next stall, not restart forever. `buildGraphAndStart()` seeds a fresh
    /// `lastCaptureAt` on the rebuild, so the tick right after the restart sees a
    /// small `sinceLastCapture` — but with no new capture, the escalation flag
    /// must survive so the following stall fails.
    func testRestartThatNeverRevivesFailsOnSecondStall() {
        var escalation = StallEscalation(captureCount: 0)
        // Grace window right after start: below threshold, capture never arrived.
        XCTAssertEqual(step(&escalation, since: .seconds(5), count: 0), .wait)
        // First real stall → restart.
        XCTAssertEqual(step(&escalation, since: .seconds(11), count: 0), .restart)
        // Rebuild seeded a fresh timestamp but capture still never fired.
        XCTAssertEqual(step(&escalation, since: .seconds(5), count: 0), .wait)
        // The next stall must fail, not restart again.
        XCTAssertEqual(step(&escalation, since: .seconds(11), count: 0), .fail)
    }

    /// A genuine capture after a restart clears the escalation, so a later,
    /// independent stall is treated as a fresh first stall (restart, not fail).
    func testRealCaptureAfterRestartResetsEscalation() {
        var escalation = StallEscalation(captureCount: 0)
        XCTAssertEqual(step(&escalation, since: .seconds(11), count: 0), .restart)
        // Capture resumes: counter advances while healthy → escalation clears.
        XCTAssertEqual(step(&escalation, since: .seconds(1), count: 5), .wait)
        // A new, unrelated stall later gets its own restart attempt.
        XCTAssertEqual(step(&escalation, since: .seconds(11), count: 5), .restart)
    }

    /// The seeded rebuild timestamp alone must not reset escalation — only a real
    /// capture (advancing counter) may. This is the precise regression guard.
    func testSeededTimestampWithoutCaptureDoesNotResetEscalation() {
        var escalation = StallEscalation(captureCount: 3)
        XCTAssertEqual(step(&escalation, since: .seconds(11), count: 3), .restart)
        // Below threshold but counter unchanged: escalation must NOT clear.
        XCTAssertEqual(step(&escalation, since: .seconds(2), count: 3), .wait)
        XCTAssertEqual(step(&escalation, since: .seconds(11), count: 3), .fail)
    }

    func testHealthyCaptureNeverEscalates() {
        var escalation = StallEscalation(captureCount: 0)
        for tick in 1 ... 5 {
            XCTAssertEqual(step(&escalation, since: .seconds(1), count: UInt64(tick)), .wait)
        }
    }

    func testInterruptionSuppressesEscalation() {
        var escalation = StallEscalation(captureCount: 0)
        XCTAssertEqual(
            escalation.step(
                sinceLastCapture: .seconds(30),
                stallThreshold: threshold,
                isInterrupted: true,
                captureCount: 0
            ),
            .wait
        )
    }

    private func step(_ escalation: inout StallEscalation, since: Duration, count: UInt64) -> CaptureStallAction {
        escalation.step(
            sinceLastCapture: since,
            stallThreshold: threshold,
            isInterrupted: false,
            captureCount: count
        )
    }
}
