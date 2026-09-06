@testable import FamilyAssistant
import Foundation
import XCTest

final class VoiceDiagnosticRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [(String, [String: String], Bool)] = []
    var records: [(String, [String: String], Bool)] {
        lock.withLock { storage }
    }

    lazy var diagnostics = VoiceConnectionDiagnostics { [weak self] event, fields, failure in
        guard let self else { return }
        self.lock.withLock { self.storage.append((event, fields, failure)) }
    }
}

/// In-memory ``GeminiLiveSocket`` for driving the client without a network.
///
/// Everything runs on the main actor in tests (the client's receive loop inherits
/// `@MainActor`), so the simple unsynchronized storage here is race-free.
final class FakeGeminiLiveSocket: GeminiLiveSocket, @unchecked Sendable {
    private(set) var sentFrames: [String] = []
    private(set) var didClose = false

    private var buffered: [Result<Data?, Error>] = []
    private var waiter: CheckedContinuation<Data?, Error>?
    var suspendSend = false
    var onSendSuspended: (() -> Void)?
    private var sendWaiter: CheckedContinuation<Void, Error>?

    func send(_ text: String) async throws {
        sentFrames.append(text)
        if suspendSend {
            try await withCheckedThrowingContinuation {
                sendWaiter = $0
                onSendSuspended?()
            }
        }
    }

    func receive() async throws -> Data? {
        if !buffered.isEmpty {
            return try buffered.removeFirst().get()
        }
        return try await withCheckedThrowingContinuation { continuation in
            waiter = continuation
        }
    }

    func close() {
        didClose = true
        sendWaiter?.resume(throwing: CancellationError())
        sendWaiter = nil
        if let waiter {
            self.waiter = nil
            waiter.resume(throwing: CancellationError())
        }
    }

    /// Deliver one inbound frame to the client's receive loop.
    func push(_ json: String) {
        let data = Data(json.utf8)
        if let waiter {
            self.waiter = nil
            waiter.resume(returning: data)
        } else {
            buffered.append(.success(data))
        }
    }

    func finish() {
        if let waiter {
            self.waiter = nil
            waiter.resume(returning: nil)
        } else {
            buffered.append(.success(nil))
        }
    }

    /// Fail the pending receive, simulating a dropped connection.
    func fail(_ error: Error) {
        if let waiter {
            self.waiter = nil
            waiter.resume(throwing: error)
        } else {
            buffered.append(.failure(error))
        }
    }
}

@MainActor
final class GeminiLiveClientTests: XCTestCase {
    func testConnectionDiagnosticsExcludeTokenAndSetupContent() async throws {
        let recorder = VoiceDiagnosticRecorder()
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(diagnostics: recorder.diagnostics, socketFactory: { _ in socket })
        try await client.connect(token: makeToken(token: "auth_tokens/private-secret"))
        XCTAssertEqual(recorder.records.map { $0.0 }, ["socket_start", "setup_sent"])
        let fields = try XCTUnwrap(recorder.records.first?.1)
        XCTAssertEqual(fields["api_version"], "v1alpha")
        XCTAssertGreaterThan(Int(fields["setup_bytes"] ?? "0") ?? 0, 0)
        XCTAssertFalse(String(describing: recorder.records).contains("private-secret"))
        XCTAssertNil(fields["system_instruction"])
        client.close()
    }

    func testDiagnosticErrorsOnlyIncludeStructuredCodes() throws {
        let recorder = VoiceDiagnosticRecorder()
        let underlying = NSError(domain: NSPOSIXErrorDomain, code: 57,
                                 userInfo: [NSLocalizedDescriptionKey: "private transcript"])
        let error = try NSError(domain: NSURLErrorDomain, code: -1005, userInfo: [
            NSUnderlyingErrorKey: underlying,
            NSURLErrorFailingURLErrorKey: XCTUnwrap(URL(string: "https://example.com/?access_token=secret")),
            NSLocalizedDescriptionKey: "private setup",
        ])
        recorder.diagnostics.record("failed", error: error)
        let record = try XCTUnwrap(recorder.records.first)
        XCTAssertTrue(record.2)
        XCTAssertEqual(record.1["error_domain"], NSURLErrorDomain)
        XCTAssertEqual(record.1["error_code"], "-1005")
        XCTAssertEqual(record.1["underlying_1_code"], "57")
        XCTAssertFalse(String(describing: record).contains("private"))
        XCTAssertFalse(String(describing: record).contains("secret"))
    }

    func testCloseReasonIsClassifiedWithoutUploadingArbitraryText() {
        let fields = VoiceConnectionDiagnostics.closeFields(code: 1008, reason: Data(
            "Too many function declarations; maximum 128. private-tool auth_tokens/secret".utf8
        ))
        XCTAssertEqual(fields["close_code"], "1008")
        XCTAssertEqual(fields["close_reason_category"], "function_limit")
        XCTAssertFalse(String(describing: fields).contains("secret"))
        XCTAssertFalse(String(describing: fields).contains("private-tool"))
        XCTAssertEqual(VoiceConnectionDiagnostics.closeFields(code: 1008, reason: Data("private transcript".utf8))["close_reason_category"], "unclassified")
    }

    private func makeToken(token: String = "auth_tokens/abc") -> EphemeralToken {
        EphemeralToken(
            token: token,
            expiresAt: nil,
            model: "gemini-test",
            systemInstruction: "sys",
            tools: [],
            config: VoiceLiveConfig(
                voiceName: "Puck",
                maxSessionMinutes: 15,
                inputTranscriptionEnabled: true,
                outputTranscriptionEnabled: true
            )
        )
    }

    /// Collects events from the client's stream on a background task.
    private final class EventCollector {
        private(set) var events: [GeminiLiveServerEvent] = []
        private(set) var finished = false
        private var task: Task<Void, Never>?

        func start(_ client: GeminiLiveClient) {
            task = Task { @MainActor in
                for await event in client.events {
                    self.events.append(event)
                }
                self.finished = true
            }
        }

        func stop() {
            task?.cancel()
        }
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

    // MARK: - Endpoint construction

    func testEphemeralTokenUsesConstrainedEndpointAndAccessTokenParam() throws {
        let url = try GeminiLiveClient.endpointURL(token: "auth_tokens/abc", host: "example.test")
        XCTAssertEqual(url.scheme, "wss")
        XCTAssertEqual(url.host, "example.test")
        XCTAssertTrue(url.path.hasSuffix("GenerativeService.BidiGenerateContentConstrained"))
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(components.queryItems?.first?.name, "access_token")
        XCTAssertEqual(components.queryItems?.first?.value, "auth_tokens/abc")
    }

    func testRawKeyUsesUnconstrainedEndpointAndKeyParam() throws {
        let url = try GeminiLiveClient.endpointURL(token: "rawkey", host: "example.test")
        XCTAssertTrue(url.path.hasSuffix("GenerativeService.BidiGenerateContent"))
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(components.queryItems?.first?.name, "key")
        XCTAssertEqual(components.queryItems?.first?.value, "rawkey")
    }

    // MARK: - Lifecycle

    func testCloseCancelsSocketWhileSetupIsStillSending() async throws {
        let socket = FakeGeminiLiveSocket()
        socket.suspendSend = true
        let suspended = expectation(description: "Setup send is suspended")
        socket.onSendSuspended = { suspended.fulfill() }
        let client = GeminiLiveClient(socketFactory: { _ in socket })
        let connection = Task { try await client.connect(token: makeToken()) }
        await fulfillment(of: [suspended], timeout: 2)

        client.close()

        XCTAssertTrue(socket.didClose)
        do {
            try await connection.value
            XCTFail("Closing during setup should cancel the pending send")
        } catch is CancellationError {
            // Closing the owned socket unblocks its pending write.
        }
    }

    func testConnectSendsSetupFrame() async throws {
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in socket })
        try await client.connect(token: makeToken())
        XCTAssertEqual(socket.sentFrames.count, 1)
        XCTAssertTrue(socket.sentFrames[0].contains("\"setup\""))
        client.close()
    }

    func testReceiveLoopYieldsDecodedEvents() async throws {
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in socket })
        let collector = EventCollector()
        collector.start(client)

        try await client.connect(token: makeToken())
        socket.push(#"{"setupComplete":{}}"#)
        socket.push(#"{"serverContent":{"outputTranscription":{"text":"hi"},"turnComplete":true}}"#)

        try await waitUntil { collector.events.contains(.turnComplete) }
        XCTAssertEqual(collector.events, [.setupComplete, .outputTranscription("hi"), .turnComplete])

        client.close()
        collector.stop()
    }

    func testSendAudioEmitsRealtimeFrame() async throws {
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in socket })
        try await client.connect(token: makeToken())
        try await client.sendAudio(Data([0x01, 0x02]))
        XCTAssertTrue(socket.sentFrames.last?.contains("\"realtimeInput\"") == true)
        XCTAssertTrue(socket.sentFrames.last?.contains("\"audio\"") == true)
        XCTAssertFalse(socket.sentFrames.last?.contains("\"mediaChunks\"") == true)
        client.close()
    }

    func testSendToolResponsesEmitsToolResponseFrame() async throws {
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in socket })
        try await client.connect(token: makeToken())
        try await client.sendToolResponses([
            GeminiFunctionResponse(id: "c1", name: "noop", response: .object([:])),
        ])
        XCTAssertTrue(socket.sentFrames.last?.contains("\"toolResponse\"") == true)
        client.close()
    }

    func testSendAudioBeforeConnectThrows() async throws {
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in FakeGeminiLiveSocket() })
        do {
            try await client.sendAudio(Data([0x00]))
            XCTFail("Expected notConnected error.")
        } catch {
            XCTAssertEqual(error as? GeminiLiveError, .notConnected)
        }
    }

    func testCleanSocketEndFinishesStreamWithoutError() async throws {
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(socketFactory: { _ in socket })
        let collector = EventCollector()
        collector.start(client)
        socket.finish()

        try await client.connect(token: makeToken())
        try await waitUntil { collector.finished }

        XCTAssertNil(client.lastError)
        XCTAssertTrue(socket.didClose)
    }

    func testSocketFailureFinishesStreamAndRecordsError() async throws {
        struct DropError: Error {}
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in socket })
        let collector = EventCollector()
        collector.start(client)

        try await client.connect(token: makeToken())
        socket.fail(DropError())

        try await waitUntil { client.lastError != nil }
        XCTAssertTrue(client.lastError is DropError)
        collector.stop()
    }

    func testMalformedFrameFinishesStreamWithError() async throws {
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in socket })
        let collector = EventCollector()
        collector.start(client)

        try await client.connect(token: makeToken())
        socket.push("this is not valid json {")

        try await waitUntil { client.lastError != nil }
        collector.stop()
    }

    func testCloseIsIdempotent() async throws {
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in socket })
        try await client.connect(token: makeToken())
        client.close()
        client.close()
        XCTAssertTrue(socket.didClose)
        XCTAssertNil(client.lastError)
    }
}
