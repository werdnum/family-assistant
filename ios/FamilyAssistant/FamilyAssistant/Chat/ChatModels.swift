import Foundation

enum ChatConstants {
    static let interfaceType = "web"
    static let conversationPrefix = "web_conv_"
    static let maxAttachmentSizeBytes = 100 * 1024 * 1024
    static let photoPickerTranscodedMIMETypes: Set<String> = [
        "image/heic",
        "image/heif",
    ]
    /// Image types the photo picker hands over as-is. HEIC/HEIF are transcoded
    /// to JPEG on the way out, so they are admitted separately.
    static let directPhotoPickerMIMETypes: Set<String> = [
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
    ]

    static let allowedPhotoPickerMIMETypes = directPhotoPickerMIMETypes.union(photoPickerTranscodedMIMETypes)

    static func uploadMIMEType(forPickedPhotoMIMEType mimeType: String) -> String {
        photoPickerTranscodedMIMETypes.contains(mimeType) ? "image/jpeg" : mimeType
    }

    static func uploadFilenameExtension(forPickedPhotoMIMEType mimeType: String, fallback: String?) -> String {
        photoPickerTranscodedMIMETypes.contains(mimeType) ? "jpg" : fallback ?? "jpg"
    }
}

struct ChatConversationSummary: Codable, Equatable, Identifiable {
    let conversationID: String
    let lastMessage: String
    let lastTimestamp: Date
    let messageCount: Int

    var id: String { conversationID }

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case lastMessage = "last_message"
        case lastTimestamp = "last_timestamp"
        case messageCount = "message_count"
    }
}

struct ChatConversationListResponse: Decodable {
    let conversations: [ChatConversationSummary]
    let count: Int
}

/// One frame from the account-global conversation-activity stream
/// (`GET /api/v1/chat/activity/stream`). Advisory only: the fields are not
/// required to act on — any frame means "some conversation you own changed, go
/// refresh the list" — but the id/reason are carried for logging and tests.
struct ChatConversationActivity: Equatable {
    let conversationID: String?
    let reason: String?
}

/// A decoded frame from the account-global activity stream. Control frames are
/// surfaced so connection health can recover on an otherwise-idle stream without
/// treating heartbeats as conversation-list changes.
enum ChatActivityStreamEvent: Equatable {
    case activity(ChatConversationActivity)
    case control
}

struct ChatProfile: Codable, Equatable, Identifiable {
    let id: String
    let description: String
    let llmModel: String?
    let availableTools: [String]
    let enabledMCPServers: [String]
    let delegationOnly: Bool

    enum CodingKeys: String, CodingKey {
        case id
        case description
        case llmModel = "llm_model"
        case availableTools = "available_tools"
        case enabledMCPServers = "enabled_mcp_servers"
        case delegationOnly = "delegation_only"
    }
}

struct ChatProfilesResponse: Decodable {
    let profiles: [ChatProfile]
    let defaultProfileID: String

    enum CodingKeys: String, CodingKey {
        case profiles
        case defaultProfileID = "default_profile_id"
    }
}

struct ChatAttachment: Codable, Equatable, Identifiable {
    let id: String
    var attachmentID: String?
    var type: ChatAttachmentType
    var name: String
    var contentURL: String?
    var mimeType: String?
    var size: Int?
    var localFileURL: URL?
    var uploadState: ChatAttachmentUploadState
    var errorMessage: String?

    enum CodingKeys: String, CodingKey {
        case id
        case attachmentID = "attachment_id"
        case type
        case name
        case contentURL = "content_url"
        case mimeType = "mime_type"
        case size
        case uploadState
        case errorMessage
    }
}

enum ChatAttachmentType: String, Codable, Equatable {
    case image
    case document
    case file

    /// Anything that is not an image is a document: that is the label the
    /// backend keeps in message history, so a file of a type this client has
    /// no case for still reaches the model and survives into later turns.
    /// `.file` remains for attachments the server labels that way.
    static func from(mimeType: String) -> ChatAttachmentType {
        if mimeType.hasPrefix("image/") {
            return .image
        }
        return .document
    }
}

enum ChatAttachmentUploadState: String, Codable, Equatable {
    case local
    case uploading
    case uploaded
    case failed
}

extension ChatAttachment {
    /// Identity for de-duplicating the same attachment reported through more
    /// than one channel. The server-side id is authoritative; the fallbacks only
    /// matter for locally-built attachments that never had one.
    var dedupeKey: String {
        attachmentID ?? contentURL ?? id
    }

    /// A copy that is renderable as an image, or nil when this attachment is not
    /// a displayable image.
    ///
    /// The content URL is derived from the attachment id when the metadata
    /// carries none: a persisted tool-result row records the mime type but not
    /// always a URL, and `/api/attachments/{id}` is the canonical route for a
    /// known id.
    ///
    /// A missing mime type is a different matter — nothing on the client can
    /// prove such an attachment is an image, so it is left alone rather than
    /// guessed at. Resolving it belongs to the server: the messages endpoint
    /// fills in the mime type of persisted attachments from the attachment
    /// registry on read, which is what turns a bare `attachment_reference` (an
    /// id and nothing else) into something displayable here.
    var displayableImage: ChatAttachment? {
        guard type == .image else {
            return nil
        }
        guard let url = contentURL ?? Self.canonicalContentURL(forAttachmentID: attachmentID) else {
            return nil
        }
        var image = self
        image.contentURL = url
        return image
    }

    private static func canonicalContentURL(forAttachmentID attachmentID: String?) -> String? {
        guard let attachmentID,
              let encoded = attachmentID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
        else {
            return nil
        }
        return "/api/attachments/\(encoded)"
    }
}

extension ChatMessage {
    /// How many images one reply may render inline.
    ///
    /// A bubble is a whole agentic turn — `groupToolCallTurns` folds every tool
    /// call of the turn into one — so a turn that took thirty camera snapshots
    /// would otherwise stack thirty full-width images into a single bubble and
    /// fetch them all eagerly, which is exactly the unbounded per-message layout
    /// the watchdog work exists to prevent. Images past the cap are not lost:
    /// they stay on the attachment strip they came from, as thumbnails.
    static let maxInlineImages = 6

    /// The reply's image attachments, to be shown inline in the bubble rather
    /// than inside the tool group.
    ///
    /// The same image reaches the client in two shapes. Live, it arrives as an
    /// `attachment` stream event and lands on the message's own attachments; on
    /// reload it comes back on the tool row that produced it, which
    /// `renderMessages` folds into the tool call. Both are collected here and
    /// de-duplicated by attachment id, so a turn reporting an image through both
    /// paths renders it once — and an image stays put when a live turn is
    /// replaced by its persisted rows.
    ///
    /// User uploads are excluded: they already render as thumbnails on the user
    /// bubble's own attachment strip.
    var inlineImageAttachments: [ChatAttachment] {
        guard role != .user else {
            return []
        }
        var seen: Set<String> = []
        var images: [ChatAttachment] = []
        for attachment in attachments + toolCalls.flatMap(\.attachments) {
            guard images.count < Self.maxInlineImages else {
                break
            }
            guard let image = attachment.displayableImage,
                  seen.insert(image.dedupeKey).inserted
            else {
                continue
            }
            images.append(image)
        }
        return images
    }
}

struct ChatUploadResponse: Decodable, Equatable {
    let attachmentID: String
    let filename: String
    let contentType: String
    let size: Int
    let url: String

    enum CodingKeys: String, CodingKey {
        case attachmentID = "attachment_id"
        case filename
        case contentType = "content_type"
        case size
        case url
    }
}

struct ChatMessage: Identifiable, Equatable {
    let id: String
    var role: ChatMessageRole
    var text: String
    var createdAt: Date
    var toolCalls: [ChatToolCall]
    var attachments: [ChatAttachment]
    var isLoading: Bool
    var status: ChatMessageStatus
    var processingProfileID: String?
    var errorTraceback: String?
    // The turn that produced this message, when known. Persisted rows carry it
    // (from the messages API); it lets a live-follow bubble (`local_follow_<turn>`)
    // be reconciled to its canonical persisted row precisely instead of by
    // heuristic. Nil for optimistic local bubbles and pre-turn_id backends.
    var turnID: String? = nil
}

enum ChatMessageRole: String, Codable, Equatable {
    case user
    case assistant
    case system
    case tool
    case error
}

enum ChatMessageStatus: String, Codable, Equatable {
    case running
    case complete
    case failed
}

struct ChatToolCall: Identifiable, Equatable {
    let id: String
    var name: String
    var argumentsText: String
    var resultText: String?
    var attachments: [ChatAttachment]
    var status: ChatToolStatus
}

enum ChatToolStatus: String, Codable, Equatable {
    case running
    case awaitingApproval
    case approved
    case rejected
    case complete
    case failed
}

struct ChatPendingConfirmation: Codable, Equatable, Identifiable {
    let requestID: String
    let toolName: String
    let toolCallID: String?
    let confirmationPrompt: String
    let args: [String: JSONValue]
    let createdAt: Date?
    let expiresAt: Date?
    let timeoutSeconds: Double
    let timeRemainingSeconds: Double?
    var receivedAt: Date
    var errorMessage: String?

    var id: String { toolCallID ?? requestID }

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case toolName = "tool_name"
        case toolCallID = "tool_call_id"
        case confirmationPrompt = "confirmation_prompt"
        case args
        case createdAt = "created_at"
        case expiresAt = "expires_at"
        case timeoutSeconds = "timeout_seconds"
        case timeRemainingSeconds = "time_remaining_seconds"
        case receivedAt = "received_at"
        case errorMessage = "error_message"
    }

    init(
        requestID: String,
        toolName: String,
        toolCallID: String?,
        confirmationPrompt: String,
        args: [String: JSONValue],
        createdAt: Date?,
        expiresAt: Date?,
        timeoutSeconds: Double,
        timeRemainingSeconds: Double?,
        receivedAt: Date = Date(),
        errorMessage: String? = nil
    ) {
        self.requestID = requestID
        self.toolName = toolName
        self.toolCallID = toolCallID
        self.confirmationPrompt = confirmationPrompt
        self.args = args
        self.createdAt = createdAt
        self.expiresAt = expiresAt
        self.timeoutSeconds = timeoutSeconds
        self.timeRemainingSeconds = timeRemainingSeconds
        self.receivedAt = receivedAt
        self.errorMessage = errorMessage
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        requestID = try container.decode(String.self, forKey: .requestID)
        toolName = try container.decode(String.self, forKey: .toolName)
        toolCallID = try container.decodeIfPresent(String.self, forKey: .toolCallID)
        confirmationPrompt = try container.decode(String.self, forKey: .confirmationPrompt)
        args = try container.decodeIfPresent([String: JSONValue].self, forKey: .args) ?? [:]
        createdAt = try container.decodeIfPresent(Date.self, forKey: .createdAt)
        expiresAt = try container.decodeIfPresent(Date.self, forKey: .expiresAt)
        timeoutSeconds = try container.decodeIfPresent(Double.self, forKey: .timeoutSeconds) ?? 0
        timeRemainingSeconds = try container.decodeIfPresent(Double.self, forKey: .timeRemainingSeconds)
        receivedAt = try container.decodeIfPresent(Date.self, forKey: .receivedAt) ?? Date()
        errorMessage = try container.decodeIfPresent(String.self, forKey: .errorMessage)
    }
}

struct ChatPendingConfirmationsResponse: Decodable {
    let confirmations: [ChatPendingConfirmation]
}

enum ChatConfirmationStatus: String, Decodable, Equatable {
    case pending
    case approved
    case rejected
    case expired
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ChatConfirmationStatus(rawValue: raw) ?? .unknown
    }

    var isPending: Bool { self == .pending }
}

struct ChatConfirmationDetail: Decodable, Equatable {
    let requestID: String
    let toolName: String
    let toolCallID: String?
    let confirmationPrompt: String
    let args: [String: JSONValue]
    let status: ChatConfirmationStatus
    let timeRemainingSeconds: Double

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case toolName = "tool_name"
        case toolCallID = "tool_call_id"
        case confirmationPrompt = "confirmation_prompt"
        case args
        case status
        case timeRemainingSeconds = "time_remaining_seconds"
    }

    init(
        requestID: String,
        toolName: String,
        toolCallID: String?,
        confirmationPrompt: String,
        args: [String: JSONValue],
        status: ChatConfirmationStatus,
        timeRemainingSeconds: Double
    ) {
        self.requestID = requestID
        self.toolName = toolName
        self.toolCallID = toolCallID
        self.confirmationPrompt = confirmationPrompt
        self.args = args
        self.status = status
        self.timeRemainingSeconds = timeRemainingSeconds
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        requestID = try container.decode(String.self, forKey: .requestID)
        toolName = try container.decode(String.self, forKey: .toolName)
        toolCallID = try container.decodeIfPresent(String.self, forKey: .toolCallID)
        confirmationPrompt = try container.decode(String.self, forKey: .confirmationPrompt)
        args = try container.decodeIfPresent([String: JSONValue].self, forKey: .args) ?? [:]
        status = try container.decode(ChatConfirmationStatus.self, forKey: .status)
        timeRemainingSeconds = try container.decodeIfPresent(Double.self, forKey: .timeRemainingSeconds) ?? 0
    }
}

struct ChatActiveTurnInfo: Decodable, Equatable {
    let turnID: String
    let startedAt: Date
    let latestSeq: Int
    let status: String

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case startedAt = "started_at"
        case latestSeq = "latest_seq"
        case status
    }
}

struct ChatConversationMessagesResponse: Decodable {
    let conversationID: String
    let messages: [ChatBackendMessage]
    let count: Int
    let totalMessages: Int
    let hasMoreBefore: Bool
    let hasMoreAfter: Bool
    let activeTurns: [ChatActiveTurnInfo]

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case messages
        case count
        case totalMessages = "total_messages"
        case hasMoreBefore = "has_more_before"
        case hasMoreAfter = "has_more_after"
        case activeTurns = "active_turns"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        conversationID = try container.decode(String.self, forKey: .conversationID)
        messages = try container.decode([ChatBackendMessage].self, forKey: .messages)
        count = try container.decode(Int.self, forKey: .count)
        totalMessages = try container.decode(Int.self, forKey: .totalMessages)
        hasMoreBefore = try container.decodeIfPresent(Bool.self, forKey: .hasMoreBefore) ?? false
        hasMoreAfter = try container.decodeIfPresent(Bool.self, forKey: .hasMoreAfter) ?? false
        activeTurns = try container.decodeIfPresent([ChatActiveTurnInfo].self, forKey: .activeTurns) ?? []
    }
}

struct ChatBackendMessage: Decodable, Equatable {
    let internalID: String
    let turnID: String?
    let role: ChatMessageRole
    let text: String
    let timestamp: Date
    let toolCalls: [ChatBackendToolCall]
    let toolCallID: String?
    let errorTraceback: String?
    let attachments: [ChatBackendAttachment]
    let processingProfileID: String?
    let metadata: [String: JSONValue]?

    enum CodingKeys: String, CodingKey {
        case internalID = "internal_id"
        case turnID = "turn_id"
        case role
        case content
        case timestamp
        case toolCalls = "tool_calls"
        case toolCallID = "tool_call_id"
        case errorTraceback = "error_traceback"
        case attachments
        case processingProfileID = "processing_profile_id"
        case metadata
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let intID = try? container.decode(Int.self, forKey: .internalID) {
            internalID = String(intID)
        } else {
            internalID = try container.decode(String.self, forKey: .internalID)
        }
        turnID = try container.decodeIfPresent(String.self, forKey: .turnID)
        role = try container.decode(ChatMessageRole.self, forKey: .role)
        timestamp = try container.decode(Date.self, forKey: .timestamp)
        toolCalls = try container.decodeIfPresent([ChatBackendToolCall].self, forKey: .toolCalls) ?? []
        toolCallID = try container.decodeIfPresent(String.self, forKey: .toolCallID)
        errorTraceback = try container.decodeIfPresent(String.self, forKey: .errorTraceback)
        attachments = try container.decodeIfPresent([ChatBackendAttachment].self, forKey: .attachments) ?? []
        processingProfileID = try container.decodeIfPresent(String.self, forKey: .processingProfileID)
        metadata = try container.decodeIfPresent([String: JSONValue].self, forKey: .metadata)

        if let content = try? container.decode(String.self, forKey: .content) {
            text = content
        } else if let parts = try? container.decode([JSONValue].self, forKey: .content) {
            text = parts.compactMap(\.textContent).joined(separator: "\n\n")
        } else {
            text = ""
        }
    }
}

struct ChatBackendToolCall: Codable, Equatable {
    let id: String
    let type: String?
    let function: ChatBackendToolFunction?
    let name: String?
    let arguments: JSONValue?

    var displayName: String {
        function?.name ?? name ?? "unknown"
    }

    var argumentsText: String {
        if let functionArguments = function?.arguments {
            return functionArguments
        }
        return arguments?.jsonString ?? "{}"
    }
}

struct ChatBackendToolFunction: Codable, Equatable {
    let name: String
    let arguments: String?

    enum CodingKeys: String, CodingKey {
        case name
        case arguments
    }

    init(name: String, arguments: String?) {
        self.name = name
        self.arguments = arguments
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        if let value = try? container.decode(String.self, forKey: .arguments) {
            arguments = value
        } else if let value = try? container.decode(JSONValue.self, forKey: .arguments) {
            arguments = value.jsonString
        } else {
            arguments = nil
        }
    }
}

struct ChatBackendAttachment: Codable, Equatable {
    let attachmentID: String?
    let name: String?
    let type: String?
    let url: String?
    let contentURL: String?
    let mimeType: String?
    let description: String?
    let filename: String?
    let size: Int?

    enum CodingKeys: String, CodingKey {
        case attachmentID = "attachment_id"
        case name
        case type
        case url
        case contentURL = "content_url"
        case mimeType = "mime_type"
        case description
        case filename
        case size
    }

    var chatAttachment: ChatAttachment {
        let resolvedURL = contentURL ?? url
        let resolvedName = name ?? filename ?? description ?? attachmentID ?? "Attachment"
        let resolvedType: ChatAttachmentType
        if let mimeType {
            resolvedType = ChatAttachmentType.from(mimeType: mimeType)
        } else if type == "image" {
            resolvedType = .image
        } else if type == "document" {
            resolvedType = .document
        } else {
            resolvedType = .file
        }
        return ChatAttachment(
            id: attachmentID ?? UUID().uuidString,
            attachmentID: attachmentID,
            type: resolvedType,
            name: resolvedName,
            contentURL: resolvedURL,
            mimeType: mimeType,
            size: size,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
    }
}

enum JSONValue: Codable, Equatable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unsupported JSON value")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    var jsonString: String {
        guard let data = try? JSONEncoder.chatEncoder.encode(self),
              let string = String(data: data, encoding: .utf8)
        else {
            return String(describing: self)
        }
        return string
    }

    var displayString: String {
        switch self {
        case .string(let value):
            value
        case .number(let value):
            if value.rounded() == value {
                String(Int(value))
            } else {
                String(value)
            }
        case .bool(let value):
            value ? "true" : "false"
        case .object, .array:
            jsonString
        case .null:
            "null"
        }
    }

    var textContent: String? {
        guard case .object(let object) = self else {
            return nil
        }
        if case .string("text") = object["type"],
           case .string(let text) = object["text"]
        {
            return text
        }
        return nil
    }
}

extension JSONDecoder {
    static var chatDecoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            if let date = ISO8601DateFormatter.chat.date(from: value) {
                return date
            }
            if let date = ISO8601DateFormatter.chatWithFractionalSeconds.date(from: value) {
                return date
            }
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Invalid ISO-8601 date: \(value)")
        }
        return decoder
    }
}

extension JSONEncoder {
    static var chatEncoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }
}

private extension ISO8601DateFormatter {
    static let chat: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    static let chatWithFractionalSeconds: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}
