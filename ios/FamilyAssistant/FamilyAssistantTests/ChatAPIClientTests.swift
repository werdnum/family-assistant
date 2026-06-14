import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class ChatAPIClientTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private let apiToken = "chat-api-token"

    override func setUp() {
        super.setUp()
        resetStoredAuth()
        KeychainHelper.save(key: "fa_api_token", string: apiToken)
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
        URLProtocol.registerClass(ChatMockBackendURLProtocol.self)
        ChatMockBackendURLProtocol.reset()
    }

    override func tearDown() {
        ChatMockBackendURLProtocol.reset()
        URLProtocol.unregisterClass(ChatMockBackendURLProtocol.self)
        resetStoredAuth()
        super.tearDown()
    }

    func testListConversationsUsesWebInterfaceFilterAndAuthorization() async throws {
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/conversations")
            let queryItems = Self.queryItems(from: request)
            XCTAssertEqual(queryItems["interface_type"], "web")
            XCTAssertEqual(queryItems["limit"], "100")
            XCTAssertEqual(queryItems["offset"], "0")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            return .json(
                """
                {
                  "conversations": [
                    {
                      "conversation_id": "web_conv_1",
                      "last_message": "Hello",
                      "last_timestamp": "2026-06-08T12:00:00Z",
                      "message_count": 2
                    }
                  ],
                  "count": 1
                }
                """
            )
        }

        let conversations = try await makeClient().listConversations()

        XCTAssertEqual(conversations.map(\.conversationID), ["web_conv_1"])
        XCTAssertEqual(conversations.first?.messageCount, 2)
    }

    func testListConversationsPaginatesUntilAllHistoryIsLoaded() async throws {
        var offsets: [String] = []
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/conversations")
            let queryItems = Self.queryItems(from: request)
            offsets.append(queryItems["offset"] ?? "")
            XCTAssertEqual(queryItems["interface_type"], "web")
            XCTAssertEqual(queryItems["limit"], "100")

            if queryItems["offset"] == "0" {
                return .json(
                    """
                    {
                      "conversations": [
                        {
                          "conversation_id": "web_conv_1",
                          "last_message": "First",
                          "last_timestamp": "2026-06-08T12:00:00Z",
                          "message_count": 2
                        }
                      ],
                      "count": 2
                    }
                    """
                )
            }

            XCTAssertEqual(queryItems["offset"], "1")
            return .json(
                """
                {
                  "conversations": [
                    {
                      "conversation_id": "web_conv_2",
                      "last_message": "Second",
                      "last_timestamp": "2026-06-08T12:01:00Z",
                      "message_count": 4
                    }
                  ],
                  "count": 2
                }
                """
            )
        }

        let conversations = try await makeClient().listConversations()

        XCTAssertEqual(offsets, ["0", "1"])
        XCTAssertEqual(conversations.map(\.conversationID), ["web_conv_1", "web_conv_2"])
    }

    func testProfilesDecodeDirectChatProfiles() async throws {
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/api/v1/profiles")
            return .json(
                """
                {
                  "profiles": [
                    {
                      "id": "default_assistant",
                      "description": "Default",
                      "llm_model": "gpt-test",
                      "available_tools": ["notes"],
                      "enabled_mcp_servers": [],
                      "delegation_only": false
                    }
                  ],
                  "default_profile_id": "default_assistant"
                }
                """
            )
        }

        let response = try await makeClient().listProfiles()

        XCTAssertEqual(response.defaultProfileID, "default_assistant")
        XCTAssertEqual(response.profiles.first?.availableTools, ["notes"])
    }

    func testStreamMessageSendsWebPayloadAndDecodesSSE() async throws {
        var sawStartRequest = false
        var sawStreamRequest = false
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/turns" {
                XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertNotNil(payload["turn_id"] as? String)
                XCTAssertEqual(payload["prompt"] as? String, "Hi")
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_stream")
                XCTAssertEqual(payload["profile_id"] as? String, "default_assistant")
                XCTAssertEqual(payload["interface_type"] as? String, "web")
                sawStartRequest = true
                return .json(
                    #"{"turn_id":"turn-1","conversation_id":"web_conv_stream","first_seq":0}"#
                )
            }
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(path, "/api/v1/chat/conversations/web_conv_stream/stream")
            sawStreamRequest = true
            return .text(
                """
                event: turn_started
                data: {"turn_id":"turn-1","seq":0}

                event: text
                data: {"content":"Hello"}

                event: turn_ended
                data: {"turn_id":"turn-1","status":"complete"}

                """
            )
        }

        let turnStream = try await makeClient().streamMessage(
            turnID: "turn-1",
            prompt: "Hi",
            conversationID: "web_conv_stream",
            profileID: "default_assistant",
            attachments: []
        )

        XCTAssertFalse(turnStream.alreadyComplete)
        let stream = try XCTUnwrap(turnStream.events)
        var events: [ChatStreamEvent] = []
        for try await event in stream {
            events.append(event)
        }

        XCTAssertEqual(events.map(\.type), [.turnStarted, .text, .turnEnded])
        XCTAssertEqual(events.first { $0.type == .text }?.text, "Hello")
        XCTAssertTrue(sawStartRequest)
        XCTAssertTrue(sawStreamRequest)
    }

    func testStreamMessageAlreadyCompleteSkipsStream() async throws {
        var sawStreamRequest = false
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/turns" {
                return .json(
                    #"{"turn_id":"turn-dup","conversation_id":"web_conv_dup","first_seq":0,"already_complete":true}"#
                )
            }
            sawStreamRequest = true
            return .text("event: turn_ended\ndata: {\"turn_id\":\"turn-dup\"}\n\n")
        }

        let turnStream = try await makeClient().streamMessage(
            turnID: "turn-dup",
            prompt: "Hi again",
            conversationID: "web_conv_dup",
            profileID: "default_assistant",
            attachments: []
        )

        XCTAssertTrue(turnStream.alreadyComplete)
        XCTAssertNil(turnStream.events)
        XCTAssertFalse(sawStreamRequest)
    }

    func testConnectEventsSendsAckSeqAndEventTypeFilter() async throws {
        var queryItems: [String: String] = [:]
        ChatMockBackendURLProtocol.respond { request in
            queryItems = Self.queryItems(from: request)
            return .text("event: heartbeat\ndata: {}\n\n")
        }

        let stream = try await makeClient().connectEvents(
            conversationID: "web_conv_follow",
            ackSeq: 12,
            eventTypes: ["message", "turn_ended"]
        )
        for try await _ in stream {}

        XCTAssertEqual(queryItems["follow"], "true")
        XCTAssertEqual(queryItems["from_seq"], "-1")
        XCTAssertEqual(queryItems["ack_seq"], "12")
        XCTAssertEqual(queryItems["event_types"], "message,turn_ended")
    }

    func testConnectEventsThrowsWhileConnectingOnHTTPError() async throws {
        var streamRequests = 0
        ChatMockBackendURLProtocol.respond { _ in
            streamRequests += 1
            return .json(#"{"detail":"boom"}"#, statusCode: 500)
        }

        // The error must surface from the connect call itself, not lazily once
        // the caller starts iterating. The live-updates reconnect loop relies on
        // this so a down/erroring endpoint keeps growing the backoff instead of
        // resetting it after merely constructing a stream object.
        do {
            _ = try await makeClient().connectEvents(conversationID: "web_conv_down")
            XCTFail("connectEvents should throw when the stream endpoint errors")
        } catch ChatAPIError.server(let statusCode, _) {
            XCTAssertEqual(statusCode, 500)
        }
        XCTAssertEqual(streamRequests, 1)
    }

    func testAcknowledgePostsConversationAndSeq() async throws {
        var payload: [String: Any]?
        var path: String?
        ChatMockBackendURLProtocol.respond { request in
            path = request.url?.path
            payload = try Self.jsonObject(from: request) as? [String: Any]
            return .json("{}")
        }

        try await makeClient().acknowledge(conversationID: "web_conv_ack", ackSeq: 5)

        XCTAssertEqual(path, "/api/v1/chat/ack")
        XCTAssertEqual(payload?["conversation_id"] as? String, "web_conv_ack")
        XCTAssertEqual(payload?["ack_seq"] as? Int, 5)
    }

    func testGetMessagesPageSendsAfterTimestamp() async throws {
        var queryItems: [String: String] = [:]
        ChatMockBackendURLProtocol.respond { request in
            queryItems = Self.queryItems(from: request)
            return .json(
                """
                {
                  "conversation_id":"web_conv_after",
                  "messages":[],
                  "count":0,
                  "total_messages":0,
                  "has_more_before":false,
                  "has_more_after":false
                }
                """
            )
        }

        let after = Date(timeIntervalSince1970: 1_700_000_000)
        _ = try await makeClient().getMessagesPage(conversationID: "web_conv_after", after: after, limit: 100)

        XCTAssertEqual(queryItems["limit"], "100")
        XCTAssertNotNil(queryItems["after"])
        XCTAssertTrue(queryItems["after"]?.hasPrefix("2023-11-14T") == true)
    }

    func testConfirmToolIncludesApprovingInterfaceIOS() async throws {
        var sawConfirmRequest = false
        ChatMockBackendURLProtocol.respond { request in
            guard request.url?.path == "/api/v1/chat/confirm_tool" else {
                return .json(#"{"confirmations":[]}"#)
            }
            sawConfirmRequest = true
            XCTAssertEqual(request.httpMethod, "POST")
            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["request_id"] as? String, "confirm-1")
            XCTAssertEqual(payload["approved"] as? Bool, true)
            XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_1")
            XCTAssertEqual(payload["approving_interface"] as? String, "ios")
            return .json(#"{"success":true,"message":"approved"}"#)
        }

        try await makeClient().confirmTool(
            requestID: "confirm-1",
            conversationID: "web_conv_1",
            approved: true
        )
        XCTAssertTrue(sawConfirmRequest)
    }

    func testUploadAndDeleteAttachmentUseAuthenticatedEndpoints() async throws {
        let fileURL = FileManager.default.temporaryDirectory.appendingPathComponent("chat-test.txt")
        try Data("hello".utf8).write(to: fileURL)

        var sawUpload = false
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/attachments/upload" {
                sawUpload = true
                XCTAssertEqual(request.httpMethod, "POST")
                XCTAssertTrue(request.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("multipart/form-data") == true)
                XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
                XCTAssertTrue(String(data: request.bodyData, encoding: .utf8)?.contains("hello") == true)
                return .json(
                    """
                    {
                      "attachment_id": "11111111-1111-1111-1111-111111111111",
                      "filename": "chat-test.txt",
                      "content_type": "text/plain",
                      "size": 5,
                      "url": "/api/attachments/11111111-1111-1111-1111-111111111111"
                    }
                    """
                )
            }
            XCTAssertTrue(sawUpload)
            XCTAssertEqual(request.httpMethod, "DELETE")
            XCTAssertEqual(request.url?.path, "/api/attachments/11111111-1111-1111-1111-111111111111")
            return .json(#"{"message":"deleted"}"#)
        }

        let upload = try await makeClient().uploadAttachment(fileURL: fileURL, mimeType: "text/plain")
        XCTAssertEqual(upload.url, "/api/attachments/11111111-1111-1111-1111-111111111111")
        try await makeClient().deleteAttachment(attachmentID: upload.attachmentID)
    }

    private func makeClient() -> ChatAPIClient {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return ChatAPIClient(authManager: authManager)
    }

    private func resetStoredAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
    }

    private static func jsonObject(from request: URLRequest) throws -> Any {
        try JSONSerialization.jsonObject(with: request.bodyData)
    }

    private static func queryItems(from request: URLRequest) -> [String: String] {
        let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        return Dictionary(uniqueKeysWithValues: items.compactMap { item in
            item.value.map { (item.name, $0) }
        })
    }
}

final class ChatMockBackendURLProtocol: URLProtocol {
    typealias Handler = (URLRequest) throws -> ChatMockResponse

    private static let lock = NSLock()
    private static var handler: Handler?

    static func respond(with handler: @escaping Handler) {
        lock.withLock {
            self.handler = handler
        }
    }

    static func reset() {
        lock.withLock {
            handler = nil
        }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "assistant.example.test"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = Self.lock.withLock({ Self.handler }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }

        do {
            let response = try handler(request)
            client?.urlProtocol(self, didReceive: response.urlResponse(for: request), cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: response.data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

struct ChatMockResponse {
    let statusCode: Int
    let data: Data
    let headers: [String: String]

    static func json(_ json: String, statusCode: Int = 200) -> ChatMockResponse {
        ChatMockResponse(statusCode: statusCode, data: Data(json.utf8), headers: ["Content-Type": "application/json"])
    }

    static func text(_ text: String, statusCode: Int = 200) -> ChatMockResponse {
        ChatMockResponse(statusCode: statusCode, data: Data(text.utf8), headers: ["Content-Type": "text/event-stream"])
    }

    func urlResponse(for request: URLRequest) -> HTTPURLResponse {
        HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!
    }
}

extension URLRequest {
    var bodyData: Data {
        if let httpBody {
            return httpBody
        }
        guard let stream = httpBodyStream else {
            return Data()
        }
        var data = Data()
        stream.open()
        defer { stream.close() }

        let bufferSize = 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }

        while stream.hasBytesAvailable {
            let bytesRead = stream.read(buffer, maxLength: bufferSize)
            if bytesRead <= 0 {
                break
            }
            data.append(buffer, count: bytesRead)
        }
        return data
    }
}
