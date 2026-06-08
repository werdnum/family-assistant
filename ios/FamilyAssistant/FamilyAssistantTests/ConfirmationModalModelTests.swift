import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class ConfirmationModalModelTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private let apiToken = "modal-api-token"

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

    func testLoadFetchesConfirmationDetail() async throws {
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/confirmations/confirm_abc")
            return .json(Self.detailJSON(status: "pending"))
        }

        let model = makeModel(requestID: "confirm_abc")
        await model.load()

        let detail = try XCTUnwrap(model.detail)
        XCTAssertEqual(detail.toolName, "add_or_update_note")
        XCTAssertEqual(detail.status, .pending)
        let title = try XCTUnwrap(detail.args["title"])
        XCTAssertEqual(title, .string("Trip"))
        XCTAssertTrue(model.canDecide)
        XCTAssertEqual(model.promptText, "Create a note for this itinerary?")
        XCTAssertNil(model.statusMessage)
    }

    func testResolvedConfirmationIsReadOnly() async {
        ChatMockBackendURLProtocol.respond { _ in
            .json(Self.detailJSON(status: "approved"))
        }

        let model = makeModel(requestID: "confirm_done")
        await model.load()

        XCTAssertFalse(model.canDecide)
        XCTAssertEqual(model.statusMessage, "This request was already approved.")
    }

    func testExpiredConfirmationIsReadOnly() async {
        ChatMockBackendURLProtocol.respond { _ in
            .json(Self.detailJSON(status: "expired"))
        }

        let model = makeModel(requestID: "confirm_old")
        await model.load()

        XCTAssertFalse(model.canDecide)
        XCTAssertEqual(model.statusMessage, "This request has expired.")
    }

    func testLoadFailureFallsBackToNotificationBody() async {
        ChatMockBackendURLProtocol.respond { _ in
            .json(#"{"detail":"not found"}"#, statusCode: 404)
        }

        let model = makeModel(requestID: "confirm_missing", body: "Original body")
        await model.load()

        XCTAssertNil(model.detail)
        XCTAssertTrue(model.canDecide)
        XCTAssertEqual(model.promptText, "Original body")
    }

    func testSubmitApprovalPostsToConfirmEndpoint() async throws {
        var capturedPayload: [String: Any]?
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/v1/chat/confirm_tool" {
                capturedPayload = try Self.jsonObject(from: request) as? [String: Any]
                return .json(#"{"success":true}"#)
            }
            return .json(Self.detailJSON(status: "pending"))
        }

        let model = makeModel(requestID: "confirm_abc", conversationID: "web_conv_2")
        let shouldDismiss = await model.submit(approved: true)

        XCTAssertTrue(shouldDismiss)
        XCTAssertTrue(model.didResolve)
        let payload = try XCTUnwrap(capturedPayload)
        XCTAssertEqual(payload["request_id"] as? String, "confirm_abc")
        XCTAssertEqual(payload["approved"] as? Bool, true)
        XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_2")
        XCTAssertEqual(payload["approving_interface"] as? String, "ios")
    }

    func testSubmitRejectionPostsRejection() async throws {
        var capturedApproved: Bool?
        ChatMockBackendURLProtocol.respond { request in
            let payload = try Self.jsonObject(from: request) as? [String: Any]
            capturedApproved = payload?["approved"] as? Bool
            return .json(#"{"success":true}"#)
        }

        let model = makeModel(requestID: "confirm_abc")
        let shouldDismiss = await model.submit(approved: false)

        XCTAssertTrue(shouldDismiss)
        XCTAssertEqual(capturedApproved, false)
    }

    func testSubmitFailureSurfacesErrorAndDoesNotDismiss() async {
        ChatMockBackendURLProtocol.respond { _ in
            .json(#"{"detail":"already handled"}"#, statusCode: 409)
        }

        let model = makeModel(requestID: "confirm_abc")
        let shouldDismiss = await model.submit(approved: false)

        XCTAssertFalse(shouldDismiss)
        XCTAssertFalse(model.didResolve)
        XCTAssertNotNil(model.errorMessage)
    }

    private func makeModel(
        requestID: String,
        conversationID: String? = nil,
        body: String = "Notification body"
    ) -> ConfirmationModalModel {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return ConfirmationModalModel(
            request: PendingConfirmationModal(
                requestID: requestID,
                conversationID: conversationID,
                title: "Confirmation needed",
                body: body
            ),
            authManager: authManager
        )
    }

    private func resetStoredAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
    }

    private static func detailJSON(status: String) -> String {
        """
        {
          "request_id": "confirm_abc",
          "tool_name": "add_or_update_note",
          "tool_call_id": "tool-call-123",
          "confirmation_prompt": "Create a note for this itinerary?",
          "args": {"title": "Trip", "content": "Flight lands at 6pm"},
          "status": "\(status)",
          "created_at": "2026-06-08T12:00:00Z",
          "expires_at": "2026-06-08T12:30:00Z",
          "time_remaining_seconds": 1800.0
        }
        """
    }

    private static func jsonObject(from request: URLRequest) throws -> Any {
        try JSONSerialization.jsonObject(with: request.bodyData)
    }
}
