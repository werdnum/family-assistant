import Foundation
import XCTest

@testable import FamilyAssistant

// MARK: - Fakes

@MainActor
private final class FakeVoiceLiveSession: VoiceLiveSession {
    let events: AsyncStream<GeminiLiveServerEvent>
    private let continuation: AsyncStream<GeminiLiveServerEvent>.Continuation
    var lastError: Error?
    var connectError: Error?
    private(set) var connected = false
    private(set) var closed = false
    private(set) var sentAudio: [Data] = []
    private(set) var sentToolResponses: [[GeminiFunctionResponse]] = []

    init() {
        (events, continuation) = AsyncStream.makeStream(of: GeminiLiveServerEvent.self)
    }

    func connect(token: EphemeralToken) async throws {
        if let connectError { throw connectError }
        connected = true
    }

    func sendAudio(_ pcm16: Data) async throws { sentAudio.append(pcm16) }
    func endAudioStream() async throws {}
    func sendToolResponses(_ responses: [GeminiFunctionResponse]) async throws {
        sentToolResponses.append(responses)
    }

    func close() {
        closed = true
        continuation.finish()
    }

    func emit(_ event: GeminiLiveServerEvent) { continuation.yield(event) }
    func finish(withError error: Error?) {
        lastError = error
        continuation.finish()
    }
}

private final class FakeAudioIO: VoiceAudioIO {
    var onCapturedAudio: (@Sendable (Data) -> Void)?
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

    func stop() { stopped = true }
    func enqueue(_ pcm24k: Data) { enqueued.append(pcm24k) }
    func flushPlayback() { flushCount += 1 }
    func setMuted(_ muted: Bool) { self.muted = muted }
}

private struct FakePermission: VoiceMicrophonePermission {
    let granted: Bool
    func requestAccess() async -> Bool { granted }
}

@MainActor
private final class FakeTokenProvider: VoiceTokenProviding {
    var error: Error?
    var maxSessionMinutes = 15

    func fetchEphemeralToken(profileID: String?) async throws -> EphemeralToken {
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
    func executeTool(name: String, arguments: JSONValue) async throws -> JSONValue {
        try await handler(name, arguments)
    }
}

@MainActor
private final class FakeTranscriptStore: VoiceTranscriptStoring {
    private(set) var saved: [[VoiceTranscriptEntry]] = []
    func saveVoiceSession(turns: [VoiceTranscriptEntry], conversationID: String?) async throws -> String {
        saved.append(turns)
        return "web_conv_test"
    }
}

private struct SampleError: LocalizedError {
    var errorDescription: String? { "boom" }
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
        timeout: Duration? = nil
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

    func testHappyPathConnectsThenActivatesOnSetupComplete() async throws {
        let model = makeModel()
        await model.start()
        XCTAssertTrue(session.connected)
        XCTAssertTrue(audio.started)
        XCTAssertEqual(model.phase, .connecting)

        session.emit(.setupComplete)
        try await waitUntil { model.phase == .active }
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
        toolExecutor.handler = { _, _ in .object(["ok": .bool(true)]) }
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
        let d1 = Data([0x01])
        let d2 = Data([0x02])
        audio.onCapturedAudio?(d1)
        audio.onCapturedAudio?(d2)
        try await waitUntil { self.session.sentAudio == [d1, d2] }
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

    func testGoAwayEndsSession() async throws {
        let model = makeModel()
        await model.start()
        session.emit(.goAway(timeLeft: "1s"))
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
}
