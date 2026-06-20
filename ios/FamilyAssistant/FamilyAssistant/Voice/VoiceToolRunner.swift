import Foundation

/// Executes a named tool with JSON arguments. Abstracts ``ChatAPIClient`` so the
/// tool runner can be tested without a network.
@MainActor
protocol VoiceToolExecuting {
    func executeTool(name: String, arguments: JSONValue) async throws -> JSONValue
}

extension ChatAPIClient: VoiceToolExecuting {}

/// Bridges Gemini function calls to backend tool execution.
///
/// Mirrors the web voice client's contract: a successful call returns
/// `{"result": <json>}` and a failure returns `{"error": <message>}`, so the
/// model always receives a well-formed response and the turn can continue.
@MainActor
final class VoiceToolRunner {
    private let executor: VoiceToolExecuting

    init(executor: VoiceToolExecuting) {
        self.executor = executor
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
            let result = try await executor.executeTool(name: call.name, arguments: call.args)
            return GeminiFunctionResponse(
                id: call.id,
                name: call.name,
                response: .object(["result": result])
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
