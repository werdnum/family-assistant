import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class SubconversationPeekTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private let apiToken = "peek-test-token"

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

    private func makeAuthManager() -> AuthManager {
        let auth = AuthManager()
        auth.serverURL = serverURL
        return auth
    }

    private func resetStoredAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
    }

    private func waitUntil(
        timeout: TimeInterval = 5.0,
        _ condition: @escaping @MainActor () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() > deadline {
                XCTFail("Timed out waiting for condition")
                return
            }
            try await Task.sleep(for: .milliseconds(20))
        }
    }

    func testPendingDelegationDecodesSubconversationID() throws {
        let json = """
        {
          "conversation_id": "conv-1",
          "delegations": [
            {
              "delegation_id": "deleg-1",
              "target_profile_id": "engineer",
              "status": "running",
              "request_preview": "Check logs",
              "created_at": "2026-11-20T09:00:00Z",
              "started_at": "2026-11-20T09:00:02Z",
              "completed_at": null,
              "cancel_requested": false,
              "children_total": 0,
              "children_finished": 0,
              "subconversation_id": "sub-abc"
            }
          ]
        }
        """
        let decoded = try JSONDecoder.chatDecoder.decode(
            ChatPendingDelegationsResponse.self,
            from: Data(json.utf8)
        )
        XCTAssertEqual(decoded.delegations.count, 1)
        XCTAssertEqual(decoded.delegations[0].subconversationID, "sub-abc")
    }

    func testPeekStreamsLiveTokensToolCallsAndReloadsOnCompletion() async throws {
        let streamController = HangingStream()
        var messagesFetches = 0
        var capturedFromSeq: String?

        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if path == "/api/v1/chat/conversations/conv-peek/subconversations/sub-1/messages" {
                messagesFetches += 1
                if messagesFetches == 1 {
                    return .json(
                        """
                        {
                          "conversation_id": "conv-peek",
                          "subconversation_id": "sub-1",
                          "delegation": {
                            "delegation_id": "deleg-1",
                            "target_profile_id": "engineer",
                            "status": "running",
                            "request_preview": "Diagnose schedule",
                            "created_at": "2026-11-20T09:00:00Z",
                            "started_at": "2026-11-20T09:00:02Z",
                            "completed_at": null,
                            "cancel_requested": false,
                            "children_total": 0,
                            "children_finished": 0,
                            "subconversation_id": "sub-1"
                          },
                          "delegation_error": null,
                          "request_text": "Diagnose schedule",
                          "child_delegations": [],
                          "messages": [
                            {
                              "internal_id": 10,
                              "role": "user",
                              "content": "Diagnose schedule",
                              "timestamp": "2026-11-20T09:00:02Z",
                              "turn_id": "sub-turn-1"
                            }
                          ],
                          "count": 1,
                          "total_messages": 1,
                          "has_more_before": false,
                          "has_more_after": false,
                          "active_turns": [
                            {
                              "turn_id": "sub-turn-1",
                              "started_at": "2026-11-20T09:00:02Z",
                              "first_seq": 0,
                              "latest_seq": 1,
                              "status": "running"
                            }
                          ]
                        }
                        """
                    )
                }
                return .json(
                    """
                    {
                      "conversation_id": "conv-peek",
                      "subconversation_id": "sub-1",
                      "delegation": {
                        "delegation_id": "deleg-1",
                        "target_profile_id": "engineer",
                        "status": "completed",
                        "request_preview": "Diagnose schedule",
                        "created_at": "2026-11-20T09:00:00Z",
                        "started_at": "2026-11-20T09:00:02Z",
                        "completed_at": "2026-11-20T09:00:20Z",
                        "cancel_requested": false,
                        "children_total": 0,
                        "children_finished": 0,
                        "subconversation_id": "sub-1"
                      },
                      "delegation_error": null,
                      "request_text": "Diagnose schedule",
                      "child_delegations": [],
                      "messages": [
                        {
                          "internal_id": 10,
                          "role": "user",
                          "content": "Diagnose schedule",
                          "timestamp": "2026-11-20T09:00:02Z",
                          "turn_id": "sub-turn-1"
                        },
                        {
                          "internal_id": 11,
                          "role": "assistant",
                          "content": "The cron job was paused.",
                          "timestamp": "2026-11-20T09:00:20Z",
                          "turn_id": "sub-turn-1",
                          "processing_profile_id": "engineer"
                        }
                      ],
                      "count": 2,
                      "total_messages": 2,
                      "has_more_before": false,
                      "has_more_after": false,
                      "active_turns": []
                    }
                    """
                )
            }

            if path == "/api/v1/chat/conversations/conv-peek/subconversations/sub-1/stream" {
                let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
                capturedFromSeq = items.first { $0.name == "from_seq" }?.value
                return .hangingStream(
                    """
                    event: heartbeat
                    data: {}

                    event: text
                    data: {"seq":1,"turn_id":"sub-turn-1","content":"Checking logs… "}

                    event: tool_call
                    data: {"seq":2,"turn_id":"sub-turn-1","tool_call":{"id":"tc-1","type":"function","function":{"name":"read_error_logs","arguments":"{}"}}}

                    event: tool_result
                    data: {"seq":3,"turn_id":"sub-turn-1","tool_call_id":"tc-1","result":"No errors","outcome":"succeeded"}

                    event: text
                    data: {"seq":4,"turn_id":"sub-turn-1","content":"All clear."}

                    """,
                    controller: streamController
                )
            }

            return .json("{}", statusCode: 404)
        }

        let auth = makeAuthManager()
        let client = ChatAPIClient(authManager: auth)
        let seed = ChatPendingDelegation(
            delegationID: "deleg-1",
            targetProfileID: "engineer",
            status: "running",
            requestPreview: "Diagnose schedule",
            createdAt: Date(),
            subconversationID: "sub-1"
        )
        let vm = SubconversationPeekViewModel(
            conversationID: "conv-peek",
            subconversationID: "sub-1",
            seedDelegation: seed,
            authManager: auth,
            apiClient: client
        )

        vm.start()
        defer {
            vm.stop()
            streamController.finish()
        }

        try await waitUntil {
            vm.messages.last?.text.contains("All clear.") == true
        }
        XCTAssertEqual(capturedFromSeq, "0")
        XCTAssertTrue(vm.isStreaming)
        let liveBubble = try XCTUnwrap(vm.messages.last)
        XCTAssertEqual(liveBubble.toolCalls.count, 1)
        XCTAssertEqual(liveBubble.toolCalls.first?.status, .succeeded)

        streamController.finish(
            appending: """
            event: turn_ended
            data: {"seq":5,"turn_id":"sub-turn-1","status":"complete"}

            """
        )

        try await waitUntil { vm.isTerminal && !vm.isStreaming }
        XCTAssertEqual(vm.statusLabel, "Completed")
        XCTAssertEqual(vm.messages.last?.text, "The cron job was paused.")
        XCTAssertGreaterThanOrEqual(messagesFetches, 2)
    }
}
