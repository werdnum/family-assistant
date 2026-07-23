import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class VoiceToolRunnerTests: XCTestCase {
    private final class FakeToolExecutor: VoiceToolExecuting {
        var handler: (String, JSONValue) async throws -> JSONValue = { _, _ in .null }
        private(set) var calls: [(name: String, arguments: JSONValue, profileID: String?, taintMetadata: JSONValue)] = []

        func executeTool(
            name: String,
            arguments: JSONValue,
            profileID: String?,
            taintMetadata: JSONValue
        ) async throws -> JSONValue {
            calls.append((name, arguments, profileID, taintMetadata))
            return try await handler(name, arguments)
        }
    }

    private func call(_ name: String, _ args: JSONValue = .object([:]), id: String? = "c1") -> GeminiFunctionCall {
        GeminiFunctionCall(id: id, name: name, args: args)
    }

    func testSuccessWrapsResultPreservingIDAndName() async throws {
        let executor = FakeToolExecutor()
        executor.handler = { _, _ in
            .object(["result": .object(["temp": .number(72)])])
        }
        let runner = VoiceToolRunner(executor: executor)

        let responses = await runner.run([call("get_weather", .object(["city": .string("NYC")]))])

        XCTAssertEqual(responses.count, 1)
        XCTAssertEqual(responses[0].id, "c1")
        XCTAssertEqual(responses[0].name, "get_weather")
        XCTAssertEqual(responses[0].response, .object(["result": .object(["temp": .number(72)])]))
        XCTAssertEqual(executor.calls.first?.arguments, .object(["city": .string("NYC")]))
    }

    func testServerErrorWithDetailRelaysDetail() async throws {
        let executor = FakeToolExecutor()
        executor.handler = { _, _ in throw ChatAPIError.server(statusCode: 400, detail: "bad city", retryAfter: nil) }
        let runner = VoiceToolRunner(executor: executor)

        let responses = await runner.run([call("get_weather")])
        XCTAssertEqual(responses[0].response, .object(["error": .string("bad city")]))
    }

    func testServerErrorWithoutDetailFallsBackToStatus() async throws {
        let executor = FakeToolExecutor()
        executor.handler = { _, _ in throw ChatAPIError.server(statusCode: 500, detail: nil, retryAfter: nil) }
        let runner = VoiceToolRunner(executor: executor)

        let responses = await runner.run([call("noop")])
        XCTAssertEqual(responses[0].response, .object(["error": .string("Tool execution failed with status 500.")]))
    }

    func testGenericErrorRelaysLocalizedDescription() async throws {
        struct SampleError: LocalizedError {
            var errorDescription: String? { "network down" }
        }
        let executor = FakeToolExecutor()
        executor.handler = { _, _ in throw SampleError() }
        let runner = VoiceToolRunner(executor: executor)

        let responses = await runner.run([call("noop")])
        XCTAssertEqual(responses[0].response, .object(["error": .string("network down")]))
    }

    func testRunsMultipleCallsInOrder() async throws {
        let executor = FakeToolExecutor()
        executor.handler = { name, _ in .object(["ran": .string(name)]) }
        let runner = VoiceToolRunner(executor: executor)

        let responses = await runner.run([call("a", id: "1"), call("b", id: "2")])
        XCTAssertEqual(responses.map(\.name), ["a", "b"])
        XCTAssertEqual(executor.calls.map(\.name), ["a", "b"])
    }

    func testThreadsProfileAndReturnedTaintAcrossCalls() async throws {
        let executor = FakeToolExecutor()
        let returnedTaint: JSONValue = .object([
            "version": .string("runtime_v1"),
            "max_tier": .string("unknown_external"),
            "sources": .array([]),
        ])
        executor.handler = { name, _ in
            .object([
                "result": .string(name),
                "taint_metadata": returnedTaint,
            ])
        }
        let runner = VoiceToolRunner(executor: executor, profileID: "research")

        _ = await runner.run([call("first"), call("second")])

        XCTAssertEqual(executor.calls.map(\.profileID), ["research", "research"])
        XCTAssertEqual(executor.calls[0].taintMetadata, VoiceToolRunner.initialTaintMetadata)
        XCTAssertEqual(executor.calls[1].taintMetadata, returnedTaint)
    }

    func testRetainsTaintReturnedWithRejectedCall() async throws {
        let executor = FakeToolExecutor()
        let returnedTaint: JSONValue = .object([
            "version": .string("runtime_v1"),
            "max_tier": .string("unknown_external"),
            "sources": .array([]),
        ])
        executor.handler = { name, _ in
            if name == "rejected" {
                return .object([
                    "detail": .string("policy denied"),
                    "taint_metadata": returnedTaint,
                ])
            }
            return .object(["result": .string("ok")])
        }
        let runner = VoiceToolRunner(executor: executor)

        let responses = await runner.run([call("rejected"), call("next")])

        XCTAssertEqual(responses[0].response, .object(["error": .string("policy denied")]))
        XCTAssertEqual(executor.calls[1].taintMetadata, returnedTaint)
    }
}

@MainActor
final class ChatAPIClientToolExecuteTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private let apiToken = "tool-api-token"

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

    func testExecuteToolPostsArgumentsAndDecodesResult() async throws {
        var capturedBody: [String: Any] = [:]
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/tools/execute/get_weather")
            capturedBody = (try? JSONSerialization.jsonObject(with: request.bodyData)) as? [String: Any] ?? [:]
            return .json(#"{"forecast":"sunny"}"#)
        }

        let result = try await makeClient().executeTool(
            name: "get_weather",
            arguments: .object(["city": .string("NYC")]),
            profileID: "research",
            taintMetadata: VoiceToolRunner.initialTaintMetadata
        )

        XCTAssertEqual((capturedBody["arguments"] as? [String: Any])?["city"] as? String, "NYC")
        XCTAssertEqual(capturedBody["profile_id"] as? String, "research")
        XCTAssertEqual(
            (capturedBody["taint_metadata"] as? [String: Any])?["max_tier"] as? String,
            "trusted_user"
        )
        XCTAssertEqual(result, .object(["forecast": .string("sunny")]))
    }

    func testExecuteToolDefaultsNonObjectArgumentsToEmptyObject() async throws {
        var capturedBody: [String: Any] = [:]
        ChatMockBackendURLProtocol.respond { request in
            capturedBody = (try? JSONSerialization.jsonObject(with: request.bodyData)) as? [String: Any] ?? [:]
            return .json("{}")
        }

        _ = try await makeClient().executeTool(
            name: "noop",
            arguments: .null,
            profileID: nil,
            taintMetadata: VoiceToolRunner.initialTaintMetadata
        )

        let arguments = try XCTUnwrap(capturedBody["arguments"] as? [String: Any])
        XCTAssertTrue(arguments.isEmpty)
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
