import Foundation

/// Executes a named tool with JSON arguments. Abstracts ``ChatAPIClient`` so the
/// tool runner can be tested without a network.
@MainActor
protocol VoiceToolExecuting {
    func executeTool(
        name: String,
        arguments: JSONValue,
        profileID: String?,
        taintMetadata: JSONValue
    ) async throws -> JSONValue
}

extension ChatAPIClient: VoiceToolExecuting {}

/// Bridges Gemini function calls to backend tool execution.
///
/// Mirrors the web voice client's contract: a successful call returns
/// `{"result": <json>}` and a failure returns `{"error": <message>}`, so the
/// model always receives a well-formed response and the turn can continue.
@MainActor
final class VoiceToolRunner {
    static let initialTaintMetadata: JSONValue = .object([
        "version": .string("runtime_v1"),
        "max_tier": .string("trusted_user"),
        "history_high_taint_present": .bool(false),
        "fresh_high_taint_seen_at_sequence": .null,
        "sources": .array([]),
    ])

    private let executor: VoiceToolExecuting
    private let profileID: String?
    private var taintMetadata = VoiceToolRunner.initialTaintMetadata

    init(executor: VoiceToolExecuting, profileID: String? = nil) {
        self.executor = executor
        self.profileID = profileID
    }

    /// Execute every call in order and collect the responses.
    func run(_ calls: [GeminiFunctionCall]) async -> [GeminiFunctionResponse] {
        var responses: [GeminiFunctionResponse] = []
        for call in calls {
            responses.append(await run(call))
        }
        return responses
    }

    private func run(_ call: GeminiFunctionCall) async -> GeminiFunctionResponse {
        do {
            let result = try await executor.executeTool(
                name: call.name,
                arguments: call.args,
                profileID: profileID,
                taintMetadata: taintMetadata
            )
            if let returnedTaint = result["taint_metadata"] {
                taintMetadata = returnedTaint
            }
            if case .string(let detail) = result["detail"] {
                return errorResponse(for: call, message: detail)
            }
            let toolResult = result["result"] ?? .null
            return GeminiFunctionResponse(
                id: call.id,
                name: call.name,
                response: .object(["result": toolResult])
            )
        } catch let ChatAPIError.server(statusCode, detail) {
            let message = detail ?? "Tool execution failed with status \(statusCode)."
            return errorResponse(for: call, message: message)
        } catch {
            return errorResponse(for: call, message: error.localizedDescription)
        }
    }

    private func errorResponse(for call: GeminiFunctionCall, message: String) -> GeminiFunctionResponse {
        GeminiFunctionResponse(
            id: call.id,
            name: call.name,
            response: .object(["error": .string(message)])
        )
    }
}
