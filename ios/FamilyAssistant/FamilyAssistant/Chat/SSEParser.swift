import Foundation

struct ServerSentEvent: Equatable {
    let event: String
    let data: String
}

struct ChatStreamEvent: Equatable {
    let type: ChatStreamEventType
    let turnID: String?
    let seq: Int?
    let text: String?
    let toolCall: ChatBackendToolCall?
    let toolCallID: String?
    let toolResult: String?
    let attachments: [ChatAttachment]
    // Origin of an `attachment` event: "response" (assistant reply) or "trigger"
    // (the user's own upload republished onto the stream). Only response
    // attachments belong in the assistant bubble. Absent → treat as "response".
    let attachmentSource: ChatAttachmentSource
    let confirmation: ChatPendingConfirmation?
    let confirmationResult: ChatConfirmationResult?
    let errorMessage: String?
    let status: String?
}

enum ChatAttachmentSource: String, Equatable {
    case response
    case trigger
}

enum ChatStreamEventType: String, Equatable {
    case text
    case toolCall
    case toolResult
    case attachment
    case toolConfirmationRequest
    case toolConfirmationResult
    case error
    case turnStarted
    case turnEnded
    case userInput
    case connected
    case message
    case heartbeat
    case streamDropped
}

struct ChatConfirmationResult: Equatable {
    let requestID: String
    let approved: Bool
}

final class SSEParser {
    private var buffer = ""
    // turn_id of the event currently being decoded, threaded into baseEvent so
    // every event carries its owning turn for client-side filtering.
    private var currentTurnID: String?
    // seq of the event currently being decoded, so the client can acknowledge
    // the highest received seq after processing turn_ended.
    private var currentSeq: Int?

    func append(_ chunk: String) -> [ServerSentEvent] {
        buffer += chunk
        // Normalize across the accumulated buffer, not per chunk: with a
        // byte-at-a-time feed the CR and LF of a CRLF arrive in separate chunks,
        // so per-chunk replacement never matches. A trailing lone "\r" is left
        // in the buffer until its following byte arrives.
        buffer = buffer.replacingOccurrences(of: "\r\n", with: "\n")
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
        currentTurnID = nil
        currentSeq = nil
        let isNamedEvent = event.event != "message"
        let eventType = ChatStreamEventType(rawValue: camelCase(event.event)) ?? .message
        guard !event.data.isEmpty, event.data != "[DONE]" else {
            return ChatStreamEvent(
                type: eventType,
                turnID: nil,
                seq: nil,
                text: nil,
                toolCall: nil,
                toolCallID: nil,
                toolResult: nil,
                attachments: [],
                attachmentSource: .response,
                confirmation: nil,
                confirmationResult: nil,
                errorMessage: nil,
                status: nil
            )
        }

        guard let data = event.data.data(using: .utf8),
              let payload = try? JSONDecoder.chatDecoder.decode([String: JSONValue].self, from: data)
        else {
            return ChatStreamEvent(
                type: .error,
                turnID: nil,
                seq: nil,
                text: nil,
                toolCall: nil,
                toolCallID: nil,
                toolResult: nil,
                attachments: [],
                attachmentSource: .response,
                confirmation: nil,
                confirmationResult: nil,
                errorMessage: "Malformed stream event: \(event.data)",
                status: nil
            )
        }

        currentTurnID = payload["turn_id"]?.stringValue
        currentSeq = payload["seq"]?.doubleValue.map { Int($0) }

        // Dispatch on the SSE event-name first. A named lifecycle/control frame
        // (e.g. `turn_ended`) is authoritative even when its payload carries a
        // shape that the heuristics below would otherwise sniff differently — a
        // FAILED turn_ended whose payload has an `error` string must still
        // decode as `.turnEnded`, not `.error`, so the live-events path reloads
        // history. Payload-shape sniffing is the fallback for unnamed/legacy
        // frames only.
        if isNamedEvent {
            switch eventType {
            case .turnEnded, .turnStarted, .heartbeat, .streamDropped, .connected:
                let errorMessage = payload["error"]?.stringValue
                return baseEvent(type: eventType, errorMessage: errorMessage, status: payload["status"]?.stringValue)
            case .userInput:
                return baseEvent(type: .userInput, text: payload["content"]?.stringValue)
            default:
                break
            }
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
            // Absent `source` defaults to `.response` (backend always sets it now;
            // legacy frames are response attachments).
            let source = payload["source"]?.stringValue
                .flatMap(ChatAttachmentSource.init(rawValue:)) ?? .response
            return baseEvent(
                type: .attachment,
                attachments: decodeAttachments(.object(payload)),
                attachmentSource: source
            )
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
        attachmentSource: ChatAttachmentSource = .response,
        confirmation: ChatPendingConfirmation? = nil,
        confirmationResult: ChatConfirmationResult? = nil,
        errorMessage: String? = nil,
        status: String? = nil
    ) -> ChatStreamEvent {
        ChatStreamEvent(
            type: type,
            turnID: currentTurnID,
            seq: currentSeq,
            text: text,
            toolCall: toolCall,
            toolCallID: toolCallID,
            toolResult: toolResult,
            attachments: attachments,
            attachmentSource: attachmentSource,
            confirmation: confirmation,
            confirmationResult: confirmationResult,
            errorMessage: errorMessage,
            status: status
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
        case "turn_started":
            "turnStarted"
        case "turn_ended":
            "turnEnded"
        case "user_input":
            "userInput"
        case "stream_dropped":
            "streamDropped"
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
