import AppIntents
import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class AppIntentsTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private let apiToken = "app-intents-token"

    override func setUp() {
        super.setUp()
        resetStoredAuth()
        URLProtocol.registerClass(ChatMockBackendURLProtocol.self)
        ChatMockBackendURLProtocol.reset()
    }

    override func tearDown() {
        ChatMockBackendURLProtocol.reset()
        URLProtocol.unregisterClass(ChatMockBackendURLProtocol.self)
        // The navigation center is a process-wide singleton; clear any pending
        // path so it does not leak into the next test.
        _ = IntentNavigationCenter.shared.consumePendingChatPath()
        resetStoredAuth()
        super.tearDown()
    }

    // MARK: - sendMessage primitive

    func testSendMessagePostsWebPayloadAndDecodesReply() async throws {
        signIn()
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/send_message")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["prompt"] as? String, "What's for dinner?")
            XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_intent")
            XCTAssertEqual(payload["profile_id"] as? String, "default_assistant")
            XCTAssertEqual(payload["interface_type"] as? String, "web")
            return .json(#"{"reply":"Pasta.","conversation_id":"web_conv_intent","turn_id":"t1"}"#)
        }

        let manager = AuthManager()
        manager.serverURL = serverURL
        let result = try await ChatAPIClient(authManager: manager).sendMessage(
            prompt: "What's for dinner?",
            conversationID: "web_conv_intent",
            profileID: "default_assistant"
        )

        XCTAssertEqual(result, ChatSendResult(reply: "Pasta.", conversationID: "web_conv_intent"))
    }

    // MARK: - AskAssistantIntent

    func testAskAssistantIntentSendsPromptToSendMessage() async throws {
        signIn()
        var sawSend = false
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/v1/chat/send_message")
            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["prompt"] as? String, "Remind me to call mum")
            XCTAssertTrue((payload["conversation_id"] as? String)?.hasPrefix("web_conv_") == true)
            sawSend = true
            return .json(#"{"reply":"Done.","conversation_id":"web_conv_x","turn_id":"t1"}"#)
        }

        let intent = AskAssistantIntent()
        intent.prompt = "Remind me to call mum"
        _ = try await intent.perform()

        XCTAssertTrue(sawSend)
    }

    func testAskAssistantIntentRequiresSignIn() async {
        // No credentials seeded.
        let intent = AskAssistantIntent()
        intent.prompt = "Hello"

        await assertNeedsSignIn { try await intent.perform() }
    }

    func testExpiredSessionMapsToNeedsSignIn() async {
        // Token present but expired, and the refresh is rejected by the server.
        UserDefaults.standard.set(serverURL, forKey: "fa_server_url")
        KeychainHelper.save(key: "fa_api_token", string: apiToken)
        KeychainHelper.save(key: "fa_refresh_token", string: "stale-refresh")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(-60)),
            forKey: "fa_token_expiry"
        )
        ChatMockBackendURLProtocol.respond { _ in
            .json(#"{"detail":"expired"}"#, statusCode: 401)
        }

        let intent = AskAssistantIntent()
        intent.prompt = "Hi"

        await assertNeedsSignIn { try await intent.perform() }
    }

    // MARK: - QuickCaptureIntent

    func testQuickCaptureIntentWrapsContentWithFilingInstruction() async throws {
        signIn()
        var capturedPrompt: String?
        var capturedProfile: String?
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/v1/chat/send_message")
            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            capturedPrompt = payload["prompt"] as? String
            capturedProfile = payload["profile_id"] as? String
            return .json(#"{"reply":"Saved.","conversation_id":"web_conv_y","turn_id":"t1"}"#)
        }

        let intent = QuickCaptureIntent()
        intent.content = "https://example.com/article"
        _ = try await intent.perform()

        let prompt = try XCTUnwrap(capturedPrompt)
        XCTAssertTrue(prompt.contains("https://example.com/article"))
        XCTAssertTrue(prompt.localizedCaseInsensitiveContains("file"))
        // Untrusted shared content must run under the constrained profile.
        XCTAssertEqual(capturedProfile, "email_intake")
    }

    // MARK: - CreateNoteIntent

    func testCreateNoteIntentPostsNote() async throws {
        signIn()
        var sawSave = false
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/notes")
            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["title"] as? String, "Groceries")
            XCTAssertEqual(payload["content"] as? String, "Milk and eggs")
            XCTAssertEqual(payload["include_in_prompt"] as? Bool, true)
            sawSave = true
            return .json("{}")
        }

        let intent = CreateNoteIntent()
        intent.title = "Groceries"
        intent.content = "Milk and eggs"
        _ = try await intent.perform()

        XCTAssertTrue(sawSave)
    }

    // MARK: - OpenConversationIntent

    func testOpenConversationIntentQueuesPromptForChat() async throws {
        let intent = OpenConversationIntent()
        intent.prompt = "hello world"
        _ = try await intent.perform()

        XCTAssertEqual(IntentNavigationCenter.shared.consumePendingChatPath(), "/chat?q=hello%20world")
    }

    func testOpenConversationIntentWithoutPromptOpensChat() async throws {
        let intent = OpenConversationIntent()
        intent.prompt = nil
        _ = try await intent.perform()

        XCTAssertEqual(IntentNavigationCenter.shared.consumePendingChatPath(), "/chat")
    }

    // MARK: - Deep links & shortcuts

    func testChatDeepLinkEncodesPromptAndConversation() {
        let url = IntentSupport.chatDeepLink(prompt: "hi there", conversationID: "web_conv_z")
        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        XCTAssertEqual(components?.scheme, "familyassistant")
        XCTAssertEqual(components?.host, "chat")
        let items = Dictionary(uniqueKeysWithValues: (components?.queryItems ?? []).map { ($0.name, $0.value) })
        XCTAssertEqual(items["q"], "hi there")
        XCTAssertEqual(items["conversation_id"], "web_conv_z")
    }

    func testChatDeepLinkOmitsEmptyParameters() {
        let url = IntentSupport.chatDeepLink()
        XCTAssertEqual(url.absoluteString, "familyassistant://chat")
    }

    func testNewConversationIDUsesWebPrefix() {
        XCTAssertTrue(IntentSupport.newConversationID().hasPrefix("web_conv_"))
    }

    func testAppShortcutsAreRegistered() {
        XCTAssertEqual(FamilyAssistantAppShortcuts.appShortcuts.count, 4)
    }

    // MARK: - Helpers

    private func signIn() {
        UserDefaults.standard.set(serverURL, forKey: "fa_server_url")
        KeychainHelper.save(key: "fa_api_token", string: apiToken)
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
    }

    private func resetStoredAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
    }

    private func assertNeedsSignIn<T>(
        _ operation: () async throws -> T,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        do {
            _ = try await operation()
            XCTFail("Expected needsSignIn", file: file, line: line)
        } catch let error as AssistantIntentError {
            guard case .needsSignIn = error else {
                return XCTFail("Expected needsSignIn, got \(error)", file: file, line: line)
            }
        } catch {
            XCTFail("Expected AssistantIntentError, got \(error)", file: file, line: line)
        }
    }

    private static func jsonObject(from request: URLRequest) throws -> Any {
        try JSONSerialization.jsonObject(with: request.bodyData)
    }
}
