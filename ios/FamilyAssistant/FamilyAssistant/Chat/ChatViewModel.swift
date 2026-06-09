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
    @ObservationIgnored private var liveEventsTask: Task<Void, Never>?
    @ObservationIgnored private var pendingConfirmationsTask: Task<Void, Never>?
    @ObservationIgnored private var lastProcessedInitialPrompt: String?

    private enum Keys {
        static let lastConversationID = "lastConversationId"
        static let selectedProfileID = "selectedProfileId"
    }

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
        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                let stream = try await apiClient.streamMessage(
                    turnID: turnID,
                    prompt: prompt,
                    conversationID: id,
                    profileID: selectedProfileID,
                    attachments: uploadedAttachments
                )
                var lastSeq: Int?
                for try await event in stream {
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
                    }
                    apply(streamEvent: event, assistantMessageID: assistantMessageID)
                    if event.type == .turnEnded || event.type == .end || event.type == .close {
                        break
                    }
                }
                // Explicitly acknowledge the highest received seq so the server
                // suppresses the disconnect push for a reply we actually saw.
                // Skip on cancellation (backgrounded mid-turn): there the push
                // is the intended delivery path. Fire-and-forget so UI
                // completion isn't blocked on the ack roundtrip.
                if let lastSeq, !Task.isCancelled {
                    let ackClient = apiClient
                    Task { try? await ackClient.acknowledge(conversationID: id, ackSeq: lastSeq) }
                }
                completeStream(assistantMessageID: assistantMessageID)
                await refreshConversations()
                await loadMessages(conversationID: id)
            } catch is CancellationError {
                markStreamStopped(assistantMessageID: assistantMessageID)
            } catch {
                appendStreamError(error.localizedDescription, assistantMessageID: assistantMessageID)
            }
            isStreaming = false
            streamTask = nil
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
        liveEventsTask = Task { [weak self] in
            guard let self else { return }
            do {
                let stream = try await apiClient.connectEvents(conversationID: conversationID)
                liveUpdatesConnected = true
                // Reload on (re)connect: the stream tails from the live head, so
                // a reconnect after a drop resumes at the new head and misses
                // events published while offline. Message content always comes
                // from persisted history, so a reload closes that gap.
                if !isStreaming {
                    await loadMessages(conversationID: conversationID)
                    await refreshConversations()
                }
                for try await event in stream {
                    if Task.isCancelled {
                        break
                    }
                    switch event.type {
                    case .turnEnded, .message:
                        // A turn finished (possibly started on another device);
                        // refresh persisted history unless this device is the
                        // one actively streaming the turn.
                        if !isStreaming {
                            await loadMessages(conversationID: conversationID)
                            await refreshConversations()
                        }
                    case .connected:
                        liveUpdatesConnected = true
                    case .heartbeat:
                        break
                    default:
                        break
                    }
                }
                if !Task.isCancelled {
                    liveUpdatesConnected = false
                }
            } catch {
                if !Task.isCancelled {
                    liveUpdatesConnected = false
                }
            }
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
            messages[index].attachments.append(contentsOf: event.attachments)
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
        case .turnEnded, .end, .close:
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
