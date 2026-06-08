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
    var draftText = ""
    var draftAttachments: [ChatAttachment] = []
    var pendingConfirmations: [ChatPendingConfirmation] = []
    var isLoadingConversations = false
    var isLoadingMessages = false
    var isLoadingProfiles = false
    var isStreaming = false
    var errorMessage: String?
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
    @ObservationIgnored private var processedInitialPrompt = false

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
        if let initialPrompt, !initialPrompt.isEmpty, !processedInitialPrompt {
            processedInitialPrompt = true
            draftText = initialPrompt
            await sendDraft()
        } else if !draftText.isEmpty, !processedInitialPrompt {
            processedInitialPrompt = true
            await sendDraft()
        }
    }

    func applyRoute(conversationID: String?, initialPrompt: String?) async {
        if let initialPrompt, !initialPrompt.isEmpty, !processedInitialPrompt {
            processedInitialPrompt = true
            startNewConversation()
            draftText = initialPrompt
            await sendDraft()
            return
        }
        if let conversationID, conversationID != self.conversationID {
            await selectConversation(conversationID, shouldLoadMessages: true)
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

    func selectConversation(_ id: String, shouldLoadMessages: Bool = true) async {
        cancelStream()
        conversationID = id
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

        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                let stream = try await apiClient.streamMessage(
                    prompt: prompt,
                    conversationID: id,
                    profileID: selectedProfileID,
                    attachments: uploadedAttachments
                )
                for try await event in stream {
                    if Task.isCancelled {
                        break
                    }
                    apply(streamEvent: event, assistantMessageID: assistantMessageID)
                    if event.type == .end || event.type == .close {
                        break
                    }
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

    func addImageData(_ data: Data, filename: String = "\(UUID().uuidString).jpg") async {
        do {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(filename)
            try data.write(to: url)
            await addAttachment(fileURL: url, mimeType: "image/jpeg", displayName: filename)
        } catch {
            errorMessage = error.localizedDescription
        }
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
        draftAttachments.removeAll { $0.id == attachment.id }
        if let attachmentID = attachment.attachmentID {
            try? await apiClient.deleteAttachment(attachmentID: attachmentID)
        }
    }

    func confirm(_ confirmation: ChatPendingConfirmation, approved: Bool) async {
        do {
            try await apiClient.confirmTool(
                requestID: confirmation.requestID,
                conversationID: conversationID,
                approved: approved
            )
            pendingConfirmations.removeAll { $0.requestID == confirmation.requestID }
            updateToolConfirmation(requestID: confirmation.requestID, approved: approved)
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

    private func startLiveEvents() {
        liveEventsTask?.cancel()
        guard let conversationID else {
            return
        }
        liveEventsTask = Task { [weak self] in
            guard let self else { return }
            do {
                let stream = try await apiClient.connectEvents(conversationID: conversationID)
                for try await event in stream {
                    if Task.isCancelled {
                        break
                    }
                    switch event.type {
                    case .message:
                        if !isStreaming {
                            await loadMessages(conversationID: conversationID)
                            await refreshConversations()
                        }
                    case .connected, .heartbeat:
                        break
                    default:
                        break
                    }
                }
            } catch {
                if !Task.isCancelled {
                    errorMessage = "Live updates disconnected. Pull to refresh."
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
                pendingConfirmations.removeAll { $0.requestID == result.requestID }
                updateToolConfirmation(requestID: result.requestID, approved: result.approved)
            }
        case .error:
            appendStreamError(event.errorMessage ?? "An error occurred.", assistantMessageID: assistantMessageID)
        case .end, .close:
            messages[index].isLoading = false
            messages[index].status = .complete
        case .connected, .message, .heartbeat:
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

    private func updateToolConfirmation(requestID: String, approved: Bool) {
        let status: ChatToolStatus = approved ? .approved : .rejected
        if let confirmation = pendingConfirmations.first(where: { $0.requestID == requestID }) {
            updateToolCall(toolCallID: confirmation.toolCallID, resultText: nil, attachments: [], status: status)
        }
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
