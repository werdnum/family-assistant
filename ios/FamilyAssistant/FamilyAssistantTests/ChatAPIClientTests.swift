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
            XCTAssertEqual(request.url?.query, "interface_type=web")
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
        var sawStreamRequest = false
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/v1/chat/events" {
                return .text(
                    """
                    event: heartbeat
                    data: {}

                    """
                )
            }
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/send_message_stream")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["prompt"] as? String, "Hi")
            XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_stream")
            XCTAssertEqual(payload["profile_id"] as? String, "default_assistant")
            XCTAssertEqual(payload["interface_type"] as? String, "web")
            sawStreamRequest = true
            return .text(
                """
                event: text
                data: {"content":"Hello"}

                event: end
                data: {}

                """
            )
        }

        let stream = try await makeClient().streamMessage(
            prompt: "Hi",
            conversationID: "web_conv_stream",
            profileID: "default_assistant",
            attachments: []
        )

        var events: [ChatStreamEvent] = []
        for try await event in stream {
            events.append(event)
        }

        XCTAssertEqual(events.map(\.type), [.text, .end])
        XCTAssertEqual(events.first?.text, "Hello")
        XCTAssertTrue(sawStreamRequest)
    }

    func testConfirmToolIncludesApprovingInterfaceIOS() async throws {
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/confirm_tool")
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
