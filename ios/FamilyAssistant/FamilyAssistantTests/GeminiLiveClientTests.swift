import Foundation
import XCTest

@testable import FamilyAssistant

/// In-memory ``GeminiLiveSocket`` for driving the client without a network.
///
/// Everything runs on the main actor in tests (the client's receive loop inherits
/// `@MainActor`), so the simple unsynchronized storage here is race-free.
final class FakeGeminiLiveSocket: GeminiLiveSocket, @unchecked Sendable {
    private(set) var sentFrames: [String] = []
    private(set) var didClose = false

    private var buffered: [Result<Data, Error>] = []
    private var waiter: CheckedContinuation<Data, Error>?

    func send(_ text: String) async throws {
        sentFrames.append(text)
    }

    func receive() async throws -> Data {
        if !buffered.isEmpty {
            return try buffered.removeFirst().get()
        }
        return try await withCheckedThrowingContinuation { continuation in
            waiter = continuation
        }
    }

    func close() {
        didClose = true
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
        private var task: Task<Void, Never>?

        func start(_ client: GeminiLiveClient) {
            task = Task { @MainActor in
                for await event in client.events {
                    self.events.append(event)
                }
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
        client.close()
    }

    func testSendToolResponsesEmitsToolResponseFrame() async throws {
        let socket = FakeGeminiLiveSocket()
        let client = GeminiLiveClient(host: "h", socketFactory: { _ in socket })
        try await client.connect(token: makeToken())
        try await client.sendToolResponses([
            GeminiFunctionResponse(id: "c1", name: "noop", response: .object([:]))
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
