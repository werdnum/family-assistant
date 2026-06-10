import Foundation
import Observation

@MainActor
@Observable
final class ChatViewModel {
    var conversations: [ChatConversationSummary] = []
    var messages: [ChatMessage] = []
    var profiles: [ChatProfile] = []
    var defaultProfileID = "default_assistant"
    var selectedProfileID: String
    var conversationID: String?
    var conversationSelection: String?
    var draftText = ""
    var draftAttachments: [ChatAttachment] = []
    var pendingConfirmations: [ChatPendingConfirmation] = []
    var isLoadingConversations = false
    var isLoadingMessages = false
    var isLoadingProfiles = false
    var isStreaming = false
    var errorMessage: String?
    var liveUpdatesConnected = true
    var mobileShowsConversationList = false

    var canSendDraft: Bool {
        let prompt = draftText.trimmingCharacters(in: .whitespacesAndNewlines)
        let hasContent = !prompt.isEmpty || !draftAttachments.isEmpty
        let attachmentsReady = draftAttachments.allSatisfy { $0.uploadState == .uploaded }
        return hasContent && attachmentsReady
    }

    @ObservationIgnored private let apiClient: ChatAPIClient
    @ObservationIgnored private var streamTask: Task<Void, Never>?
    // Identifies the active send. A superseded (cancelled) streamTask resuming
    // across an await must not clobber the turn that replaced it, so its tail
    // work is gated on this token still matching.
    @ObservationIgnored private var currentStreamToken: UUID?
    @ObservationIgnored private var liveEventsTask: Task<Void, Never>?
    @ObservationIgnored private var pendingConfirmationsTask: Task<Void, Never>?
    @ObservationIgnored private var lastProcessedInitialPrompt: String?
    // Highest stream seq applied for the active conversation, threaded into the
    // follow subscribe's `ack_seq` and the `/ack` POST after a turn_ended so the
    // server suppresses the disconnect push for events this client has seen.
    // Reset whenever the active conversation changes.
    @ObservationIgnored private var highestAppliedSeq: Int?

    private enum Keys {
        static let lastConversationID = "lastConversationId"
        static let selectedProfileID = "selectedProfileId"
    }

    // Page size for incremental `after`-timestamp message loads. The server's
    // paginated path ignores `after` when limit=0, so a bounded page is needed;
    // `mergeNewMessages` pages through on `hasMoreAfter`.
    private static let messageDeltaPageSize = 100

    init(
        authManager: AuthManager,
        conversationID: String? = nil,
        initialPrompt: String? = nil
    ) {
        apiClient = ChatAPIClient(authManager: authManager)
        selectedProfileID = UserDefaults.standard.string(forKey: Keys.selectedProfileID) ?? "default_assistant"
        self.conversationID = conversationID
            ?? UserDefaults.standard.string(forKey: Keys.lastConversationID)
            ?? Self.generateConversationID()
        if let initialPrompt, !initialPrompt.isEmpty {
            self.conversationID = Self.generateConversationID()
            draftText = initialPrompt
        }
        conversationSelection = self.conversationID
        persistConversationID()
    }

    deinit {
        streamTask?.cancel()
        liveEventsTask?.cancel()
        pendingConfirmationsTask?.cancel()
    }

    func bootstrap(initialPrompt: String? = nil) async {
        await loadProfiles()
        await refreshConversations()
        if let conversationID {
            await selectConversation(conversationID, shouldLoadMessages: true)
        }
        startPendingConfirmationsPolling()
        if let initialPrompt, shouldProcessInitialPrompt(initialPrompt) {
            lastProcessedInitialPrompt = initialPrompt
            draftText = initialPrompt
            await sendDraft()
        } else if shouldProcessInitialPrompt(draftText) {
            lastProcessedInitialPrompt = draftText
            await sendDraft()
        }
    }

    func applyRoute(conversationID: String?, initialPrompt: String?) async {
        if let initialPrompt, shouldProcessInitialPrompt(initialPrompt) {
            lastProcessedInitialPrompt = initialPrompt
            startNewConversation()
            draftText = initialPrompt
            await sendDraft()
            return
        }
        guard let conversationID else {
            return
        }
        if conversationID != self.conversationID {
            await selectConversation(conversationID, shouldLoadMessages: true)
        } else if conversationSelection != conversationID {
            // The active conversation is still loaded, but the user navigated
            // back to the list so the selection was cleared. Restore it so a
            // deep link to the same thread reopens it instead of leaving the
            // user stranded on the conversation list.
            conversationSelection = conversationID
        }
    }

    func refreshConversations() async {
        isLoadingConversations = true
        do {
            conversations = try await apiClient.listConversations()
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoadingConversations = false
    }

    /// Refresh only the most recent page of conversation summaries.
    ///
    /// Used after a turn or live event: the changed conversation surfaces on the
    /// first (most-recent-first) page, so paging the entire history each time is
    /// wasteful. Merges the page over the held list, preserving older summaries
    /// the page didn't include.
    private func refreshRecentConversations() async {
        do {
            let recent = try await apiClient.listRecentConversations()
            let recentIDs = Set(recent.map(\.conversationID))
            let untouched = conversations.filter { !recentIDs.contains($0.conversationID) }
            conversations = recent + untouched
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Drives the conversation list selection binding in the sidebar.
    ///
    /// In compact width, `NavigationSplitView` writes `nil` here when the user
    /// taps Back out of a conversation. Honoring that nil clears the row
    /// highlight so the same conversation can be reopened with a single tap.
    /// Swallowing it (the previous behavior) left the row stuck selected, and
    /// because the selection value never changed, tapping it again was a no-op.
    func updateSelection(_ id: String?) {
        conversationSelection = id
        guard let id else {
            return
        }
        Task { await selectConversation(id) }
    }

    func selectConversation(_ id: String, shouldLoadMessages: Bool = true) async {
        cancelStream()
        highestAppliedSeq = nil
        conversationID = id
        conversationSelection = id
        persistConversationID()
        mobileShowsConversationList = false
        startLiveEvents()
        if shouldLoadMessages {
            await loadMessages(conversationID: id)
        }
    }

    func startNewConversation() {
        cancelStream()
        highestAppliedSeq = nil
        conversationID = Self.generateConversationID()
        conversationSelection = conversationID
        messages = []
        draftAttachments = []
        mobileShowsConversationList = false
        persistConversationID()
        startLiveEvents()
    }

    func changeProfile(to profileID: String) {
        guard selectedProfileID != profileID else {
            return
        }
        selectedProfileID = profileID
        UserDefaults.standard.set(profileID, forKey: Keys.selectedProfileID)
        startNewConversation()
    }

    func loadMessages(conversationID: String? = nil) async {
        guard let id = conversationID ?? self.conversationID else {
            return
        }
        isLoadingMessages = true
        do {
            let backendMessages = try await apiClient.getMessages(conversationID: id)
            messages = Self.renderMessages(from: backendMessages)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoadingMessages = false
    }

    /// Reconcile the held thread with newly persisted messages without refetching
    /// the entire history. Fetches only messages newer than the latest persisted
    /// one held, drops any local optimistic placeholders, then appends the delta
    /// de-duped by id. Falls back to a full load when nothing persisted is held
    /// yet (e.g. the very first turn in a conversation).
    private func mergeNewMessages(conversationID id: String) async {
        guard let after = latestPersistedTimestamp() else {
            await loadMessages(conversationID: id)
            return
        }
        do {
            let delta = try await fetchMessages(conversationID: id, after: after)
            guard !delta.isEmpty else {
                errorMessage = nil
                return
            }
            let rendered = Self.renderMessages(from: delta)
            // Drop optimistic local placeholders now that persisted copies exist,
            // then append any rendered messages not already present.
            var merged = messages.filter { !$0.id.hasPrefix("local_") }
            let existingIDs = Set(merged.map(\.id))
            merged.append(contentsOf: rendered.filter { !existingIDs.contains($0.id) })
            messages = merged
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Page through all persisted messages newer than `after`.
    private func fetchMessages(conversationID id: String, after: Date) async throws -> [ChatBackendMessage] {
        var collected: [ChatBackendMessage] = []
        var cursor = after
        while true {
            let page = try await apiClient.getMessagesPage(
                conversationID: id,
                after: cursor,
                limit: Self.messageDeltaPageSize
            )
            collected.append(contentsOf: page.messages)
            guard page.hasMoreAfter, let last = page.messages.last else {
                return collected
            }
            cursor = last.timestamp
        }
    }

    /// Timestamp of the newest persisted (server-backed) message currently held,
    /// or nil if only local placeholders are present.
    private func latestPersistedTimestamp() -> Date? {
        messages.filter { $0.id.hasPrefix("msg_") }.map(\.createdAt).max()
    }

    private func recordAppliedSeq(_ seq: Int) {
        if let current = highestAppliedSeq {
            highestAppliedSeq = max(current, seq)
        } else {
            highestAppliedSeq = seq
        }
    }

    private func removeLocalAssistantPlaceholder(_ assistantMessageID: String) {
        messages.removeAll { $0.id == assistantMessageID }
    }

    func loadProfiles() async {
        isLoadingProfiles = true
        do {
            let response = try await apiClient.listProfiles()
            defaultProfileID = response.defaultProfileID
            profiles = response.profiles.filter { !$0.delegationOnly }
            if !profiles.contains(where: { $0.id == selectedProfileID }) {
                selectedProfileID = response.defaultProfileID
                UserDefaults.standard.set(selectedProfileID, forKey: Keys.selectedProfileID)
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoadingProfiles = false
    }

    func sendDraft() async {
        let prompt = draftText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty || !draftAttachments.isEmpty else {
            return
        }
        guard draftAttachments.allSatisfy({ $0.uploadState != .uploading }) else {
            errorMessage = "Wait for attachments to finish uploading before sending."
            return
        }
        guard draftAttachments.allSatisfy({ $0.uploadState == .uploaded }) else {
            errorMessage = "Remove failed attachments before sending."
            return
        }
        guard let id = conversationID else {
            startNewConversation()
            return await sendDraft()
        }

        cancelStream()

        let uploadedAttachments = draftAttachments.filter { $0.uploadState == .uploaded }
        let userMessage = ChatMessage(
            id: "local_user_\(UUID().uuidString)",
            role: .user,
            text: prompt,
            createdAt: Date(),
            toolCalls: [],
            attachments: uploadedAttachments,
            isLoading: false,
            status: .complete,
            processingProfileID: selectedProfileID,
            errorTraceback: nil
        )
        let assistantMessageID = "local_assistant_\(UUID().uuidString)"
        let assistantMessage = ChatMessage(
            id: assistantMessageID,
            role: .assistant,
            text: "",
            createdAt: Date(),
            toolCalls: [],
            attachments: [],
            isLoading: true,
            status: .running,
            processingProfileID: selectedProfileID,
            errorTraceback: nil
        )
        messages.append(userMessage)
        messages.append(assistantMessage)
        draftText = ""
        draftAttachments = []
        isStreaming = true
        persistConversationID()

        let turnID = UUID().uuidString
        let streamToken = UUID()
        currentStreamToken = streamToken
        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                let turnStream = try await apiClient.streamMessage(
                    turnID: turnID,
                    prompt: prompt,
                    conversationID: id,
                    profileID: selectedProfileID,
                    attachments: uploadedAttachments
                )
                guard let events = turnStream.events else {
                    // already_complete: the retried turn finished durably but is
                    // not replayable from the hub. Don't open a stream; reload
                    // persisted history to surface the saved reply.
                    guard !Task.isCancelled, currentStreamToken == streamToken else {
                        return
                    }
                    removeLocalAssistantPlaceholder(assistantMessageID)
                    await refreshRecentConversations()
                    await loadMessages(conversationID: id)
                    if currentStreamToken == streamToken {
                        isStreaming = false
                        streamTask = nil
                    }
                    return
                }
                var lastSeq: Int?
                for try await event in events {
                    if Task.isCancelled {
                        break
                    }
                    // The conversation stream carries every turn's events. In
                    // this send-and-watch flow only apply events for the turn we
                    // started; ignore a turn started concurrently elsewhere in
                    // the same conversation. Connection-level events carry no
                    // turn id and fall through.
                    if let eventTurnID = event.turnID, eventTurnID != turnID {
                        continue
                    }
                    if let seq = event.seq {
                        lastSeq = seq
                        recordAppliedSeq(seq)
                    }
                    apply(streamEvent: event, assistantMessageID: assistantMessageID)
                    if event.type == .turnEnded {
                        break
                    }
                }
                // If a newer send (or conversation switch) superseded this task,
                // stop here: the post-completion reloads and shared-state resets
                // below belong to the turn that replaced us.
                guard !Task.isCancelled, currentStreamToken == streamToken else {
                    return
                }
                // Explicitly acknowledge the highest received seq so the server
                // suppresses the disconnect push for a reply we actually saw.
                // Fire-and-forget so UI completion isn't blocked on the ack.
                if let lastSeq {
                    let ackClient = apiClient
                    Task { try? await ackClient.acknowledge(conversationID: id, ackSeq: lastSeq) }
                }
                completeStream(assistantMessageID: assistantMessageID)
                await refreshRecentConversations()
                await mergeNewMessages(conversationID: id)
            } catch is CancellationError {
                markStreamStopped(assistantMessageID: assistantMessageID)
            } catch {
                appendStreamError(error.localizedDescription, assistantMessageID: assistantMessageID)
            }
            // Only the still-current send resets shared streaming state; a
            // superseded task must not nil out the new turn's streamTask.
            if currentStreamToken == streamToken {
                isStreaming = false
                streamTask = nil
            }
        }
    }

    func cancelStream() {
        guard streamTask != nil || isStreaming else {
            return
        }
        streamTask?.cancel()
        streamTask = nil
        isStreaming = false
        if let index = messages.lastIndex(where: { $0.role == .assistant && $0.status == .running }) {
            messages[index].isLoading = false
            messages[index].status = .complete
            if messages[index].text.isEmpty {
                messages[index].text = "Response stopped."
            }
        }
    }

    func addImageData(
        _ data: Data,
        filename: String = "\(UUID().uuidString).jpg",
        mimeType: String = "image/jpeg"
    ) async {
        do {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(filename)
            try data.write(to: url)
            await addAttachment(fileURL: url, mimeType: mimeType, displayName: filename)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func reportAttachmentImportError(_ message: String) {
        errorMessage = message
    }

    func addAttachment(fileURL: URL) async {
        let scoped = fileURL.startAccessingSecurityScopedResource()
        defer {
            if scoped {
                fileURL.stopAccessingSecurityScopedResource()
            }
        }
        let mimeType = apiClient.mimeType(for: fileURL)
        await addAttachment(fileURL: fileURL, mimeType: mimeType, displayName: fileURL.lastPathComponent)
    }

    func removeDraftAttachment(_ attachment: ChatAttachment) async {
        guard attachment.uploadState != .uploading else {
            errorMessage = "Wait for attachment upload to finish before removing."
            return
        }
        if let attachmentID = attachment.attachmentID {
            do {
                try await apiClient.deleteAttachment(attachmentID: attachmentID)
            } catch {
                errorMessage = "Could not remove attachment. \(error.localizedDescription)"
                return
            }
        }
        draftAttachments.removeAll { $0.id == attachment.id }
    }

    func confirm(_ confirmation: ChatPendingConfirmation, approved: Bool) async {
        do {
            try await apiClient.confirmTool(
                requestID: confirmation.requestID,
                conversationID: conversationID,
                approved: approved
            )
            updateToolConfirmation(toolCallID: confirmation.toolCallID, approved: approved)
            pendingConfirmations.removeAll { $0.requestID == confirmation.requestID }
        } catch {
            if let index = pendingConfirmations.firstIndex(where: { $0.requestID == confirmation.requestID }) {
                pendingConfirmations[index].errorMessage = error.localizedDescription
            }
        }
    }

    func downloadAttachment(_ attachment: ChatAttachment) async throws -> URL {
        guard let contentURL = attachment.contentURL else {
            throw ChatAPIError.validation("Attachment does not have a download URL.")
        }
        let (data, _) = try await apiClient.downloadAttachment(path: contentURL)
        let destination = FileManager.default.temporaryDirectory.appendingPathComponent(attachment.name)
        try data.write(to: destination, options: .atomic)
        return destination
    }

    func downloadAttachmentForSharing(_ attachment: ChatAttachment) async -> URL? {
        do {
            return try await downloadAttachment(attachment)
        } catch {
            errorMessage = "Could not download attachment. \(error.localizedDescription)"
            return nil
        }
    }

    func authenticatedImageData(for attachment: ChatAttachment) async throws -> Data {
        guard let contentURL = attachment.contentURL else {
            throw ChatAPIError.validation("Attachment does not have an image URL.")
        }
        return try await apiClient.downloadAttachment(path: contentURL).0
    }

    private func addAttachment(fileURL: URL, mimeType: String, displayName: String) async {
        var attachment = ChatAttachment(
            id: UUID().uuidString,
            attachmentID: nil,
            type: ChatAttachmentType.from(mimeType: mimeType),
            name: displayName,
            contentURL: nil,
            mimeType: mimeType,
            size: (try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize),
            localFileURL: fileURL,
            uploadState: .uploading,
            errorMessage: nil
        )
        draftAttachments.append(attachment)
        do {
            let upload = try await apiClient.uploadAttachment(fileURL: fileURL, mimeType: mimeType)
            attachment.attachmentID = upload.attachmentID
            attachment.contentURL = upload.url
            attachment.name = upload.filename
            attachment.mimeType = upload.contentType
            attachment.size = upload.size
            attachment.uploadState = .uploaded
        } catch {
            attachment.uploadState = .failed
            attachment.errorMessage = error.localizedDescription
        }
        if let index = draftAttachments.firstIndex(where: { $0.id == attachment.id }) {
            draftAttachments[index] = attachment
        }
    }

    private func startPendingConfirmationsPolling() {
        pendingConfirmationsTask?.cancel()
        pendingConfirmationsTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.loadPendingConfirmations()
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }

    private func loadPendingConfirmations() async {
        do {
            pendingConfirmations = try await apiClient.listPendingConfirmations()
            errorMessage = nil
        } catch {
            errorMessage = "Could not load pending approvals. \(error.localizedDescription)"
        }
    }

    func reconnectLiveUpdates() async {
        liveUpdatesConnected = true
        startLiveEvents()
    }

    private func startLiveEvents() {
        liveEventsTask?.cancel()
        guard let conversationID else {
            return
        }
        // Capture the API client (a value type whose only reference is the
        // AuthManager, never the view model) and the subscribe parameters up
        // front. The follow loop below never binds a strong `self` across its
        // awaits — every mutation goes through a weak `self?` hop — so releasing
        // the view model (e.g. on logout) lets `deinit` run, which cancels this
        // task and tears down the open SSE connection instead of stranding it.
        let client = apiClient
        let ackSeq = highestAppliedSeq ?? -1
        liveEventsTask = Task { [weak self] in
            do {
                let stream = try await client.connectEvents(
                    conversationID: conversationID,
                    ackSeq: ackSeq,
                    // Follow streams only reload persisted history on these; the
                    // token firehose is ignored, so don't ask for it. Lifecycle
                    // frames (turn_ended, heartbeat, stream_dropped) are always
                    // delivered regardless of this filter.
                    eventTypes: ["message", "turn_ended"]
                )
                guard let self else { return }
                await self.handleLiveReconnect(conversationID: conversationID)
                for try await event in stream {
                    if Task.isCancelled {
                        break
                    }
                    // Re-acquire `self` weakly each iteration so the view model
                    // is not retained between events across the indefinite loop.
                    guard let self else { break }
                    let shouldContinue = await self.handleLiveEvent(
                        event,
                        conversationID: conversationID,
                        client: client
                    )
                    if !shouldContinue {
                        break
                    }
                }
                self?.markLiveUpdatesDisconnectedIfActive()
            } catch {
                self?.markLiveUpdatesDisconnectedIfActive()
            }
        }
    }

    private func handleLiveReconnect(conversationID: String) async {
        liveUpdatesConnected = true
        // Reload on (re)connect: the stream tails from the live head, so a
        // reconnect after a drop resumes at the new head and misses events
        // published while offline. Message content always comes from persisted
        // history, so a reload closes that gap.
        if !isStreaming {
            await mergeNewMessages(conversationID: conversationID)
            await refreshRecentConversations()
        }
    }

    /// Handle one follow-stream event. Returns false when the loop should stop.
    private func handleLiveEvent(
        _ event: ChatStreamEvent,
        conversationID: String,
        client: ChatAPIClient
    ) async -> Bool {
        if let seq = event.seq {
            recordAppliedSeq(seq)
        }
        switch event.type {
        case .turnEnded, .message:
            // A turn finished (possibly started on another device); refresh
            // persisted history unless this device is actively streaming it.
            if !isStreaming {
                await mergeNewMessages(conversationID: conversationID)
                await refreshRecentConversations()
            }
            // Acknowledge the turn_ended seq so the server marks the reply
            // delivered and suppresses the disconnect push. Fire-and-forget.
            if event.type == .turnEnded, let seq = event.seq {
                Task { try? await client.acknowledge(conversationID: conversationID, ackSeq: seq) }
            }
        case .connected:
            liveUpdatesConnected = true
        case .heartbeat:
            break
        default:
            break
        }
        return true
    }

    private func markLiveUpdatesDisconnectedIfActive() {
        if !Task.isCancelled {
            liveUpdatesConnected = false
        }
    }

    private func apply(streamEvent event: ChatStreamEvent, assistantMessageID: String) {
        guard let index = messages.firstIndex(where: { $0.id == assistantMessageID }) else {
            return
        }

        switch event.type {
        case .text:
            messages[index].text += event.text ?? ""
            messages[index].isLoading = false
        case .toolCall:
            if let toolCall = event.toolCall {
                messages[index].toolCalls.append(
                    ChatToolCall(
                        id: toolCall.id,
                        name: toolCall.displayName,
                        argumentsText: toolCall.argumentsText,
                        resultText: nil,
                        attachments: [],
                        status: .running
                    )
                )
                messages[index].isLoading = false
            }
        case .toolResult:
            updateToolCall(
                toolCallID: event.toolCallID,
                resultText: event.toolResult,
                attachments: event.attachments,
                status: .complete
            )
        case .attachment:
            // Only assistant-response attachments belong on this bubble. A
            // `trigger` attachment is the user's own upload republished onto the
            // stream; it already renders on the user message, so ignore it here.
            // Absent source defaults to `.response` (backend always sets it).
            if event.attachmentSource == .response {
                messages[index].attachments.append(contentsOf: event.attachments)
            }
        case .toolConfirmationRequest:
            if let confirmation = event.confirmation {
                upsertPendingConfirmation(confirmation)
                updateToolCall(
                    toolCallID: confirmation.toolCallID,
                    resultText: nil,
                    attachments: [],
                    status: .awaitingApproval
                )
            }
        case .toolConfirmationResult:
            if let result = event.confirmationResult {
                let toolCallID = pendingConfirmations.first { $0.requestID == result.requestID }?.toolCallID
                updateToolConfirmation(toolCallID: toolCallID, approved: result.approved)
                pendingConfirmations.removeAll { $0.requestID == result.requestID }
            }
        case .error:
            appendStreamError(event.errorMessage ?? "An error occurred.", assistantMessageID: assistantMessageID)
        case .turnEnded:
            messages[index].isLoading = false
            messages[index].status = .complete
        case .turnStarted, .connected, .message, .heartbeat, .streamDropped:
            break
        }
    }

    private func completeStream(assistantMessageID: String) {
        if let index = messages.firstIndex(where: { $0.id == assistantMessageID }) {
            messages[index].isLoading = false
            messages[index].status = messages[index].status == .failed ? .failed : .complete
            if messages[index].text.isEmpty && messages[index].toolCalls.isEmpty {
                messages[index].text = "Done."
            }
        }
    }

    private func markStreamStopped(assistantMessageID: String) {
        if let index = messages.firstIndex(where: { $0.id == assistantMessageID }) {
            messages[index].isLoading = false
            messages[index].status = .complete
            if messages[index].text.isEmpty {
                messages[index].text = "Response stopped."
            }
        }
    }

    private func appendStreamError(_ message: String, assistantMessageID: String) {
        if let index = messages.firstIndex(where: { $0.id == assistantMessageID }) {
            messages[index].isLoading = false
            messages[index].status = .failed
            if messages[index].text.isEmpty {
                messages[index].text = "Sorry, I encountered an error processing your message."
            }
            messages[index].text += "\n\n\(message)"
        }
        errorMessage = message
    }

    private func updateToolCall(
        toolCallID: String?,
        resultText: String?,
        attachments: [ChatAttachment],
        status: ChatToolStatus
    ) {
        guard let toolCallID else {
            return
        }
        for messageIndex in messages.indices {
            if let toolIndex = messages[messageIndex].toolCalls.firstIndex(where: { $0.id == toolCallID }) {
                if let resultText {
                    messages[messageIndex].toolCalls[toolIndex].resultText = resultText
                }
                if !attachments.isEmpty {
                    messages[messageIndex].toolCalls[toolIndex].attachments = attachments
                }
                messages[messageIndex].toolCalls[toolIndex].status = status
            }
        }
    }

    private func updateToolConfirmation(toolCallID: String?, approved: Bool) {
        let status: ChatToolStatus = approved ? .approved : .rejected
        updateToolCall(toolCallID: toolCallID, resultText: nil, attachments: [], status: status)
    }

    private func upsertPendingConfirmation(_ confirmation: ChatPendingConfirmation) {
        if let index = pendingConfirmations.firstIndex(where: { $0.requestID == confirmation.requestID }) {
            pendingConfirmations[index] = confirmation
        } else {
            pendingConfirmations.append(confirmation)
        }
    }

    private func persistConversationID() {
        if let conversationID {
            UserDefaults.standard.set(conversationID, forKey: Keys.lastConversationID)
        }
    }

    static func generateConversationID() -> String {
        "\(ChatConstants.conversationPrefix)\(UUID().uuidString)"
    }

    private func shouldProcessInitialPrompt(_ prompt: String?) -> Bool {
        guard let prompt, !prompt.isEmpty else {
            return false
        }
        return prompt != lastProcessedInitialPrompt
    }

    nonisolated static func renderMessages(from backendMessages: [ChatBackendMessage]) -> [ChatMessage] {
        var toolResults: [String: (String, [ChatAttachment])] = [:]
        for message in backendMessages where message.role == .tool {
            if let toolCallID = message.toolCallID {
                toolResults[toolCallID] = (message.text, message.attachments.map(\.chatAttachment))
            }
        }

        return backendMessages.compactMap { backend in
            guard backend.role != .tool else {
                return nil
            }

            let toolCalls = backend.toolCalls.map { toolCall in
                let result = toolResults[toolCall.id]
                return ChatToolCall(
                    id: toolCall.id,
                    name: toolCall.displayName,
                    argumentsText: toolCall.argumentsText,
                    resultText: result?.0,
                    attachments: result?.1 ?? [],
                    status: result == nil ? .running : .complete
                )
            }

            var attachments = backend.attachments.map(\.chatAttachment)
            if let metadataAttachments = metadataAttachments(from: backend.metadata) {
                attachments.append(contentsOf: metadataAttachments)
            }

            let role = backend.role == .error ? ChatMessageRole.assistant : backend.role
            let text = backend.role == .error && backend.text.isEmpty
                ? "An error occurred while processing this message."
                : backend.text
            return ChatMessage(
                id: "msg_\(backend.internalID)",
                role: role,
                text: text,
                createdAt: backend.timestamp,
                toolCalls: toolCalls,
                attachments: attachments,
                isLoading: false,
                status: backend.errorTraceback == nil ? .complete : .failed,
                processingProfileID: backend.processingProfileID,
                errorTraceback: backend.errorTraceback
            )
        }
    }

    nonisolated private static func metadataAttachments(from metadata: [String: JSONValue]?) -> [ChatAttachment]? {
        guard let attachmentsValue = metadata?["attachments"],
              case .array(let values) = attachmentsValue
        else {
            return nil
        }
        return values.compactMap { value in
            guard let data = value.jsonString.data(using: .utf8),
                  let backend = try? JSONDecoder.chatDecoder.decode(ChatBackendAttachment.self, from: data)
            else {
                return nil
            }
            return backend.chatAttachment
        }
    }
}
