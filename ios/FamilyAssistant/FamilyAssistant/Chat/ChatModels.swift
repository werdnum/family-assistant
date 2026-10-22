import Foundation

/// The processing profile the user last chose, which every entry point that is
/// not pinned to a conversation starts from — the chat composer and the voice
/// tab alike. Owning the stored key in one place is what keeps the two from
/// drifting onto different profiles.
enum PreferredProfile {
    /// The profile id a fresh session runs under. `default_assistant` until the
    /// user picks something else, matching the backend's own default.
    static var id: String {
        UserDefaults.standard.string(forKey: storageKey) ?? fallbackID
    }

    static func store(_ profileID: String) {
        UserDefaults.standard.set(profileID, forKey: storageKey)
    }

    static let fallbackID = "default_assistant"
    private static let storageKey = "selectedProfileId"
}

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
    /// Generated one-line summary; absent until the server has summarized the
    /// conversation, when the list falls back to the last message.
    var summary: String?
    /// Present only on search results: a snippet of a message that matched.
    var matchExcerpt: String?
    /// `home`, `thread` or `conversation`; absent from a server without threads.
    var kind: String?

    var id: String { conversationID }

    /// What the conversation list labels this row with.
    var displayTitle: String {
        if let summary, !summary.isEmpty {
            return summary
        }
        return lastMessage.isEmpty ? "New chat" : lastMessage
    }

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case lastMessage = "last_message"
        case summary
        case lastTimestamp = "last_timestamp"
        case messageCount = "message_count"
        case matchExcerpt = "match_excerpt"
        case kind
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

/// One intelligence level a profile can be run at: a named model recipe the
/// user may select per request, not a model id.
struct ChatModelTier: Codable, Equatable, Identifiable {
    let id: String
    let label: String
    let tierDescription: String?

    enum CodingKeys: String, CodingKey {
        case id
        case label
        case tierDescription = "description"
    }

    init(id: String, label: String, tierDescription: String?) {
        self.id = id
        self.label = label
        self.tierDescription = tierDescription
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        // The server always sends a label (falling back to the id itself), but a
        // tier that names no level is worse than one named by its id.
        label = try container.decodeIfPresent(String.self, forKey: .label) ?? id
        tierDescription = try container.decodeIfPresent(String.self, forKey: .tierDescription)
    }
}

struct ChatProfile: Codable, Equatable, Identifiable {
    let id: String
    let description: String
    let llmModel: String?
    let availableTools: [String]
    let enabledMCPServers: [String]
    let delegationOnly: Bool
    /// Whether the profile picker offers this profile. False for internal
    /// profiles such as delegation targets and background tasks.
    let userSelectable: Bool
    /// Tiers this profile permits the user to choose between, in configuration
    /// order. Empty for a profile pinned to one model, which is how the client
    /// knows to offer no intelligence control for it.
    let modelTiers: [ChatModelTier]
    /// The tier the profile runs at when a request names none. Nil for a pinned
    /// profile.
    let defaultModelTier: String?
    /// Whether Home's turns may enter this profile: a thread on it can be
    /// continued from Home with context, or have a Home turn moved into it.
    /// Elsewhere the server refuses both, so the client does not offer them.
    var receivesHouseholdContext = false

    enum CodingKeys: String, CodingKey {
        case id
        case description
        case llmModel = "llm_model"
        case availableTools = "available_tools"
        case enabledMCPServers = "enabled_mcp_servers"
        case delegationOnly = "delegation_only"
        case userSelectable = "user_selectable"
        case modelTiers = "model_tiers"
        case defaultModelTier = "default_model_tier"
        case receivesHouseholdContext = "receives_household_context"
    }

    init(
        id: String,
        description: String,
        llmModel: String?,
        availableTools: [String],
        enabledMCPServers: [String],
        delegationOnly: Bool,
        userSelectable: Bool? = nil,
        modelTiers: [ChatModelTier] = [],
        defaultModelTier: String? = nil,
        receivesHouseholdContext: Bool = false
    ) {
        self.id = id
        self.description = description
        self.llmModel = llmModel
        self.availableTools = availableTools
        self.enabledMCPServers = enabledMCPServers
        self.delegationOnly = delegationOnly
        self.userSelectable = userSelectable ?? !delegationOnly
        self.modelTiers = modelTiers
        self.defaultModelTier = defaultModelTier
        self.receivesHouseholdContext = receivesHouseholdContext
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        description = try container.decode(String.self, forKey: .description)
        llmModel = try container.decodeIfPresent(String.self, forKey: .llmModel)
        availableTools = try container.decode([String].self, forKey: .availableTools)
        enabledMCPServers = try container.decode([String].self, forKey: .enabledMCPServers)
        delegationOnly = try container.decode(Bool.self, forKey: .delegationOnly)
        // Absent against a server that predates the flag, which listed every
        // profile it did not mark delegation-only.
        userSelectable = try container.decodeIfPresent(Bool.self, forKey: .userSelectable)
            ?? !delegationOnly
        // Absent against a server that predates tier selection: no tiers means no
        // control, which is the same thing a pinned profile reports.
        modelTiers = try container.decodeIfPresent([ChatModelTier].self, forKey: .modelTiers) ?? []
        defaultModelTier = try container.decodeIfPresent(String.self, forKey: .defaultModelTier)
        // Absent against a server that predates the context gate: offering
        // nothing is what the server would then refuse anyway.
        receivesHouseholdContext = try container.decodeIfPresent(
            Bool.self,
            forKey: .receivesHouseholdContext
        ) ?? false
    }

    /// Whether this profile gives the user something to decide. One tier is not a
    /// choice, so the intelligence control is hidden rather than shown dead.
    var offersModelTierChoice: Bool {
        modelTiers.count > 1
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
    // The intelligence level this reply was produced at, from the persisted
    // message's `reasoning_info`. Nil on user messages, on a profile pinned to a
    // single model, and on an optimistic bubble that its persisted row has not
    // replaced yet — the badge appears when the turn settles.
    var modelTier: String? = nil
    // Who chose that tier: `user`, `model` (another profile), `auto` (the
    // router), `experiment` (a live experiment's draw), or `default` for the
    // profile's own. What ran and who asked for it are separate questions.
    var modelTierSource: String? = nil
    // The tier the person chose when an experiment served the turn on another:
    // it is still the conversation's choice, and the server continues it.
    var modelTierRequested: String? = nil
    /// On a Home message echoing a finished thread's outcome: the thread's
    /// conversation id, so the message is shown as coming from it.
    var echoedFromThread: String? = nil
    /// On a Home message marking a turn the user moved into a thread: that
    /// thread's conversation id. Shown as "Moved to …" in place of its text.
    var movedToThread: String? = nil
    /// The user's thumbs up or down on the turn this message belongs to.
    var feedback: ChatTurnRating? = nil
}

/// A thumbs up or down on an assistant turn.
enum ChatTurnRating: String, Codable, Equatable {
    case up
    case down
}

/// How a rating write ended. `superseded` means a newer choice for the same
/// turn replaced it, so its result says nothing about what is shown.
enum TurnFeedbackOutcome: Equatable {
    case saved
    case superseded
    case failed
}

/// The longest comment the server keeps with a rating.
let maxTurnFeedbackCommentLength = 2000

/// The intelligence level a persisted reply ran at, as reported on the message's
/// `reasoning_info`. Only the tier fields are decoded: the token counts and
/// request id in the same blob have no surface on iOS.
struct ChatMessageReasoningInfo: Decodable, Equatable {
    let modelTier: String?
    let modelTierSource: String?
    let modelTierRequested: String?

    enum CodingKeys: String, CodingKey {
        case modelTier = "model_tier"
        case modelTierSource = "model_tier_source"
        case modelTierRequested = "model_tier_requested"
    }
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
    /// How the server describes the call to the person watching.
    var display: ChatToolCallDisplay? = nil
    /// How much of its message's text (in UTF-16 code units) came before the
    /// call, so the bubble can draw it between the text written before and
    /// after it. Nil sorts after all of the text.
    var textOffset: Int? = nil
}

/// The server's phrases for a tool call: what it is doing, what it did, and
/// whether it changed anything (rather than only reading).
struct ChatToolCallDisplay: Codable, Equatable {
    let active: String
    let done: String
    let changes: Bool
}

/// What a tool call shows the user, the same states for a live stream and for
/// history. Only `succeeded` reads as done: a finished call's outcome comes
/// from the backend (`outcome` on a `tool_result` event, `tool_outcome` on a
/// history row), and a call with no result once its turn is over is `unknown`,
/// never `running`.
enum ChatToolStatus: String, Codable, Equatable {
    case running
    case awaitingApproval
    case succeeded
    case failed
    case rejected
    case unknown

    /// A finished call's outcome as the backend names it.
    init?(backendOutcome: String?) {
        switch backendOutcome {
        case "succeeded":
            self = .succeeded
        case "failed":
            self = .failed
        case "rejected":
            self = .rejected
        default:
            return nil
        }
    }

    /// Still in progress: running, or waiting for the user to approve it.
    var isPending: Bool {
        self == .running || self == .awaitingApproval
    }

    /// Finished without succeeding: failed, not run, or no result recorded.
    var didNotSucceed: Bool {
        self == .failed || self == .rejected || self == .unknown
    }
}

extension ChatToolStatus {
    /// Short label for a call's state, or nil for a call that succeeded.
    var displayLabel: String? {
        switch self {
        case .running:
            "Running"
        case .awaitingApproval:
            "Needs approval"
        case .succeeded:
            nil
        case .failed:
            "Failed"
        case .rejected:
            "Not run"
        case .unknown:
            "No result recorded"
        }
    }

    /// Whether a call needs the user, so its group opens by itself: only an
    /// approval they have to give. A running call is named in the group's
    /// collapsed header instead, and one that did not succeed is counted there.
    /// Only the auto state depends on this: a group the user opened or closed
    /// stays that way whatever its calls do next.
    var wantsAttention: Bool {
        self == .awaitingApproval
    }
}

/// Compact, human-readable text for a tool call card. Raw arguments and results
/// stay available, pretty-printed, behind the card's details.
extension ChatToolCall {
    private static let argumentSummaryLimit = 80
    private static let resultPreviewLimit = 240
    private static let primaryArgumentKeys = [
        "query", "sql", "title", "name", "url", "path", "prompt", "message",
        "text", "description", "target_service", "entity_id",
    ]
    private static let listResultKeys = ["rows", "results", "items", "data", "events", "notes", "matches"]

    /// "query_trino" -> "Query trino".
    var displayName: String {
        let words = name
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .trimmingCharacters(in: .whitespaces)
            .lowercased()
        guard let first = words.first else {
            return "Tool"
        }
        return first.uppercased() + words.dropFirst()
    }

    /// What the call is doing, for a call still in progress or one that did
    /// not succeed. A call the server did not describe reads as its name and
    /// most telling argument.
    var activePhrase: String {
        display?.active ?? fallbackPhrase
    }

    /// What the call did, for a call that succeeded.
    var donePhrase: String {
        display?.done ?? fallbackPhrase
    }

    /// The phrase that matches the call's state: past tense only once it has
    /// succeeded.
    var phrase: String {
        status == .succeeded ? donePhrase : activePhrase
    }

    private var fallbackPhrase: String {
        argumentsSummary.map { "\(displayName) · \($0)" } ?? displayName
    }

    /// One line describing what the call was asked to do, or nil when the
    /// arguments hold nothing a person would read.
    var argumentsSummary: String? {
        guard case .object(let arguments) = Self.parseJSON(argumentsText) else {
            return nil
        }
        for key in Self.primaryArgumentKeys {
            if let text = arguments[key].flatMap(Self.scalarText) {
                return Self.clip(text, to: Self.argumentSummaryLimit)
            }
        }
        let parts = arguments.keys.sorted().compactMap { key -> String? in
            guard let text = arguments[key].flatMap(Self.scalarText) else {
                return nil
            }
            return "\(key.replacingOccurrences(of: "_", with: " ")): \(text)"
        }
        guard !parts.isEmpty else {
            return nil
        }
        return Self.clip(parts.prefix(2).joined(separator: " · "), to: Self.argumentSummaryLimit)
    }

    /// A short preview of the result: plain text clipped, JSON described by its
    /// shape instead of dumped.
    var resultPreview: String? {
        guard let resultText else {
            return nil
        }
        let trimmed = resultText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }
        if let value = Self.parseJSON(trimmed), Self.looksLikeJSON(trimmed) {
            return Self.describe(value)
        }
        return Self.clip(trimmed, to: Self.resultPreviewLimit)
    }

    var prettyArgumentsText: String {
        Self.prettyPrinted(argumentsText)
    }

    var prettyResultText: String? {
        resultText.map(Self.prettyPrinted)
    }

    private static func looksLikeJSON(_ text: String) -> Bool {
        text.hasPrefix("{") || text.hasPrefix("[")
    }

    private static func parseJSON(_ text: String) -> JSONValue? {
        try? JSONDecoder().decode(JSONValue.self, from: Data(text.utf8))
    }

    private static func prettyPrinted(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard looksLikeJSON(trimmed),
              let object = try? JSONSerialization.jsonObject(with: Data(trimmed.utf8)),
              let data = try? JSONSerialization.data(
                  withJSONObject: object,
                  options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
              ),
              let pretty = String(data: data, encoding: .utf8)
        else {
            return text
        }
        return pretty
    }

    private static func scalarText(_ value: JSONValue) -> String? {
        switch value {
        case .string(let text):
            text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : text
        case .number, .bool:
            value.displayString
        case .object, .array, .null:
            nil
        }
    }

    private static func clip(_ text: String, to limit: Int) -> String {
        let singleLine = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard singleLine.count > limit else {
            return singleLine
        }
        return String(singleLine.prefix(limit - 1)) + "…"
    }

    private static func describeList(_ items: [JSONValue], noun: String) -> String {
        let scalars = items.compactMap(scalarText)
        if !items.isEmpty, scalars.count == items.count {
            return clip(scalars.joined(separator: ", "), to: resultPreviewLimit)
        }
        let singular = noun.hasSuffix("s") ? String(noun.dropLast()) : noun
        return items.count == 1 ? "1 \(singular)" : "\(items.count) \(noun)"
    }

    private static func describe(_ value: JSONValue) -> String {
        switch value {
        case .array(let items):
            return describeList(items, noun: "items")
        case .object(let object):
            if let message = (object["error"] ?? object["message"]).flatMap(scalarText) {
                return clip(message, to: resultPreviewLimit)
            }
            let lists = object.keys.sorted().compactMap { key -> (String, [JSONValue])? in
                guard case .array(let items) = object[key] else {
                    return nil
                }
                return (key, items)
            }
            if let list = lists.first(where: { listResultKeys.contains($0.0) }) ?? (lists.count == 1 ? lists[0] : nil) {
                return describeList(list.1, noun: list.0.replacingOccurrences(of: "_", with: " "))
            }
            return object.count == 1 ? "1 field" : "\(object.count) fields"
        case .string, .number, .bool, .null:
            return clip(value.displayString, to: resultPreviewLimit)
        }
    }
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

/// A background delegation the conversation is still waiting to hear back from.
/// Mirrors `PendingDelegation` in `web/routers/chat_api.py`.
struct ChatPendingDelegation: Decodable, Equatable, Identifiable {
    let delegationID: String
    let targetProfileID: String
    let status: String
    let requestPreview: String
    let createdAt: Date
    let startedAt: Date?
    let completedAt: Date?
    let cancelRequested: Bool
    let childrenTotal: Int
    let childrenFinished: Int

    var id: String { delegationID }

    enum CodingKeys: String, CodingKey {
        case delegationID = "delegation_id"
        case targetProfileID = "target_profile_id"
        case status
        case requestPreview = "request_preview"
        case createdAt = "created_at"
        case startedAt = "started_at"
        case completedAt = "completed_at"
        case cancelRequested = "cancel_requested"
        case childrenTotal = "children_total"
        case childrenFinished = "children_finished"
    }

    var profileDisplayName: String {
        switch targetProfileID {
        case "default_assistant":
            return "Assistant"
        case "browser":
            return "Browser"
        case "research":
            return "Research"
        case "event_handler":
            return "Events"
        default:
            return targetProfileID.prefix(1).uppercased() + targetProfileID.dropFirst()
        }
    }

    var statusLabel: String {
        if cancelRequested {
            return "Cancelling"
        }
        switch status {
        case "queued":
            return "Queued"
        case "completed", "failed":
            // The run is done; the reply about it is still being written.
            return "Finishing up"
        default:
            if childrenTotal > 0 {
                return "\(childrenFinished) of \(childrenTotal) done"
            }
            return "Working on it"
        }
    }

    func startedAgo(now: Date) -> String {
        let minutes = Int(now.timeIntervalSince(startedAt ?? createdAt) / 60)
        if minutes < 1 {
            return "started just now"
        }
        if minutes < 60 {
            return "started \(minutes) min ago"
        }
        return "started \(minutes / 60) h ago"
    }
}

struct ChatPendingDelegationsResponse: Decodable {
    let conversationID: String
    let delegations: [ChatPendingDelegation]

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case delegations
    }
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
    /// The profile the conversation's history is under, sent only when the
    /// request asks for it (`include_conversation_profile`). For a thread it is
    /// the thread's own profile.
    let latestUserProfileID: String?
    let conversationKind: ChatConversationKind?
    let thread: ChatThreadSummary?

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case messages
        case count
        case totalMessages = "total_messages"
        case hasMoreBefore = "has_more_before"
        case hasMoreAfter = "has_more_after"
        case activeTurns = "active_turns"
        case latestUserProfileID = "latest_user_profile_id"
        case conversationKind = "conversation_kind"
        case thread
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
        latestUserProfileID = try container.decodeIfPresent(String.self, forKey: .latestUserProfileID)
        conversationKind = ChatConversationKind(
            rawValue: try container.decodeIfPresent(String.self, forKey: .conversationKind)
        )
        thread = try container.decodeIfPresent(ChatThreadSummary.self, forKey: .thread)
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
    /// For a tool row: how the call ended, as the backend classified it.
    let toolOutcome: ChatToolStatus?
    let attachments: [ChatBackendAttachment]
    let processingProfileID: String?
    let reasoningInfo: ChatMessageReasoningInfo?
    let metadata: [String: JSONValue]?
    /// On a Home row echoing a finished thread's outcome: that thread.
    let echoedFromThread: String?
    /// On a Home row marking a turn moved into a thread: that thread.
    let movedToThread: String?
    /// The caller's rating of the row's turn, on every row of a rated turn.
    let feedback: ChatTurnRating?

    enum CodingKeys: String, CodingKey {
        case internalID = "internal_id"
        case turnID = "turn_id"
        case role
        case content
        case timestamp
        case toolCalls = "tool_calls"
        case toolCallID = "tool_call_id"
        case errorTraceback = "error_traceback"
        case toolOutcome = "tool_outcome"
        case attachments
        case processingProfileID = "processing_profile_id"
        case reasoningInfo = "reasoning_info"
        case metadata
        case echoedFromThread = "echoed_from_thread"
        case movedToThread = "moved_to_thread"
        case feedback
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
        toolOutcome = ChatToolStatus(
            backendOutcome: try container.decodeIfPresent(String.self, forKey: .toolOutcome)
        )
        attachments = try container.decodeIfPresent([ChatBackendAttachment].self, forKey: .attachments) ?? []
        processingProfileID = try container.decodeIfPresent(String.self, forKey: .processingProfileID)
        reasoningInfo = try container.decodeIfPresent(ChatMessageReasoningInfo.self, forKey: .reasoningInfo)
        metadata = try container.decodeIfPresent([String: JSONValue].self, forKey: .metadata)
        echoedFromThread = try container.decodeIfPresent(String.self, forKey: .echoedFromThread)
        movedToThread = try container.decodeIfPresent(String.self, forKey: .movedToThread)
        feedback = try container.decodeIfPresent(ChatTurnRating.self, forKey: .feedback)

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
    var display: ChatToolCallDisplay? = nil

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
            if let integer = Int(exactly: value) {
                String(integer)
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

/// One stretch of a message as it happened: text the assistant wrote, or a run
/// of consecutive tool calls between two stretches of text.
enum ChatMessageSegment: Identifiable, Equatable {
    case text(id: String, String)
    case tools(id: String, [ChatToolCall])

    var id: String {
        switch self {
        case .text(let id, _), .tools(let id, _):
            id
        }
    }
}

extension ChatMessage {
    /// The message's text and tool calls in the order they happened.
    ///
    /// Each call records how much of the text came before it (`textOffset`);
    /// splitting the text there puts every call between the text written
    /// before it and the text written after it. Whitespace between two calls
    /// is dropped so it does not split their group in two.
    var segments: [ChatMessageSegment] {
        let length = text.utf16.count
        var segments: [ChatMessageSegment] = []
        var consumed = 0
        var pendingCalls: [ChatToolCall] = []

        func flushCalls() {
            guard let first = pendingCalls.first else {
                return
            }
            segments.append(.tools(id: "\(id)-tools-\(first.id)", pendingCalls))
            pendingCalls = []
        }

        func takeText(upTo end: Int) {
            guard end > consumed else {
                return
            }
            let start = String.Index(utf16Offset: consumed, in: text)
            let stop = String.Index(utf16Offset: end, in: text)
            // Newlines at either end only separated this stretch from a call
            // or a merged step; the bubble spaces segments itself.
            let stretch = String(text[start..<stop]).trimmingCharacters(in: .newlines)
            consumed = end
            guard !stretch.trimmingCharacters(in: .whitespaces).isEmpty else {
                return
            }
            flushCalls()
            segments.append(.text(id: "\(id)-text-\(segments.count)", stretch))
        }

        for call in toolCalls {
            takeText(upTo: min(max(call.textOffset ?? length, 0), length))
            pendingCalls.append(call)
        }
        flushCalls()
        takeText(upTo: length)
        return segments
    }
}

/// The collapsed header of a group of consecutive tool calls.
///
/// While work is in flight it names the latest call still running, so the
/// line reads as a live ticker. Once everything has finished it names the call
/// that mattered most — the latest that changed something, otherwise the
/// latest that succeeded — and counts the rest.
struct ChatToolGroupSummary: Equatable {
    let headline: String
    /// Progress while running, failures once finished.
    let detail: String?
    let isRunning: Bool
    let needsApproval: Bool
    /// The call the headline names.
    let subject: ChatToolCall?

    init(calls: [ChatToolCall]) {
        let unsuccessful = calls.filter { $0.status.didNotSucceed }.count
        let failureDetail = unsuccessful > 0 ? "\(unsuccessful) didn't finish" : nil

        if let awaiting = calls.last(where: { $0.status == .awaitingApproval }) {
            headline = "Needs your approval: \(awaiting.activePhrase)"
            detail = failureDetail
            isRunning = true
            needsApproval = true
            subject = awaiting
            return
        }
        if let running = calls.last(where: { $0.status == .running }) {
            let finished = calls.filter { !$0.status.isPending }.count
            headline = running.activePhrase
            detail = finished > 0 ? "\(finished) done" : nil
            isRunning = true
            needsApproval = false
            subject = running
            return
        }
        let key = calls.last { $0.status == .succeeded && $0.display?.changes == true }
            ?? calls.last { $0.status == .succeeded }
            ?? calls.last
        isRunning = false
        needsApproval = false
        detail = failureDetail
        subject = key
        guard let key else {
            headline = "No tool calls"
            return
        }
        let others = calls.count - 1
        headline = others > 0 ? "\(key.phrase) and \(others) more" : key.phrase
    }
}
