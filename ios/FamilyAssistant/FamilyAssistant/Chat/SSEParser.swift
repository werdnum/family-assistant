import Foundation

struct ServerSentEvent: Equatable {
    let event: String
    let data: String
}

struct ChatStreamEvent: Equatable {
    let type: ChatStreamEventType
    let text: String?
    let toolCall: ChatBackendToolCall?
    let toolCallID: String?
    let toolResult: String?
    let attachments: [ChatAttachment]
    let confirmation: ChatPendingConfirmation?
    let confirmationResult: ChatConfirmationResult?
    let errorMessage: String?
}

enum ChatStreamEventType: String, Equatable {
    case text
    case toolCall
    case toolResult
    case attachment
    case toolConfirmationRequest
    case toolConfirmationResult
    case error
    case end
    case close
    case connected
    case message
    case heartbeat
}

struct ChatConfirmationResult: Equatable {
    let requestID: String
    let approved: Bool
}

final class SSEParser {
    private var buffer = ""

    func append(_ chunk: String) -> [ServerSentEvent] {
        buffer += chunk.replacingOccurrences(of: "\r\n", with: "\n")
        var events: [ServerSentEvent] = []

        while let range = buffer.range(of: "\n\n") {
            let rawEvent = String(buffer[..<range.lowerBound])
            buffer.removeSubrange(buffer.startIndex..<range.upperBound)
            if let event = parse(rawEvent) {
                events.append(event)
            }
        }

        return events
    }

    func flush() -> [ServerSentEvent] {
        guard !buffer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            buffer = ""
            return []
        }
        let rawEvent = buffer
        buffer = ""
        return parse(rawEvent).map { [$0] } ?? []
    }

    func decode(_ event: ServerSentEvent) -> ChatStreamEvent {
        let eventType = ChatStreamEventType(rawValue: camelCase(event.event)) ?? .message
        guard !event.data.isEmpty, event.data != "[DONE]" else {
            return ChatStreamEvent(
                type: eventType,
                text: nil,
                toolCall: nil,
                toolCallID: nil,
                toolResult: nil,
                attachments: [],
                confirmation: nil,
                confirmationResult: nil,
                errorMessage: nil
            )
        }

        guard let data = event.data.data(using: .utf8),
              let payload = try? JSONDecoder.chatDecoder.decode([String: JSONValue].self, from: data)
        else {
            return ChatStreamEvent(
                type: .error,
                text: nil,
                toolCall: nil,
                toolCallID: nil,
                toolResult: nil,
                attachments: [],
                confirmation: nil,
                confirmationResult: nil,
                errorMessage: "Malformed stream event: \(event.data)"
            )
        }

        if eventType == .text, case .string(let content) = payload["content"] {
            return baseEvent(type: .text, text: content)
        }

        if let toolCall = decodeToolCall(from: payload["tool_call"]) {
            return baseEvent(type: .toolCall, toolCall: toolCall)
        }

        if case .string(let toolCallID) = payload["tool_call_id"],
           let result = payload["result"]
        {
            let attachments = decodeAttachments(payload["attachments"])
            return baseEvent(
                type: .toolResult,
                toolCallID: toolCallID,
                toolResult: result.displayString,
                attachments: attachments
            )
        }

        if case .string(let requestID) = payload["request_id"],
           case .string(let toolName) = payload["tool_name"]
        {
            let confirmation = ChatPendingConfirmation(
                requestID: requestID,
                toolName: toolName,
                toolCallID: payload["tool_call_id"]?.stringValue,
                confirmationPrompt: payload["confirmation_prompt"]?.stringValue
                    ?? "Do you want to execute this tool?",
                args: payload["args"]?.objectValue ?? [:],
                createdAt: Date(),
                expiresAt: nil,
                timeoutSeconds: payload["timeout_seconds"]?.doubleValue ?? 0,
                timeRemainingSeconds: payload["timeout_seconds"]?.doubleValue,
                receivedAt: Date()
            )
            return baseEvent(type: .toolConfirmationRequest, confirmation: confirmation)
        }

        if case .string(let requestID) = payload["request_id"],
           case .bool(let approved) = payload["approved"]
        {
            return baseEvent(
                type: .toolConfirmationResult,
                confirmationResult: ChatConfirmationResult(requestID: requestID, approved: approved)
            )
        }

        if payload["attachment_id"] != nil || payload["content_url"] != nil || payload["url"] != nil {
            return baseEvent(type: .attachment, attachments: decodeAttachments(.object(payload)))
        }

        if case .string(let error) = payload["error"] {
            return baseEvent(type: .error, errorMessage: error)
        }

        return baseEvent(type: eventType)
    }

    private func parse(_ rawEvent: String) -> ServerSentEvent? {
        var event = "message"
        var dataLines: [String] = []

        for rawLine in rawEvent.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix(":") {
                continue
            }
            if line.hasPrefix("event:") {
                event = String(line.dropFirst("event:".count)).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("data:") {
                dataLines.append(String(line.dropFirst("data:".count)).trimmingCharacters(in: .whitespaces))
            }
        }

        guard !event.isEmpty || !dataLines.isEmpty else {
            return nil
        }
        return ServerSentEvent(event: event, data: dataLines.joined(separator: "\n"))
    }

    private func decodeToolCall(from value: JSONValue?) -> ChatBackendToolCall? {
        guard let value,
              let data = value.jsonString.data(using: .utf8)
        else {
            return nil
        }
        return try? JSONDecoder.chatDecoder.decode(ChatBackendToolCall.self, from: data)
    }

    private func decodeAttachments(_ value: JSONValue?) -> [ChatAttachment] {
        guard let value else {
            return []
        }

        let attachmentValues: [JSONValue]
        if case .array(let values) = value {
            attachmentValues = values
        } else {
            attachmentValues = [value]
        }

        return attachmentValues.compactMap { item in
            guard let data = item.jsonString.data(using: .utf8),
                  let backend = try? JSONDecoder.chatDecoder.decode(ChatBackendAttachment.self, from: data)
            else {
                return nil
            }
            return backend.chatAttachment
        }
    }

    private func baseEvent(
        type: ChatStreamEventType,
        text: String? = nil,
        toolCall: ChatBackendToolCall? = nil,
        toolCallID: String? = nil,
        toolResult: String? = nil,
        attachments: [ChatAttachment] = [],
        confirmation: ChatPendingConfirmation? = nil,
        confirmationResult: ChatConfirmationResult? = nil,
        errorMessage: String? = nil
    ) -> ChatStreamEvent {
        ChatStreamEvent(
            type: type,
            text: text,
            toolCall: toolCall,
            toolCallID: toolCallID,
            toolResult: toolResult,
            attachments: attachments,
            confirmation: confirmation,
            confirmationResult: confirmationResult,
            errorMessage: errorMessage
        )
    }

    private func camelCase(_ value: String) -> String {
        switch value {
        case "tool_call":
            "toolCall"
        case "tool_result":
            "toolResult"
        case "tool_confirmation_request":
            "toolConfirmationRequest"
        case "tool_confirmation_result":
            "toolConfirmationResult"
        default:
            value
        }
    }
}

extension JSONValue {
    var stringValue: String? {
        if case .string(let value) = self {
            return value
        }
        return nil
    }

    var doubleValue: Double? {
        if case .number(let value) = self {
            return value
        }
        return nil
    }

    var objectValue: [String: JSONValue]? {
        if case .object(let value) = self {
            return value
        }
        return nil
    }
}
