import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class EphemeralTokenTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private let apiToken = "voice-api-token"

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

    private static let sampleResponse = """
    {
      "token": "auth_tokens/xyz",
      "expires_at": "2026-06-20T12:30:00Z",
      "model": "gemini-3.1-flash-live-preview",
      "system_instruction": "You are a helpful voice assistant.",
      "tools": [
        {"functionDeclarations": [{"name": "get_weather", "description": "Weather"}]}
      ],
      "config": {
        "model": "gemini-3.1-flash-live-preview",
        "voice": {"name": "Charon"},
        "session": {"max_duration_minutes": 10},
        "transcription": {"input_enabled": true, "output_enabled": false}
      }
    }
    """

    func testDecodeEphemeralTokenResponse() throws {
        let token = try JSONDecoder().decode(
            EphemeralToken.self,
            from: Data(Self.sampleResponse.utf8)
        )
        XCTAssertEqual(token.token, "auth_tokens/xyz")
        XCTAssertEqual(token.model, "gemini-3.1-flash-live-preview")
        XCTAssertEqual(token.systemInstruction, "You are a helpful voice assistant.")
        XCTAssertEqual(token.tools.count, 1)
        XCTAssertEqual(token.config.voiceName, "Charon")
        XCTAssertEqual(token.config.maxSessionMinutes, 10)
        XCTAssertTrue(token.config.inputTranscriptionEnabled)
        XCTAssertFalse(token.config.outputTranscriptionEnabled)
        XCTAssertNotNil(token.expiresAt)
    }

    func testDecodeUsesConfigDefaultsWhenSectionsMissing() throws {
        let json = """
        {
          "token": "auth_tokens/abc",
          "expires_at": "2026-06-20T12:30:00Z",
          "model": "m",
          "system_instruction": "s",
          "tools": [],
          "config": {"model": "m"}
        }
        """
        let token = try JSONDecoder().decode(EphemeralToken.self, from: Data(json.utf8))
        XCTAssertEqual(token.config.voiceName, VoiceLiveConfig.defaultVoiceName)
        XCTAssertEqual(token.config.maxSessionMinutes, VoiceLiveConfig.defaultMaxSessionMinutes)
        XCTAssertTrue(token.config.inputTranscriptionEnabled)
        XCTAssertTrue(token.config.outputTranscriptionEnabled)
        XCTAssertTrue(token.tools.isEmpty)
    }

    func testFetchEphemeralTokenPostsProfileAndAuthorizes() async throws {
        var capturedBody: [String: Any] = [:]
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/gemini/ephemeral-token")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            capturedBody = (try? JSONSerialization.jsonObject(with: request.bodyData)) as? [String: Any] ?? [:]
            return .json(Self.sampleResponse)
        }

        let token = try await makeClient().fetchEphemeralToken(profileID: "complex_tasks")

        XCTAssertEqual(capturedBody["profile_id"] as? String, "complex_tasks")
        XCTAssertEqual(token.token, "auth_tokens/xyz")
        XCTAssertEqual(token.config.voiceName, "Charon")
    }

    func testFetchEphemeralTokenSurfacesServerError() async throws {
        ChatMockBackendURLProtocol.respond { _ in
            .json(#"{"detail":"Voice mode is not configured."}"#, statusCode: 500)
        }

        do {
            _ = try await makeClient().fetchEphemeralToken(profileID: nil)
            XCTFail("Expected a server error.")
        } catch let ChatAPIError.server(statusCode, detail) {
            XCTAssertEqual(statusCode, 500)
            XCTAssertEqual(detail, "Voice mode is not configured.")
        }
    }

    func testFetchEphemeralTokenLabelsBlankServerErrorAsVoiceFailure() async throws {
        ChatMockBackendURLProtocol.respond { _ in
            ChatMockResponse(statusCode: 500, data: Data(), headers: [:])
        }

        do {
            _ = try await makeClient().fetchEphemeralToken(profileID: nil)
            XCTFail("Expected a voice session error.")
        } catch let ChatAPIError.validation(message) {
            XCTAssertEqual(message, "Voice session request failed with status 500.")
        }
    }

    func testSaveVoiceSessionPostsTurnsAndReturnsConversationID() async throws {
        var capturedBody: [String: Any] = [:]
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/v1/chat/voice-sessions")
            capturedBody = (try? JSONSerialization.jsonObject(with: request.bodyData)) as? [String: Any] ?? [:]
            return .json(#"{"conversation_id":"web_conv_42","message_count":2}"#)
        }

        let entries = [
            VoiceTranscriptEntry(speaker: .user, text: "hi"),
            VoiceTranscriptEntry(speaker: .assistant, text: "hello"),
        ]
        let conversationID = try await makeClient().saveVoiceSession(turns: entries, conversationID: nil)

        XCTAssertEqual(conversationID, "web_conv_42")
        let turns = try XCTUnwrap(capturedBody["turns"] as? [[String: Any]])
        XCTAssertEqual(turns.map { $0["role"] as? String }, ["user", "assistant"])
        XCTAssertEqual(turns.map { $0["text"] as? String }, ["hi", "hello"])
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
}
