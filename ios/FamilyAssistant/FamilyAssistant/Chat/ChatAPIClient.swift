import Foundation
import UniformTypeIdentifiers

@MainActor
struct ChatAPIClient {
    let authManager: AuthManager
    var urlSession: URLSession = .shared
    private static let conversationPageSize = 100
    private static let iso8601Formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    func listConversations() async throws -> [ChatConversationSummary] {
        var conversations: [ChatConversationSummary] = []
        var offset = 0

        while true {
            let response = try await listConversationPage(limit: Self.conversationPageSize, offset: offset)
            conversations.append(contentsOf: response.conversations)

            if response.conversations.isEmpty || conversations.count >= response.count {
                return conversations
            }
            offset += response.conversations.count
        }
    }

    /// Fetch only the most recent page of conversation summaries.
    ///
    /// Used for incremental refreshes after a turn or live event: the list is
    /// ordered most-recent-first, so the conversation that just changed is on
    /// the first page. Paging the entire history on every event is wasteful.
    func listRecentConversations() async throws -> [ChatConversationSummary] {
        try await listConversationPage(limit: Self.conversationPageSize, offset: 0).conversations
    }

    private func listConversationPage(limit: Int, offset: Int) async throws -> ChatConversationListResponse {
        let (data, response) = try await authorizedGETWithAuthRetry(
            url: conversationListURL(limit: limit, offset: offset)
        )
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatConversationListResponse.self, from: data)
    }

    /// Load a conversation's full persisted history (oldest → newest) together
    /// with the server-reported `active_turns`, so the caller can tail-attach to a
    /// turn already running server-side (e.g. started on another device).
    func getMessages(conversationID: String) async throws -> ChatConversationMessagesResponse {
        try await getMessagesPage(conversationID: conversationID, after: nil, limit: 0)
    }

    /// Load only messages newer than `after` (ISO-8601 timestamp).
    ///
    /// Incremental loads after a turn or live event pass the timestamp of the
    /// newest message already held, so only the delta crosses the wire instead
    /// of the entire history. The server's paginated path honors `after` only
    /// with a non-zero limit (limit=0 falls back to returning everything), so a
    /// page size is required; the caller pages on `hasMoreAfter`.
    func getMessagesPage(
        conversationID: String,
        after: Date?,
        limit: Int
    ) async throws -> ChatConversationMessagesResponse {
        let encodedID = Self.encodedPathComponent(conversationID)
        var components = URLComponents(
            url: try apiURL("/api/v1/chat/conversations/\(encodedID)/messages"),
            resolvingAgainstBaseURL: false
        )
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let after {
            queryItems.append(URLQueryItem(name: "after", value: Self.iso8601Formatter.string(from: after)))
        }
        components?.queryItems = queryItems
        guard let url = components?.url else {
            throw ChatAPIError.invalidServerURL
        }
        let (data, response) = try await authorizedGETWithAuthRetry(url: url)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatConversationMessagesResponse.self, from: data)
    }

    func listProfiles() async throws -> ChatProfilesResponse {
        let (data, response) = try await authorizedGETWithAuthRetry(url: apiURL("/api/v1/profiles"))
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatProfilesResponse.self, from: data)
    }

    /// Mint a single-use Gemini Live ephemeral token for native voice mode.
    ///
    /// POSTs `/api/gemini/ephemeral-token`. The returned token authorizes a direct
    /// WebSocket to Gemini and carries the context-injected system instruction,
    /// confirmation-filtered tools, and live session config.
    func fetchEphemeralToken(profileID: String?) async throws -> EphemeralToken {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/gemini/ephemeral-token"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(EphemeralTokenRequestBody(profileID: profileID))
        let (data, response) = try await urlSession.data(for: request)
        do {
            try validate(response: response, data: data)
        } catch let ChatAPIError.server(statusCode, detail, _) where detail == nil {
            throw ChatAPIError.validation("Voice session request failed with status \(statusCode).")
        }
        return try JSONDecoder.chatDecoder.decode(EphemeralToken.self, from: data)
    }

    /// Persist a completed voice session as its own conversation.
    ///
    /// POSTs `/api/v1/chat/voice-sessions` with the accumulated transcript turns.
    /// Returns the conversation id the backend stored them under.
    @discardableResult
    func saveVoiceSession(turns: [VoiceTranscriptEntry], conversationID: String?) async throws -> String {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/voice-sessions"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            VoiceSessionBody(
                conversationID: conversationID,
                turns: turns.map { VoiceSessionTurnBody(role: $0.speaker.rawValue, text: $0.text) }
            )
        )
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(VoiceSessionResponseBody.self, from: data).conversationID
    }

    /// Execute a tool by name on behalf of a Gemini voice function call.
    ///
    /// POSTs `/api/tools/execute/{name}` with the session's profile and taint state.
    /// The response body is returned for both successful and rejected calls so the
    /// voice runner can retain any taint accumulated before an error.
    func executeTool(
        name: String,
        arguments: JSONValue,
        profileID: String?,
        taintMetadata: JSONValue
    ) async throws -> JSONValue {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/tools/execute/\(Self.encodedPathComponent(name))"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let argumentsObject: JSONValue = {
            if case .object = arguments { return arguments }
            return .object([:])
        }()
        request.httpBody = try JSONEncoder().encode(
            ToolExecuteBody(
                arguments: argumentsObject,
                profileID: profileID,
                taintMetadata: taintMetadata
            )
        )
        let (data, response) = try await urlSession.data(for: request)
        guard response is HTTPURLResponse else {
            throw ChatAPIError.invalidResponse
        }
        return try JSONDecoder.chatDecoder.decode(JSONValue.self, from: data)
    }

    /// Sends a prompt and waits for the assistant's full reply.
    ///
    /// Unlike the two-step ``startTurn(turnID:prompt:conversationID:profileID:attachments:)``
    /// plus ``subscribeToTurn(conversationID:fromSeq:ackSeq:)`` flow, this uses the
    /// non-streaming `/send_message` endpoint, which suits headless
    /// callers (App Intents / Siri) that present a single completed reply rather
    /// than a live token stream.
    func sendMessage(
        prompt: String,
        conversationID: String,
        profileID: String?
    ) async throws -> ChatSendResult {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/send_message"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.chatEncoder.encode(
            ChatSendMessageRequest(
                prompt: prompt,
                conversationID: conversationID,
                profileID: profileID,
                interfaceType: ChatConstants.interfaceType
            )
        )

        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        let decoded = try JSONDecoder.chatDecoder.decode(ChatSendMessageResponse.self, from: data)
        return ChatSendResult(reply: decoded.reply, conversationID: decoded.conversationID)
    }

    /// Start a chat turn. First step of the two-step resumable-streaming flow.
    ///
    /// POSTs `/api/v1/chat/turns` (idempotent on `turnID`: a retried POST returns
    /// the existing turn instead of starting a second producer, so a
    /// network-timed-out send is safe to retry). The returned ``ChatTurnStart``
    /// carries the seq to subscribe from and whether the turn was already durably
    /// complete — a retried turn found in the DB but no longer replayable from the
    /// hub, for which the caller reloads persisted history instead of subscribing.
    func startTurn(
        turnID: String,
        prompt: String,
        conversationID: String,
        profileID: String?,
        attachments: [ChatAttachment]
    ) async throws -> ChatTurnStart {
        var startRequest = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/turns"),
            method: "POST"
        )
        startRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        startRequest.httpBody = try JSONEncoder.chatEncoder.encode(
            ChatStreamRequest(
                turnID: turnID,
                prompt: prompt,
                conversationID: conversationID,
                profileID: profileID,
                interfaceType: ChatConstants.interfaceType,
                attachments: attachments.map(ChatStreamAttachment.init(attachment:))
            )
        )
        let (startData, startResponse) = try await urlSession.data(for: startRequest)
        if (startResponse as? HTTPURLResponse)?.statusCode == 409,
           let conflict = try? JSONDecoder.chatDecoder.decode(ChatTurnConflictResponse.self, from: startData) {
            throw ChatAPIError.turnAlreadyRunning(activeTurnID: conflict.detail.activeTurnID)
        }
        try validate(response: startResponse, data: startData)
        let turn = try JSONDecoder.chatDecoder.decode(ChatTurnResponse.self, from: startData)
        return ChatTurnStart(
            conversationID: turn.conversationID,
            firstSeq: turn.firstSeq,
            alreadyComplete: turn.alreadyComplete,
            incomplete: turn.incomplete
        )
    }

    /// Request server-side cancellation of a running turn.
    ///
    /// POSTs `/api/v1/chat/turns/{turnID}/cancel`. The server ends the turn as
    /// `cancelled` and rejects pending durable confirmations for that turn; the
    /// caller should keep the stream open and let the terminal SSE event settle
    /// the visible bubble.
    func cancelTurn(turnID: String, conversationID: String) async throws -> ChatTurnCancelResult {
        let encodedTurnID = Self.encodedPathComponent(turnID)
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/turns/\(encodedTurnID)/cancel"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.chatEncoder.encode(ChatTurnControlRequest(conversationID: conversationID))
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatTurnCancelResult.self, from: data)
    }

    /// Inject a steering message into a running turn.
    ///
    /// POSTs `/api/v1/chat/turns/{turnID}/steer`. A 409 response means the turn
    /// has already finished and the text should be sent as a normal follow-up.
    func steerTurn(turnID: String, conversationID: String, prompt: String) async throws -> ChatTurnSteerResult {
        let encodedTurnID = Self.encodedPathComponent(turnID)
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/turns/\(encodedTurnID)/steer"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.chatEncoder.encode(
            ChatTurnSteerRequest(conversationID: conversationID, prompt: prompt)
        )
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatTurnSteerResult.self, from: data)
    }

    /// Subscribe to (or resume) a turn's event stream. Second step of the
    /// resumable-streaming flow.
    ///
    /// Uses `follow=false`: the server drains the buffer from `fromSeq` and the
    /// in-flight turn through `turn_ended`, then closes. The send-and-watch flow
    /// calls this once with the turn's `first_seq`; after a `stream_dropped` or a
    /// transient connect failure it resumes by calling again with the highest seq
    /// already applied, so no events are replayed or missed.
    ///
    /// `ackSeq` marks everything through that seq delivered on (re)subscribe so
    /// the server suppresses the disconnect push for events already applied.
    func subscribeToTurn(
        conversationID: String,
        fromSeq: Int,
        ackSeq: Int? = nil
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        try await streamConversation(
            conversationID: conversationID,
            fromSeq: fromSeq,
            follow: false,
            ackSeq: ackSeq
        )
    }

    /// Connect to a conversation's event stream for live updates. With
    /// `follow=true` the connection stays open across turns so replies started
    /// from other devices/tabs surface here too.
    ///
    /// Defaults to `fromSeq = -1` (tail from the current head): a follow-only
    /// listener wants future events, not a replay, and subscribing from 0 would
    /// 410 once the conversation's hub buffer has rotated.
    func connectEvents(
        conversationID: String,
        fromSeq: Int = -1,
        ackSeq: Int? = nil,
        eventTypes: [String]? = nil
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        try await streamConversation(
            conversationID: conversationID,
            fromSeq: fromSeq,
            follow: true,
            ackSeq: ackSeq,
            eventTypes: eventTypes
        )
    }

    /// Connect to the account-global conversation-activity stream for live
    /// conversation-list updates.
    ///
    /// Emits a `ChatConversationActivity` whenever any conversation the caller
    /// owns changes (a turn starts/ends, a delegated/scheduled reply lands) —
    /// including conversations other than the one currently open, which the
    /// per-conversation follow stream never sees. The frame is advisory; the
    /// caller reacts by re-fetching the authoritative conversation list. Stays
    /// open with server heartbeats; the caller resubscribes on close/error.
    func connectActivityStream() async throws -> AsyncThrowingStream<ChatConversationActivity, Error> {
        let request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/activity/stream"),
            method: "GET"
        )
        // Validate the response status before returning the stream (see
        // `streamConversation` for why this matters to the reconnect backoff).
        let (bytes, response) = try await urlSession.bytes(for: request)
        try validate(response: response, data: Data())

        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    try await streamActivityEvents(bytes: bytes, continuation: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    private func streamActivityEvents(
        bytes: URLSession.AsyncBytes,
        continuation: AsyncThrowingStream<ChatConversationActivity, Error>.Continuation
    ) async throws {
        let parser = SSEParser()
        var pendingUTF8 = Data()
        for try await byte in bytes {
            pendingUTF8.append(byte)
            guard let chunk = String(data: pendingUTF8, encoding: .utf8) else {
                continue
            }
            pendingUTF8.removeAll(keepingCapacity: true)
            for event in parser.append(chunk) {
                if let activity = Self.activity(from: event) {
                    continuation.yield(activity)
                }
            }
        }
        if !pendingUTF8.isEmpty {
            for event in parser.append(String(decoding: pendingUTF8, as: UTF8.self)) {
                if let activity = Self.activity(from: event) {
                    continuation.yield(activity)
                }
            }
        }
        for event in parser.flush() {
            if let activity = Self.activity(from: event) {
                continuation.yield(activity)
            }
        }
    }

    /// Map a raw SSE frame to a `ChatConversationActivity`, ignoring the
    /// heartbeat/stream_dropped control frames (a closed stream surfaces as the
    /// AsyncThrowingStream finishing, which the caller treats as "reconnect").
    private static func activity(from event: ServerSentEvent) -> ChatConversationActivity? {
        guard event.event == "conversation_activity" else {
            return nil
        }
        guard let data = event.data.data(using: .utf8),
              let payload = try? JSONDecoder.chatDecoder.decode([String: JSONValue].self, from: data)
        else {
            return ChatConversationActivity(conversationID: nil, reason: nil)
        }
        return ChatConversationActivity(
            conversationID: payload["conversation_id"]?.stringValue,
            reason: payload["reason"]?.stringValue
        )
    }

    /// Acknowledge the highest received seq for a conversation so the server
    /// suppresses the "new reply" disconnect push. The server never treats
    /// writing the SSE chunk as delivery — only this explicit ack counts.
    func acknowledge(conversationID: String, ackSeq: Int) async throws {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/ack"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.chatEncoder.encode(
            ChatAckRequest(conversationID: conversationID, ackSeq: ackSeq)
        )
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
    }

    private func streamConversation(
        conversationID: String,
        fromSeq: Int,
        follow: Bool,
        ackSeq: Int? = nil,
        eventTypes: [String]? = nil
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        let encodedConversation = Self.encodedPathComponent(conversationID)
        var components = URLComponents(
            url: try apiURL("/api/v1/chat/conversations/\(encodedConversation)/stream"),
            resolvingAgainstBaseURL: false
        )
        var queryItems = [
            URLQueryItem(name: "from_seq", value: String(fromSeq)),
            URLQueryItem(name: "follow", value: follow ? "true" : "false"),
        ]
        if let ackSeq {
            // Mark everything through `ackSeq` delivered on (re)subscribe so the
            // server suppresses the disconnect push for events already applied.
            queryItems.append(URLQueryItem(name: "ack_seq", value: String(ackSeq)))
        }
        if let eventTypes, !eventTypes.isEmpty {
            // Lifecycle/control frames (turn_ended, heartbeat, stream_dropped)
            // are always delivered regardless of this filter.
            queryItems.append(URLQueryItem(name: "event_types", value: eventTypes.joined(separator: ",")))
        }
        components?.queryItems = queryItems
        guard let url = components?.url else {
            throw ChatAPIError.invalidServerURL
        }
        let request = try await authManager.authorizedRequest(url: url, method: "GET")

        // Establish the connection and validate the response status BEFORE
        // returning the stream. `URLSession.bytes(for:)` resolves once the
        // response headers arrive (the body still streams lazily via
        // AsyncBytes), so a connection failure or a non-2xx status throws here at
        // the call site rather than surfacing later during iteration. Callers
        // (notably the live-updates reconnect loop) rely on this to tell a real
        // connection apart from a stream object that will immediately error —
        // otherwise they would reset their backoff before the stream ever
        // succeeded and tight-loop against a down or erroring endpoint.
        let (bytes, response) = try await urlSession.bytes(for: request)
        try validate(response: response, data: Data())

        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let parser = SSEParser()
                    try await streamServerSentEvents(bytes: bytes, parser: parser, continuation: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    func listPendingConfirmations() async throws -> [ChatPendingConfirmation] {
        let (data, response) = try await authorizedGETWithAuthRetry(
            url: apiURL("/api/v1/chat/confirmations/pending")
        )
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatPendingConfirmationsResponse.self, from: data).confirmations
    }

    func fetchConfirmation(requestID: String) async throws -> ChatConfirmationDetail {
        let encodedID = Self.encodedPathComponent(requestID)
        let (data, response) = try await authorizedGETWithAuthRetry(
            url: apiURL("/api/v1/chat/confirmations/\(encodedID)")
        )
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatConfirmationDetail.self, from: data)
    }

    func confirmTool(requestID: String, conversationID: String?, approved: Bool) async throws {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/confirm_tool"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.chatEncoder.encode(
            ChatConfirmationActionRequest(
                requestID: requestID,
                approved: approved,
                conversationID: conversationID,
                approvingInterface: "ios"
            )
        )

        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        let result = try JSONDecoder.chatDecoder.decode(ChatConfirmationActionResponse.self, from: data)
        if !result.success {
            throw ChatAPIError.server(
                statusCode: 200,
                detail: result.message ?? "Confirmation request failed.",
                retryAfter: nil
            )
        }
    }

    func uploadAttachment(fileURL: URL, mimeType: String) async throws -> ChatUploadResponse {
        let values = try fileURL.resourceValues(forKeys: [.fileSizeKey, .nameKey])
        let fileSize = values.fileSize ?? 0
        guard fileSize <= ChatConstants.maxAttachmentSizeBytes else {
            throw ChatAPIError.validation("File size exceeds 100MB.")
        }
        guard ChatConstants.allowedAttachmentMIMETypes.contains(mimeType) else {
            throw ChatAPIError.validation("Unsupported file type: \(mimeType).")
        }

        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/attachments/upload"),
            method: "POST"
        )
        let boundary = "Boundary-\(UUID().uuidString)"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = try await Self.multipartBody(
            fileURL: fileURL,
            filename: values.name ?? fileURL.lastPathComponent,
            mimeType: mimeType,
            boundary: boundary
        )

        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatUploadResponse.self, from: data)
    }

    func deleteAttachment(attachmentID: String) async throws {
        let request = try await authManager.authorizedRequest(
            url: apiURL("/api/attachments/\(Self.encodedPathComponent(attachmentID))"),
            method: "DELETE"
        )
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
    }

    func downloadAttachment(path: String) async throws -> (Data, String?) {
        let request = try await authManager.authorizedRequest(url: apiURL(path), method: "GET")
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return (data, (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Type"))
    }

    func mimeType(for fileURL: URL) -> String {
        if let contentType = try? fileURL.resourceValues(forKeys: [.contentTypeKey]).contentType,
           let mimeType = contentType.preferredMIMEType
        {
            return mimeType
        }
        let ext = fileURL.pathExtension
        return UTType(filenameExtension: ext)?.preferredMIMEType ?? "application/octet-stream"
    }

    private func conversationListURL(limit: Int, offset: Int) throws -> URL {
        var components = URLComponents(url: try apiURL("/api/v1/chat/conversations"), resolvingAgainstBaseURL: false)
        components?.queryItems = [
            URLQueryItem(name: "interface_type", value: ChatConstants.interfaceType),
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset)),
        ]
        guard let url = components?.url else {
            throw ChatAPIError.invalidServerURL
        }
        return url
    }

    private func apiURL(_ path: String) throws -> URL {
        guard let baseURL = authManager.validatedServerURL(),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
        else {
            throw ChatAPIError.invalidServerURL
        }
        return url
    }

    /// Perform an IDEMPOTENT GET, transparently refreshing-and-retrying exactly
    /// once on a response-time 401.
    ///
    /// A token can pass the client's clock-based freshness check yet still be
    /// rejected at request time (server-side rotation, revocation, clock skew). For
    /// a GET that only reads — no turn is started, no state mutated — it is safe to
    /// force a single coalesced refresh and replay the request with the new token.
    /// The retry wraps the conversation list, the messages page, the profile list,
    /// and the pending-confirmations poll. It must only ever wrap idempotent reads:
    /// a POST/DELETE (startTurn, steer, stop, ack, confirm, attachment mutations) is
    /// NEVER routed here, so this path cannot double-send a turn.
    ///
    /// Propagation: a rejection surfaces as ``AuthError/noCredentials`` and the
    /// terminal ``AuthManager/authRequired`` state is latched (which drives the
    /// coordinator's `.authRequired` presentation). The view model suppresses the
    /// generic error modal for that error, so a persistent 401 never modals.
    private func authorizedGETWithAuthRetry(url: URL) async throws -> (Data, URLResponse) {
        // Capture the epoch before the first request so a logout/re-login that
        // completes while the refresh or retried GET is in flight cannot make a
        // stale rejection clear the newly issued credentials.
        let capturedEpoch = authManager.authEpoch
        let request = try await authManager.authorizedRequest(url: url, method: "GET")
        let (data, response) = try await urlSession.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 401 else {
            return (data, response)
        }

        let rejectedAccessToken: String? = request
            .value(forHTTPHeaderField: "Authorization")
            .flatMap { authorization -> String? in
                let bearerPrefix = "Bearer "
                guard authorization.hasPrefix(bearerPrefix) else {
                    return nil
                }
                return String(authorization.dropFirst(bearerPrefix.count))
            }

        do {
            try await authManager.refreshIfNeeded(
                force: true,
                ownerEpoch: capturedEpoch,
                rejectedAccessToken: rejectedAccessToken
            )
        } catch AuthError.authRejected, AuthError.noCredentials {
            authManager.markAuthRequiredIfCurrent(capturedEpoch: capturedEpoch)
            throw AuthError.noCredentials
        }

        let retryRequest = try await authManager.authorizedRequest(url: url, method: "GET")
        let (retryData, retryResponse) = try await urlSession.data(for: retryRequest)
        if (retryResponse as? HTTPURLResponse)?.statusCode == 401 {
            authManager.markAuthRequiredIfCurrent(capturedEpoch: capturedEpoch)
            throw AuthError.noCredentials
        }
        return (retryData, retryResponse)
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let httpResponse = response as? HTTPURLResponse else {
            throw ChatAPIError.invalidResponse
        }
        guard (200 ..< 300).contains(httpResponse.statusCode) else {
            let detail = try? JSONDecoder.chatDecoder.decode(ChatServerError.self, from: data).detail
            throw ChatAPIError.server(
                statusCode: httpResponse.statusCode,
                detail: detail,
                retryAfter: Self.parseRetryAfter(httpResponse)
            )
        }
    }

    /// Parse a `Retry-After` response header into seconds. Handles the delta-seconds
    /// form (`"30"`); an HTTP-date form is ignored (returns nil) so the caller falls
    /// back to its default backoff.
    private static func parseRetryAfter(_ response: HTTPURLResponse) -> TimeInterval? {
        guard let raw = response.value(forHTTPHeaderField: "Retry-After") else {
            return nil
        }
        return TimeInterval(raw.trimmingCharacters(in: .whitespaces))
    }

    private func streamServerSentEvents(
        bytes: URLSession.AsyncBytes,
        parser: SSEParser,
        continuation: AsyncThrowingStream<ChatStreamEvent, Error>.Continuation
    ) async throws {
        var pendingUTF8 = Data()
        for try await byte in bytes {
            pendingUTF8.append(byte)
            guard let chunk = String(data: pendingUTF8, encoding: .utf8) else {
                continue
            }
            pendingUTF8.removeAll(keepingCapacity: true)
            for event in parser.append(chunk) {
                continuation.yield(parser.decode(event))
            }
        }
        if !pendingUTF8.isEmpty {
            for event in parser.append(String(decoding: pendingUTF8, as: UTF8.self)) {
                continuation.yield(parser.decode(event))
            }
        }
        for event in parser.flush() {
            continuation.yield(parser.decode(event))
        }
    }

    private nonisolated static func multipartBody(
        fileURL: URL,
        filename: String,
        mimeType: String,
        boundary: String
    ) async throws -> Data {
        try await Task.detached(priority: .utility) {
            var data = Data()
            data.append("--\(boundary)\r\n")
            data.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n")
            data.append("Content-Type: \(mimeType)\r\n\r\n")
            data.append(try Data(contentsOf: fileURL))
            data.append("\r\n--\(boundary)--\r\n")
            return data
        }.value
    }

    private static func encodedPathComponent(_ value: String) -> String {
        var allowedCharacters = CharacterSet.urlPathAllowed
        allowedCharacters.remove(charactersIn: "/?#")
        return value.addingPercentEncoding(withAllowedCharacters: allowedCharacters) ?? value
    }
}

/// The completed result of a non-streaming chat send.
struct ChatSendResult: Equatable {
    let reply: String
    let conversationID: String
}

/// Result of starting a chat turn (POST `/api/v1/chat/turns`).
///
/// `firstSeq` is the seq to subscribe from. `alreadyComplete` is true for a
/// retried turn that finished durably but is no longer replayable from the hub;
/// in that case the caller reloads persisted history instead of subscribing.
struct ChatTurnStart {
    let conversationID: String
    let firstSeq: Int
    let alreadyComplete: Bool
    let incomplete: Bool
}

private struct EphemeralTokenRequestBody: Encodable {
    let profileID: String?

    enum CodingKeys: String, CodingKey {
        case profileID = "profile_id"
    }
}

private struct ToolExecuteBody: Encodable {
    let arguments: JSONValue
    let profileID: String?
    let taintMetadata: JSONValue

    enum CodingKeys: String, CodingKey {
        case arguments
        case profileID = "profile_id"
        case taintMetadata = "taint_metadata"
    }
}

private struct VoiceSessionTurnBody: Encodable {
    let role: String
    let text: String
}

private struct VoiceSessionBody: Encodable {
    let conversationID: String?
    let turns: [VoiceSessionTurnBody]

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case turns
    }
}

private struct VoiceSessionResponseBody: Decodable {
    let conversationID: String
    let messageCount: Int

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case messageCount = "message_count"
    }
}

private struct ChatSendMessageRequest: Encodable {
    let prompt: String
    let conversationID: String
    let profileID: String?
    let interfaceType: String

    enum CodingKeys: String, CodingKey {
        case prompt
        case conversationID = "conversation_id"
        case profileID = "profile_id"
        case interfaceType = "interface_type"
    }
}

private struct ChatSendMessageResponse: Decodable {
    let reply: String
    let conversationID: String

    enum CodingKeys: String, CodingKey {
        case reply
        case conversationID = "conversation_id"
    }
}

private struct ChatStreamRequest: Encodable {
    let turnID: String
    let prompt: String
    let conversationID: String
    let profileID: String?
    let interfaceType: String
    let attachments: [ChatStreamAttachment]?

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case prompt
        case conversationID = "conversation_id"
        case profileID = "profile_id"
        case interfaceType = "interface_type"
        case attachments
    }
}

private struct ChatTurnControlRequest: Encodable {
    let conversationID: String

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
    }
}

private struct ChatTurnSteerRequest: Encodable {
    let conversationID: String
    let prompt: String

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case prompt
    }
}

struct ChatTurnCancelResult: Decodable, Equatable {
    let turnID: String
    let conversationID: String
    let status: String
    let alreadyComplete: Bool

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case conversationID = "conversation_id"
        case status
        case alreadyComplete = "already_complete"
    }
}

struct ChatTurnSteerResult: Decodable, Equatable {
    let turnID: String
    let conversationID: String
    let accepted: Bool

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case conversationID = "conversation_id"
        case accepted
    }
}

private struct ChatTurnResponse: Decodable {
    let turnID: String
    let conversationID: String
    let firstSeq: Int
    let alreadyComplete: Bool
    let incomplete: Bool

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case conversationID = "conversation_id"
        case firstSeq = "first_seq"
        case alreadyComplete = "already_complete"
        case incomplete
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        turnID = try container.decode(String.self, forKey: .turnID)
        conversationID = try container.decode(String.self, forKey: .conversationID)
        firstSeq = try container.decode(Int.self, forKey: .firstSeq)
        // Durable-idempotency fallback: a turn found in the DB but not the hub
        // returns already_complete=true and is not replayable from the stream.
        alreadyComplete = try container.decodeIfPresent(Bool.self, forKey: .alreadyComplete) ?? false
        incomplete = try container.decodeIfPresent(Bool.self, forKey: .incomplete) ?? false
    }
}

private struct ChatAckRequest: Encodable {
    let conversationID: String
    let ackSeq: Int

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case ackSeq = "ack_seq"
    }
}

private struct ChatStreamAttachment: Encodable {
    let id: String
    let type: String
    let name: String
    let content: String

    init(attachment: ChatAttachment) {
        id = attachment.id
        type = attachment.type.rawValue
        name = attachment.name
        content = attachment.contentURL ?? ""
    }
}

private struct ChatConfirmationActionRequest: Encodable {
    let requestID: String
    let approved: Bool
    let conversationID: String?
    let approvingInterface: String

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case approved
        case conversationID = "conversation_id"
        case approvingInterface = "approving_interface"
    }
}

private struct ChatConfirmationActionResponse: Decodable {
    let success: Bool
    let message: String?
}

private struct ChatServerError: Decodable {
    let detail: String?
}

private struct ChatTurnConflictResponse: Decodable {
    let detail: Detail

    struct Detail: Decodable {
        let activeTurnID: String

        enum CodingKeys: String, CodingKey {
            case activeTurnID = "active_turn_id"
        }
    }
}

enum ChatAPIError: LocalizedError, Equatable {
    case invalidServerURL
    case invalidResponse
    case validation(String)
    /// Starting a second turn was refused because the conversation already has
    /// a running turn. The client can recover by steering `activeTurnID`.
    case turnAlreadyRunning(activeTurnID: String)
    /// A non-2xx HTTP response. `retryAfter` carries the parsed `Retry-After`
    /// header (seconds) when the server attached one — chiefly on a 429 — so the
    /// classifier can honor the server's backoff instead of a hard-coded default.
    case server(statusCode: Int, detail: String?, retryAfter: TimeInterval?)

    var errorDescription: String? {
        switch self {
        case .invalidServerURL:
            "Invalid server URL."
        case .invalidResponse:
            "The server returned an invalid response."
        case .validation(let message):
            message
        case .turnAlreadyRunning:
            "This conversation already has a running turn."
        case .server(let statusCode, let detail, _):
            if let detail, !detail.isEmpty {
                detail
            } else {
                "Chat request failed with status \(statusCode)."
            }
        }
    }
}

private extension Data {
    mutating func append(_ string: String) {
        append(Data(string.utf8))
    }
}
