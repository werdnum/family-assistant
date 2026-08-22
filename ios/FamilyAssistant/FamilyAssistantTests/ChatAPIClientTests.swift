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

    func testGetSharedConversationUsesScopedAuthenticatedEndpoint() async throws {
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(
                request.url?.path,
                "/api/v1/shared-conversations/share-token_123/messages"
            )
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            return .json(
                """
                {
                  "conversation_id": "web_conv_shared",
                  "messages": [
                    {
                      "internal_id": "shared-1",
                      "role": "assistant",
                      "content": "Shared answer",
                      "timestamp": "2026-08-13T12:00:00Z",
                      "attachments": [
                        {
                          "attachment_id": "attachment-1",
                          "name": "photo.png",
                          "mime_type": "image/png",
                          "content_url": "/api/v1/shared-conversations/share-token_123/attachments/attachment-1"
                        }
                      ]
                    }
                  ],
                  "count": 1,
                  "total_messages": 1,
                  "has_more_before": false,
                  "has_more_after": false,
                  "active_turns": []
                }
                """
            )
        }

        let response = try await makeClient().getSharedConversationMessages(token: "share-token_123")

        XCTAssertEqual(response.conversationID, "web_conv_shared")
        XCTAssertEqual(response.messages.map(\.text), ["Shared answer"])
        XCTAssertEqual(
            response.messages.first?.attachments.first?.contentURL,
            "/api/v1/shared-conversations/share-token_123/attachments/attachment-1"
        )
    }

    func testGetSharedConversationPreservesUnavailableStatus() async throws {
        ChatMockBackendURLProtocol.respond { _ in
            .json(#"{"detail":"Shared conversation not found"}"#, statusCode: 404)
        }

        do {
            _ = try await makeClient().getSharedConversationMessages(token: "revoked-token")
            XCTFail("Expected a revoked share to throw")
        } catch let ChatAPIError.server(statusCode, _, _) {
            XCTAssertEqual(statusCode, 404)
        }
    }

    func testSharedConversationTransientAuthFailureOffersRetry() async {
        seedRefreshToken()
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/auth/refresh" {
                throw URLError(.notConnectedToInternet)
            }
            return .json(#"{"detail":"expired"}"#, statusCode: 401)
        }
        let authManager = makeAuthManager()
        let viewModel = SharedConversationViewModel(
            authManager: authManager,
            apiClient: ChatAPIClient(authManager: authManager)
        )

        await viewModel.load(token: "share-token")

        guard case .failed = viewModel.loadState else {
            return XCTFail("A transient refresh failure should remain retryable")
        }
    }

    func testConversationShareControlsLoadRotateAndRevoke() async throws {
        var methods: [String] = []
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(
                request.url?.path,
                "/api/v1/chat/conversations/web_conv_share/encoded/share"
            )
            XCTAssertTrue(request.url?.absoluteString.contains("web_conv_share%2Fencoded") == true)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            let method = request.httpMethod ?? ""
            methods.append(method)
            switch method {
            case "GET":
                return .json(#"{"active":false}"#)
            case "POST":
                return .json(#"{"share_url":"/shared/conversations/new-token"}"#)
            case "DELETE":
                return .json("", statusCode: 204)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let viewModel = ConversationShareViewModel(
            apiClient: makeClient(),
            errorReporter: ErrorReporter(spoolDirectory: nil)
        )

        await viewModel.loadStatus(conversationID: "web_conv_share/encoded")
        XCTAssertEqual(viewModel.status, .inactive)

        let url = await viewModel.createShare(conversationID: "web_conv_share/encoded")
        XCTAssertEqual(url?.absoluteString, "https://assistant.example.test/shared/conversations/new-token")
        XCTAssertEqual(viewModel.status, .active)

        await viewModel.revokeShare(conversationID: "web_conv_share/encoded")
        XCTAssertEqual(viewModel.status, .inactive)
        XCTAssertEqual(methods, ["GET", "POST", "DELETE"])
    }

    func testConversationShareStatusFailureCanRetry() async {
        var requestCount = 0
        ChatMockBackendURLProtocol.respond { _ in
            requestCount += 1
            if requestCount == 1 {
                return .json(#"{"detail":"temporarily unavailable"}"#, statusCode: 503)
            }
            return .json(#"{"active":true}"#)
        }
        let viewModel = ConversationShareViewModel(
            apiClient: makeClient(),
            errorReporter: ErrorReporter(spoolDirectory: nil)
        )

        await viewModel.loadStatus(conversationID: "web_conv_share")
        XCTAssertEqual(viewModel.status, .failed)

        await viewModel.loadStatus(conversationID: "web_conv_share")
        XCTAssertEqual(viewModel.status, .active)
    }

    func testConversationShareMutationsLatchAuthRequiredWithoutRetry() async throws {
        for method in ["POST", "DELETE"] {
            resetStoredAuth()
            KeychainHelper.save(key: "fa_api_token", string: apiToken)
            UserDefaults.standard.set(
                ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
                forKey: "fa_token_expiry"
            )
            let requestCount = AtomicCounter()
            ChatMockBackendURLProtocol.respond { request in
                XCTAssertEqual(request.httpMethod, method)
                _ = requestCount.increment()
                return .json(#"{"detail":"expired token"}"#, statusCode: 401)
            }
            let authManager = makeAuthManager()
            let viewModel = ConversationShareViewModel(
                apiClient: ChatAPIClient(authManager: authManager),
                errorReporter: ErrorReporter(spoolDirectory: nil)
            )

            if method == "POST" {
                let url = await viewModel.createShare(conversationID: "web_conv_share")
                XCTAssertNil(url)
            } else {
                await viewModel.revokeShare(conversationID: "web_conv_share")
            }
            XCTAssertEqual(requestCount.value, 1, "\(method) must not be replayed")
            XCTAssertTrue(authManager.authRequired)
            XCTAssertNil(KeychainHelper.readString(key: "fa_api_token"))
            XCTAssertNil(viewModel.actionErrorMessage, "the dedicated re-auth flow replaces a sharing alert")
        }
    }

    func testConversationShareMutationPreservesConcurrentlyRotatedCredentials() async throws {
        let requestCount = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            _ = requestCount.increment()
            KeychainHelper.save(key: "fa_api_token", string: "concurrently-rotated-token")
            return .json(#"{"detail":"expired token"}"#, statusCode: 401)
        }
        let authManager = makeAuthManager()

        do {
            _ = try await ChatAPIClient(authManager: authManager)
                .createConversationShare(conversationID: "web_conv_share")
            XCTFail("Expected the stale mutation to fail")
        } catch let ChatAPIError.server(statusCode, _, _) {
            XCTAssertEqual(statusCode, 401)
            XCTAssertEqual(requestCount.value, 1)
            XCTAssertFalse(authManager.authRequired)
            XCTAssertEqual(
                KeychainHelper.readString(key: "fa_api_token"),
                "concurrently-rotated-token"
            )
        }
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

    func testStartTurnSendsWebPayloadAndSubscribeDecodesSSE() async throws {
        var sawStartRequest = false
        var streamQueryItems: [String: String] = [:]
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/turns" {
                XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["turn_id"] as? String, "turn-1")
                XCTAssertEqual(payload["prompt"] as? String, "Hi")
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_stream")
                XCTAssertEqual(payload["profile_id"] as? String, "default_assistant")
                XCTAssertEqual(payload["interface_type"] as? String, "web")
                sawStartRequest = true
                return .json(
                    #"{"turn_id":"turn-1","conversation_id":"web_conv_stream","first_seq":3}"#
                )
            }
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(path, "/api/v1/chat/conversations/web_conv_stream/stream")
            streamQueryItems = Self.queryItems(from: request)
            return .text(
                """
                event: turn_started
                data: {"turn_id":"turn-1","seq":3}

                event: text
                data: {"content":"Hello"}

                event: turn_ended
                data: {"turn_id":"turn-1","status":"complete"}

                """
            )
        }

        let client = makeClient()
        let start = try await client.startTurn(
            turnID: "turn-1",
            prompt: "Hi",
            conversationID: "web_conv_stream",
            profileID: "default_assistant",
            attachments: []
        )

        XCTAssertFalse(start.alreadyComplete)
        XCTAssertFalse(start.incomplete)
        XCTAssertEqual(start.firstSeq, 3)
        XCTAssertEqual(start.conversationID, "web_conv_stream")
        XCTAssertTrue(sawStartRequest)

        let stream = try await client.subscribeToTurn(
            conversationID: start.conversationID,
            fromSeq: start.firstSeq,
            ackSeq: nil
        )
        var events: [ChatStreamEvent] = []
        for try await event in stream {
            events.append(event)
        }

        XCTAssertEqual(events.map(\.type), [.turnStarted, .text, .turnEnded])
        XCTAssertEqual(events.first { $0.type == .text }?.text, "Hello")
        // The send-and-watch subscribe uses follow=false and starts at first_seq.
        XCTAssertEqual(streamQueryItems["follow"], "false")
        XCTAssertEqual(streamQueryItems["from_seq"], "3")
    }

    func testSubscribeToTurnResumesFromSeqWithAck() async throws {
        var streamQueryItems: [String: String] = [:]
        ChatMockBackendURLProtocol.respond { request in
            streamQueryItems = Self.queryItems(from: request)
            return .text("event: turn_ended\ndata: {\"turn_id\":\"turn-resume\",\"seq\":9}\n\n")
        }

        let stream = try await makeClient().subscribeToTurn(
            conversationID: "web_conv_resume",
            fromSeq: 7,
            ackSeq: 6
        )
        for try await _ in stream {}

        // A resume after a drop re-subscribes from the last applied seq and acks
        // everything already seen so the server suppresses the disconnect push.
        XCTAssertEqual(streamQueryItems["follow"], "false")
        XCTAssertEqual(streamQueryItems["from_seq"], "7")
        XCTAssertEqual(streamQueryItems["ack_seq"], "6")
    }

    func testStartTurnAlreadyCompleteReportsFlag() async throws {
        var sawStreamRequest = false
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/turns" {
                return .json(
                    #"{"turn_id":"turn-dup","conversation_id":"web_conv_dup","first_seq":0,"already_complete":true,"incomplete":true}"#
                )
            }
            sawStreamRequest = true
            return .text("event: turn_ended\ndata: {\"turn_id\":\"turn-dup\"}\n\n")
        }

        let start = try await makeClient().startTurn(
            turnID: "turn-dup",
            prompt: "Hi again",
            conversationID: "web_conv_dup",
            profileID: "default_assistant",
            attachments: []
        )

        XCTAssertTrue(start.alreadyComplete)
        XCTAssertTrue(start.incomplete)
        // The caller reloads history instead of subscribing, so no stream opens.
        XCTAssertFalse(sawStreamRequest)
    }

    func testStartTurnConflictCarriesRunningTurnID() async throws {
        ChatMockBackendURLProtocol.respond { _ in
            .json(
                """
                {
                  "detail": {
                    "message": "This conversation already has a running turn.",
                    "active_turn_id": "turn-already-running"
                  }
                }
                """,
                statusCode: 409
            )
        }

        do {
            _ = try await makeClient().startTurn(
                turnID: "turn-rival",
                prompt: "Change direction",
                conversationID: "web_conv_conflict",
                profileID: "default_assistant",
                attachments: []
            )
            XCTFail("startTurn should expose the running turn from a 409 response")
        } catch ChatAPIError.turnAlreadyRunning(let activeTurnID) {
            XCTAssertEqual(activeTurnID, "turn-already-running")
        }
    }

    func testCancelTurnPostsConversationID() async throws {
        var payload: [String: Any]?
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertTrue(request.url?.absoluteString.contains("/api/v1/chat/turns/turn%20one/cancel") == true)
            payload = try Self.jsonObject(from: request) as? [String: Any]
            return .json(
                #"{"turn_id":"turn one","conversation_id":"web_conv_cancel","status":"cancelling","already_complete":false}"#
            )
        }

        let result = try await makeClient().cancelTurn(
            turnID: "turn one",
            conversationID: "web_conv_cancel"
        )

        XCTAssertEqual(payload?["conversation_id"] as? String, "web_conv_cancel")
        XCTAssertEqual(result.status, "cancelling")
        XCTAssertFalse(result.alreadyComplete)
    }

    func testSteerTurnPostsPrompt() async throws {
        var payload: [String: Any]?
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/turns/turn-steer/steer")
            payload = try Self.jsonObject(from: request) as? [String: Any]
            return .json(
                #"{"turn_id":"turn-steer","conversation_id":"web_conv_steer","accepted":true}"#
            )
        }

        let result = try await makeClient().steerTurn(
            turnID: "turn-steer",
            conversationID: "web_conv_steer",
            prompt: "focus on tomorrow"
        )

        XCTAssertEqual(payload?["conversation_id"] as? String, "web_conv_steer")
        XCTAssertEqual(payload?["prompt"] as? String, "focus on tomorrow")
        XCTAssertTrue(result.accepted)
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

    func testConnectEventsDecodesUserInputFrames() async throws {
        ChatMockBackendURLProtocol.respond { _ in
            .text(
                """
                event: text
                data: {"turn_id":"turn-live-steer","content":"Working","seq":1}

                event: user_input
                data: {"turn_id":"turn-live-steer","content":"focus on tomorrow","seq":2}

                """
            )
        }

        let stream = try await makeClient().connectEvents(conversationID: "web_conv_follow")
        var events: [ChatStreamEvent] = []
        for try await event in stream {
            events.append(event)
        }

        XCTAssertEqual(events.map(\.type), [.text, .userInput])
        XCTAssertEqual(events.map(\.text), ["Working", "focus on tomorrow"])
        XCTAssertEqual(events.map(\.turnID), ["turn-live-steer", "turn-live-steer"])
        XCTAssertEqual(events.map(\.seq), [1, 2])
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
        } catch ChatAPIError.server(let statusCode, _, _) {
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

    func testGetMessagesDecodesActiveTurns() async throws {
        ChatMockBackendURLProtocol.respond { _ in
            .json(
                """
                {
                  "conversation_id":"web_conv_turns",
                  "messages":[],
                  "count":0,
                  "total_messages":0,
                  "has_more_before":false,
                  "has_more_after":false,
                  "active_turns":[
                    {
                      "turn_id":"turn-1",
                      "started_at":"2026-06-08T12:00:00Z",
                      "latest_seq":7,
                      "status":"running"
                    }
                  ]
                }
                """
            )
        }

        let response = try await makeClient().getMessagesPage(conversationID: "web_conv_turns", after: nil, limit: 100)

        XCTAssertEqual(response.activeTurns.count, 1)
        let turn = try XCTUnwrap(response.activeTurns.first)
        XCTAssertEqual(turn.turnID, "turn-1")
        XCTAssertEqual(turn.latestSeq, 7)
        XCTAssertEqual(turn.status, "running")
        XCTAssertEqual(turn.startedAt, Date(timeIntervalSince1970: 1_780_920_000))
    }

    func testGetMessagesWithoutActiveTurnsDecodesEmpty() async throws {
        ChatMockBackendURLProtocol.respond { _ in
            .json(
                """
                {
                  "conversation_id":"web_conv_no_turns",
                  "messages":[],
                  "count":0,
                  "total_messages":0,
                  "has_more_before":false,
                  "has_more_after":false
                }
                """
            )
        }

        let response = try await makeClient().getMessagesPage(conversationID: "web_conv_no_turns", after: nil, limit: 100)

        XCTAssertTrue(response.activeTurns.isEmpty)
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

    func testResponse401OnIdempotentGETRefreshesAndRetriesOnce() async throws {
        seedRefreshToken()
        let getRequests = AtomicCounter()
        let refreshRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            switch request.url?.path {
            case "/api/auth/refresh":
                _ = refreshRequests.increment()
                return .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
            case "/api/v1/profiles":
                // First attempt is rejected; the retry (with the rotated token)
                // succeeds. The client must replay exactly once.
                if getRequests.increment() <= 1 {
                    return .json(#"{"detail":"expired token"}"#, statusCode: 401)
                }
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let response = try await makeClient().listProfiles()

        XCTAssertEqual(response.defaultProfileID, "default_assistant")
        XCTAssertEqual(getRequests.value, 2, "the GET is retried exactly once after refresh")
        XCTAssertEqual(refreshRequests.value, 1, "exactly one coalesced refresh")
    }

    func testConcurrentResponse401sShareOneForcedRefresh() async throws {
        seedRefreshToken()
        let getRequests = AtomicCounter()
        let refreshRequests = AtomicCounter()
        let authManager = makeAuthManager()
        let client = ChatAPIClient(authManager: authManager)
        ChatMockBackendURLProtocol.respond { request in
            switch request.url?.path {
            case "/api/auth/refresh":
                _ = refreshRequests.increment()
                return .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
            case "/api/v1/profiles":
                _ = getRequests.increment()
                if request.value(forHTTPHeaderField: "Authorization") == "Bearer \(self.apiToken)" {
                    return .json(#"{"detail":"expired token"}"#, statusCode: 401)
                }
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        async let first = client.listProfiles()
        async let second = client.listProfiles()
        let responses = try await (first, second)

        XCTAssertEqual(responses.0.defaultProfileID, "default_assistant")
        XCTAssertEqual(responses.1.defaultProfileID, "default_assistant")
        XCTAssertTrue(
            (3 ... 4).contains(getRequests.value),
            "each GET runs once and only requests that used the rejected token retry"
        )
        XCTAssertEqual(refreshRequests.value, 1, "concurrent 401 recovery is single-flight")
        XCTAssertFalse(authManager.authRequired)
    }

    func testResponse401TwiceLatchesAuthRequiredWithoutFurtherRetry() async throws {
        seedRefreshToken()
        let getRequests = AtomicCounter()
        let refreshRequests = AtomicCounter()
        let authManager = makeAuthManager()
        ChatMockBackendURLProtocol.respond { request in
            switch request.url?.path {
            case "/api/auth/refresh":
                _ = refreshRequests.increment()
                return .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
            case "/api/v1/profiles":
                _ = getRequests.increment()
                return .json(#"{"detail":"still expired"}"#, statusCode: 401)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        do {
            _ = try await ChatAPIClient(authManager: authManager).listProfiles()
            XCTFail("Expected a persistent 401 to throw")
        } catch AuthError.noCredentials {
            XCTAssertEqual(getRequests.value, 2, "the GET is retried exactly once, not repeatedly")
            XCTAssertEqual(refreshRequests.value, 1)
            XCTAssertTrue(authManager.authRequired, "a persistent 401 latches the terminal auth state")
            // Finding 6: the freshly-saved (but still-rejected) rotated token must
            // be cleared, not just have authRequired latched — otherwise other
            // request paths would keep replaying it.
            XCTAssertNil(
                KeychainHelper.readString(key: "fa_api_token"),
                "a rejected retry token must be cleared so other paths cannot replay it"
            )
        }
    }

    func testResponse401WithNoRefreshTokenLatchesAuthRequired() async throws {
        // No refresh token is seeded (only the base setUp's api token). A
        // response-time 401 forces a refresh that immediately hits
        // `performRefresh`'s no-credentials branch — the terminal auth state must
        // still latch so the user gets the re-auth affordance, not silence.
        let getRequests = AtomicCounter()
        let authManager = makeAuthManager()
        ChatMockBackendURLProtocol.respond { request in
            switch request.url?.path {
            case "/api/v1/profiles":
                _ = getRequests.increment()
                return .json(#"{"detail":"expired token"}"#, statusCode: 401)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        do {
            _ = try await ChatAPIClient(authManager: authManager).listProfiles()
            XCTFail("Expected a 401 with no refresh token to throw")
        } catch AuthError.noCredentials {
            XCTAssertEqual(getRequests.value, 1, "no retry is possible without a refresh token")
            XCTAssertTrue(
                authManager.authRequired,
                "a 401 with no stored refresh token must latch the terminal auth state"
            )
        }
    }

    func testResponse401OnNonIdempotentPOSTIsNotRetried() async throws {
        seedRefreshToken()
        let postRequests = AtomicCounter()
        let refreshRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/auth/refresh" {
                _ = refreshRequests.increment()
                return .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
            }
            if request.httpMethod == "POST", request.url?.path == "/api/v1/chat/turns" {
                _ = postRequests.increment()
                return .json(#"{"detail":"expired token"}"#, statusCode: 401)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        do {
            _ = try await makeClient().startTurn(
                turnID: "turn-401",
                prompt: "Hi",
                conversationID: "web_conv_401",
                profileID: "default_assistant",
                attachments: []
            )
            XCTFail("Expected the non-idempotent turn start to throw on 401")
        } catch ChatAPIError.server(let statusCode, _, _) {
            XCTAssertEqual(statusCode, 401)
            XCTAssertEqual(postRequests.value, 1, "a turn start must never be refreshed-and-replayed")
            XCTAssertEqual(refreshRequests.value, 0)
        }
    }

    func test429RetryAfterHeaderIsCarriedOnServerError() async throws {
        // Finding F5: a 429's `Retry-After` header must be parsed onto
        // `ChatAPIError.server` so the classifier can honor the server's backoff
        // instead of the hard-coded default.
        ChatMockBackendURLProtocol.respond { _ in
            .json(#"{"detail":"slow down"}"#, statusCode: 429, headers: ["Retry-After": "30"])
        }

        do {
            _ = try await makeClient().listConversations()
            XCTFail("Expected a 429 to throw")
        } catch let ChatAPIError.server(statusCode, _, retryAfter) {
            XCTAssertEqual(statusCode, 429)
            XCTAssertEqual(retryAfter, 30)
            XCTAssertEqual(
                ChatViewModel.retryAfterSeconds(from: ChatAPIError.server(
                    statusCode: statusCode, detail: nil, retryAfter: retryAfter
                )),
                30,
                "the parser reads the header value"
            )
        }
    }

    func test429WithoutHeaderFallsBackToNumericDetail() async throws {
        // Finding F5: with no `Retry-After` header, a numeric JSON detail (e.g. "45")
        // is still honored as the backoff.
        ChatMockBackendURLProtocol.respond { _ in
            .json(#"{"detail":"45"}"#, statusCode: 429)
        }

        do {
            _ = try await makeClient().listConversations()
            XCTFail("Expected a 429 to throw")
        } catch let ChatAPIError.server(statusCode, _, retryAfter) {
            XCTAssertEqual(statusCode, 429)
            XCTAssertNil(retryAfter, "no header ⇒ no parsed header value")
            XCTAssertEqual(
                ChatViewModel.retryAfterSeconds(from: ChatAPIError.server(
                    statusCode: statusCode, detail: "45", retryAfter: nil
                )),
                45,
                "the numeric-detail fallback still applies"
            )
        }
    }

    func testHTMLResponseSurfacesAuthWallInsteadOfDecodeError() async throws {
        ChatMockBackendURLProtocol.respond { _ in
            .json(
                "<html><head><title>Just a moment...</title></head><body></body></html>",
                headers: ["Content-Type": "text/html; charset=utf-8"]
            )
        }

        do {
            _ = try await makeClient().listProfiles()
            XCTFail("Expected an HTML login page to throw authWall")
        } catch let error as ChatAPIError {
            XCTAssertEqual(error, .authWall)
            XCTAssertEqual(error.errorDescription?.contains("authentication wall"), true)
        }
    }

    func testMarkupBodyWithJSONContentTypeSurfacesAuthWall() async throws {
        // A followed redirect can deliver the wall's markup under any content
        // type; a body starting with "<" is still detected.
        ChatMockBackendURLProtocol.respond { _ in
            .json(
                "\n  <html><body>Sign in</body></html>",
                headers: ["Content-Type": "application/json"]
            )
        }

        do {
            _ = try await makeClient().listConversations()
            XCTFail("Expected HTML markup in the body to throw authWall")
        } catch ChatAPIError.authWall {}
    }

    private func makeClient() -> ChatAPIClient {
        ChatAPIClient(authManager: makeAuthManager())
    }

    private func makeAuthManager() -> AuthManager {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return authManager
    }

    /// The base setUp seeds an api token + fresh expiry but no refresh token; the
    /// forced-refresh retry path needs one.
    private func seedRefreshToken() {
        KeychainHelper.save(key: "fa_refresh_token", string: "chat-refresh-token")
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

final class HangingStreamTests: XCTestCase {
    func testFinishNotifiesEveryActiveObserverWithoutAWorkerWait() {
        let stream = HangingStream()
        let cancelledObserver = expectation(description: "cancelled observer")
        cancelledObserver.isInverted = true
        let firstActiveObserver = expectation(description: "first active observer")
        let secondActiveObserver = expectation(description: "second active observer")

        let cancelledID = stream.observeCompletion { _ in
            cancelledObserver.fulfill()
        }
        _ = stream.observeCompletion { result in
            XCTAssertTrue(result.finished)
            XCTAssertEqual(String(decoding: result.data, as: UTF8.self), "tail")
            firstActiveObserver.fulfill()
        }
        _ = stream.observeCompletion { result in
            XCTAssertTrue(result.finished)
            XCTAssertEqual(String(decoding: result.data, as: UTF8.self), "tail")
            secondActiveObserver.fulfill()
        }

        stream.cancelObservation(cancelledID)
        stream.finish(appending: "tail")

        wait(
            for: [cancelledObserver, firstActiveObserver, secondActiveObserver],
            timeout: 0.1
        )
    }
}

/// Lock-guarded counter for tallying requests off the URLProtocol loading thread.
private final class AtomicCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    @discardableResult
    func increment() -> Int {
        lock.withLock {
            count += 1
            return count
        }
    }

    var value: Int {
        lock.withLock { count }
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

    private struct HangingStreamObservation {
        let controller: HangingStream
        let id: UUID
    }

    private let stopLock = NSLock()
    private var stopped = false
    private var activeHangingStream: HangingStreamObservation?

    override func startLoading() {
        guard let handler = Self.lock.withLock({ Self.handler }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }

        do {
            let response = try handler(request)
            client?.urlProtocol(self, didReceive: response.urlResponse(for: request), cacheStoragePolicy: .notAllowed)
            if let controller = response.hangingStream {
                // Hold the request open after the initial chunk so a turn can be
                // observed mid-flight. Completion is callback-driven so held SSE
                // requests do not occupy worker threads while they are idle.
                if !response.data.isEmpty {
                    client?.urlProtocol(self, didLoad: response.data)
                }
                let observationID = controller.observeCompletion { [weak self] result in
                    guard let self else { return }
                    if self.stopLock.withLock({ self.stopped }) {
                        return
                    }
                    if result.finished {
                        if !result.data.isEmpty {
                            self.client?.urlProtocol(self, didLoad: result.data)
                        }
                        self.client?.urlProtocolDidFinishLoading(self)
                    } else {
                        self.client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
                    }
                }
                let wasAlreadyStopped = stopLock.withLock {
                    if stopped {
                        return true
                    }
                    activeHangingStream = HangingStreamObservation(
                        controller: controller,
                        id: observationID
                    )
                    return false
                }
                if wasAlreadyStopped {
                    controller.cancelObservation(observationID)
                }
                return
            }
            client?.urlProtocol(self, didLoad: response.data)
            if response.dropsConnectionAfterData {
                // Simulate a connection that streamed some bytes and then dropped
                // mid-turn (backgrounding, network change, proxy idle timeout).
                // URLSession.bytes yields the delivered bytes, then throws.
                client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
            } else {
                client?.urlProtocolDidFinishLoading(self)
            }
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {
        let observation = stopLock.withLock {
            stopped = true
            let observation = activeHangingStream
            activeHangingStream = nil
            return observation
        }
        if let observation {
            observation.controller.cancelObservation(observation.id)
        }
    }
}

/// Test handle for a stream the mock holds open until the test finishes it (or
/// the request is cancelled), so a turn can be driven and observed mid-flight.
final class HangingStream: @unchecked Sendable {
    typealias CompletionResult = (finished: Bool, data: Data)

    private let lock = NSLock()
    private var result: CompletionResult?
    private var observers: [UUID: (CompletionResult) -> Void] = [:]

    /// Finish the held stream, optionally appending a final SSE chunk.
    func finish(appending text: String = "") {
        resolve((finished: true, data: Data(text.utf8)))
    }

    fileprivate func observeCompletion(
        _ completion: @escaping (CompletionResult) -> Void
    ) -> UUID {
        let id = UUID()
        let completedResult = lock.withLock { () -> CompletionResult? in
            if let result {
                return result
            }
            observers[id] = completion
            return nil
        }
        if let completedResult {
            completion(completedResult)
        }
        return id
    }

    fileprivate func cancelObservation(_ id: UUID) {
        _ = lock.withLock {
            observers.removeValue(forKey: id)
        }
    }

    private func resolve(_ completedResult: CompletionResult) {
        let completions = lock.withLock { () -> [(CompletionResult) -> Void] in
            guard result == nil else {
                return []
            }
            result = completedResult
            let completions = Array(observers.values)
            observers.removeAll()
            return completions
        }
        for completion in completions {
            completion(completedResult)
        }
    }
}

struct ChatMockResponse {
    let statusCode: Int
    let data: Data
    let headers: [String: String]
    /// When true, the loader delivers `data` and then fails the request with a
    /// network error instead of finishing cleanly — modelling a stream that
    /// dropped mid-turn rather than one that closed normally.
    var dropsConnectionAfterData = false
    /// When set, the loader delivers `data` and then holds the request open until
    /// the controller is finished (or the request is cancelled) — letting a test
    /// observe and drive a turn while it is still in flight.
    var hangingStream: HangingStream?

    static func json(
        _ json: String,
        statusCode: Int = 200,
        headers extraHeaders: [String: String] = [:]
    ) -> ChatMockResponse {
        var headers = ["Content-Type": "application/json"]
        headers.merge(extraHeaders) { _, new in new }
        return ChatMockResponse(statusCode: statusCode, data: Data(json.utf8), headers: headers)
    }

    static func text(_ text: String, statusCode: Int = 200) -> ChatMockResponse {
        ChatMockResponse(statusCode: statusCode, data: Data(text.utf8), headers: ["Content-Type": "text/event-stream"])
    }

    /// An SSE response that delivers `text` and then drops the connection
    /// without a `turn_ended` frame, modelling a mid-turn disconnect.
    static func droppedStream(_ text: String) -> ChatMockResponse {
        ChatMockResponse(
            statusCode: 200,
            data: Data(text.utf8),
            headers: ["Content-Type": "text/event-stream"],
            dropsConnectionAfterData: true
        )
    }

    /// An SSE response that delivers `initial` and then stays open until
    /// `controller` is finished or the request is cancelled, so a turn can be
    /// held in flight while the test drives other interactions.
    static func hangingStream(
        _ initial: String,
        statusCode: Int = 200,
        controller: HangingStream
    ) -> ChatMockResponse {
        ChatMockResponse(
            statusCode: statusCode,
            data: Data(initial.utf8),
            headers: ["Content-Type": "text/event-stream"],
            hangingStream: controller
        )
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
