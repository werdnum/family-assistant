import Markdown
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

struct ChatRootView: View {
    @State private var viewModel: ChatViewModel
    let routeConversationID: String?
    let initialPrompt: String?
    let onShowNotes: () -> Void
    let onLogout: () -> Void

    init(
        authManager: AuthManager,
        conversationID: String?,
        initialPrompt: String?,
        onShowNotes: @escaping () -> Void,
        onLogout: @escaping () -> Void
    ) {
        _viewModel = State(
            initialValue: ChatViewModel(
                authManager: authManager,
                conversationID: conversationID,
                initialPrompt: initialPrompt
            )
        )
        routeConversationID = conversationID
        self.initialPrompt = initialPrompt
        self.onShowNotes = onShowNotes
        self.onLogout = onLogout
    }

    var body: some View {
        NavigationSplitView {
            ConversationListView(viewModel: viewModel)
                .navigationTitle("Chats")
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button {
                            onShowNotes()
                        } label: {
                            Label("Notes", systemImage: "note.text")
                        }
                    }
                    ToolbarItemGroup(placement: .topBarTrailing) {
                        Button {
                            viewModel.startNewConversation()
                        } label: {
                            Label("New Chat", systemImage: "square.and.pencil")
                        }
                        AppSettingsMenu(onLogout: onLogout)
                    }
                }
        } detail: {
            ChatThreadView(viewModel: viewModel)
                .navigationTitle("Chat")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        ProfilePickerView(viewModel: viewModel)
                    }
                }
        }
        .task {
            await viewModel.bootstrap(initialPrompt: initialPrompt)
        }
        .onChange(of: routeConversationID) { _, newValue in
            Task {
                await viewModel.applyRoute(conversationID: newValue, initialPrompt: nil)
            }
        }
        .onChange(of: initialPrompt) { _, newValue in
            Task {
                await viewModel.applyRoute(conversationID: routeConversationID, initialPrompt: newValue)
            }
        }
        .alert(
            "Chat Error",
            isPresented: Binding(
                get: { viewModel.errorMessage != nil },
                set: { if !$0 { viewModel.errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(viewModel.errorMessage ?? "")
        }
    }
}

private struct ConversationListView: View {
    var viewModel: ChatViewModel
    @State private var searchText = ""

    private var filteredConversations: [ChatConversationSummary] {
        guard !searchText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return viewModel.conversations
        }
        return viewModel.conversations.filter {
            $0.conversationID.localizedCaseInsensitiveContains(searchText)
                || $0.lastMessage.localizedCaseInsensitiveContains(searchText)
        }
    }

    var body: some View {
        List(selection: Binding(get: { viewModel.conversationID }, set: { value in
            guard let value else { return }
            Task { await viewModel.selectConversation(value) }
        })) {
            if filteredConversations.isEmpty && !viewModel.isLoadingConversations {
                ContentUnavailableView("No Chats", systemImage: "message", description: Text("Start a new chat."))
            } else {
                ForEach(filteredConversations) { conversation in
                    ConversationRow(conversation: conversation)
                        .tag(conversation.conversationID)
                        .accessibilityIdentifier("conversation-row-\(conversation.conversationID)")
                }
            }
        }
        .searchable(text: $searchText, prompt: "Search chats")
        .refreshable {
            await viewModel.refreshConversations()
        }
        .overlay {
            if viewModel.isLoadingConversations && viewModel.conversations.isEmpty {
                ProgressView("Loading chats...")
            }
        }
    }
}

private struct ConversationRow: View {
    let conversation: ChatConversationSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(conversation.lastMessage.isEmpty ? "New chat" : conversation.lastMessage)
                    .lineLimit(2)
                    .font(.headline)
                Spacer()
                Text(conversation.lastTimestamp, style: .date)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                Label("\(conversation.messageCount)", systemImage: "text.bubble")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(conversation.conversationID)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 4)
    }
}

private struct ChatThreadView: View {
    var viewModel: ChatViewModel

    var body: some View {
        VStack(spacing: 0) {
            PendingConfirmationsBanner(viewModel: viewModel)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 14) {
                        if viewModel.messages.isEmpty && !viewModel.isLoadingMessages {
                            ContentUnavailableView {
                                Label("Ask Family Assistant", systemImage: "sparkles")
                            } description: {
                                Text("Start with a message or attach a file.")
                            }
                            .padding(.top, 80)
                        }
                        ForEach(viewModel.messages) { message in
                            MessageBubble(message: message, viewModel: viewModel)
                                .id(message.id)
                                .accessibilityIdentifier("chat-message-\(message.id)")
                        }
                    }
                    .padding()
                }
                .overlay {
                    if viewModel.isLoadingMessages && viewModel.messages.isEmpty {
                        ProgressView("Loading messages...")
                    }
                }
                .onChange(of: viewModel.messages.map(\.id)) { _, _ in
                    if let lastID = viewModel.messages.last?.id {
                        withAnimation(.easeOut(duration: 0.2)) {
                            proxy.scrollTo(lastID, anchor: .bottom)
                        }
                    }
                }
            }

            Divider()
            ChatComposerView(viewModel: viewModel)
        }
    }
}

private struct MessageBubble: View {
    let message: ChatMessage
    var viewModel: ChatViewModel

    private var isUser: Bool {
        message.role == .user
    }

    var body: some View {
        HStack(alignment: .top) {
            if isUser {
                Spacer(minLength: 32)
            }

            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Label(roleTitle, systemImage: isUser ? "person.crop.circle" : "sparkles")
                        .font(.caption.bold())
                    if let profile = message.processingProfileID {
                        Text(profile)
                            .font(.caption2)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(.thinMaterial, in: Capsule())
                    }
                }
                .foregroundStyle(.secondary)

                if message.isLoading && message.text.isEmpty {
                    LoadingDotsView()
                }
                if !message.text.isEmpty {
                    NativeMarkdownView(markdown: message.text)
                        .textSelection(.enabled)
                }
                if !message.toolCalls.isEmpty {
                    ToolGroupView(toolCalls: message.toolCalls, viewModel: viewModel)
                }
                if !message.attachments.isEmpty {
                    AttachmentStrip(attachments: message.attachments, viewModel: viewModel)
                }
            }
            .padding(12)
            .background(isUser ? Color.accentColor.opacity(0.16) : Color(.secondarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .frame(maxWidth: 680, alignment: isUser ? .trailing : .leading)

            if !isUser {
                Spacer(minLength: 32)
            }
        }
    }

    private var roleTitle: String {
        switch message.role {
        case .user:
            "You"
        case .assistant:
            "Assistant"
        case .system:
            "System"
        case .tool:
            "Tool"
        case .error:
            "Error"
        }
    }
}

private struct ChatComposerView: View {
    var viewModel: ChatViewModel
    @State private var selectedPhotoItem: PhotosPickerItem?
    @State private var importsFiles = false

    var body: some View {
        VStack(spacing: 8) {
            if !viewModel.draftAttachments.isEmpty {
                DraftAttachmentStrip(viewModel: viewModel)
            }
            HStack(alignment: .bottom, spacing: 8) {
                PhotosPicker(selection: $selectedPhotoItem, matching: .images) {
                    Image(systemName: "photo")
                        .frame(width: 36, height: 36)
                }
                .buttonStyle(.bordered)
                .accessibilityLabel("Add Photo")

                Button {
                    importsFiles = true
                } label: {
                    Image(systemName: "paperclip")
                        .frame(width: 36, height: 36)
                }
                .buttonStyle(.bordered)
                .accessibilityLabel("Add File")

                TextField(
                    "Message",
                    text: Binding(
                        get: { viewModel.draftText },
                        set: { viewModel.draftText = $0 }
                    ),
                    axis: .vertical
                )
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...6)
                    .accessibilityIdentifier("chat-composer")

                Button {
                    if viewModel.isStreaming {
                        viewModel.cancelStream()
                    } else {
                        Task { await viewModel.sendDraft() }
                    }
                } label: {
                    Image(systemName: viewModel.isStreaming ? "stop.fill" : "arrow.up")
                        .frame(width: 36, height: 36)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!viewModel.isStreaming && !viewModel.canSendDraft)
                .accessibilityIdentifier(viewModel.isStreaming ? "chat-stop-button" : "chat-send-button")
            }
        }
        .padding()
        .background(.bar)
        .onChange(of: selectedPhotoItem) { _, item in
            guard let item else { return }
            Task {
                defer { selectedPhotoItem = nil }
                guard let contentType = Self.supportedPhotoContentType(for: item),
                      let mimeType = contentType.preferredMIMEType
                else {
                    await viewModel.reportAttachmentImportError("Selected image type is not supported.")
                    return
                }
                do {
                    guard let data = try await item.loadTransferable(type: Data.self) else {
                        await viewModel.reportAttachmentImportError("Could not import the selected photo.")
                        return
                    }
                    let fileExtension = contentType.preferredFilenameExtension ?? "jpg"
                    await viewModel.addImageData(
                        data,
                        filename: "\(UUID().uuidString).\(fileExtension)",
                        mimeType: mimeType
                    )
                } catch {
                    await viewModel.reportAttachmentImportError("Could not import the selected photo. \(error.localizedDescription)")
                }
            }
        }
        .fileImporter(
            isPresented: $importsFiles,
            allowedContentTypes: [.jpeg, .png, .gif, .webP, .plainText, .markdown, .pdf],
            allowsMultipleSelection: true
        ) { result in
            if case .success(let urls) = result {
                for url in urls {
                    Task { await viewModel.addAttachment(fileURL: url) }
                }
            }
        }
    }

    private static func supportedPhotoContentType(for item: PhotosPickerItem) -> UTType? {
        item.supportedContentTypes.first { contentType in
            guard contentType.conforms(to: .image),
                  let mimeType = contentType.preferredMIMEType
            else {
                return false
            }
            return ChatConstants.allowedAttachmentMIMETypes.contains(mimeType)
        }
    }
}

private struct DraftAttachmentStrip: View {
    var viewModel: ChatViewModel

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(viewModel.draftAttachments) { attachment in
                    HStack(spacing: 6) {
                        Image(systemName: icon(for: attachment))
                        Text(attachment.name)
                            .lineLimit(1)
                        if attachment.uploadState == .uploading {
                            ProgressView()
                        }
                        if attachment.uploadState == .failed {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.red)
                        }
                        Button {
                            Task { await viewModel.removeDraftAttachment(attachment) }
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                        }
                        .accessibilityLabel("Remove \(attachment.name)")
                    }
                    .font(.caption)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8))
                    .accessibilityIdentifier("draft-attachment-\(attachment.id)")
                }
            }
        }
    }

    private func icon(for attachment: ChatAttachment) -> String {
        switch attachment.type {
        case .image:
            "photo"
        case .document:
            "doc.text"
        case .file:
            "paperclip"
        }
    }
}

private struct ProfilePickerView: View {
    var viewModel: ChatViewModel

    var body: some View {
        Menu {
            ForEach(viewModel.profiles) { profile in
                Button {
                    viewModel.changeProfile(to: profile.id)
                } label: {
                    VStack(alignment: .leading) {
                        Text(profile.id)
                        Text(profile.description)
                    }
                }
                .disabled(viewModel.isStreaming)
            }
        } label: {
            Label(viewModel.selectedProfileID, systemImage: "person.badge.gearshape")
                .labelStyle(.titleAndIcon)
        }
        .accessibilityIdentifier("profile-picker")
    }
}

private struct PendingConfirmationsBanner: View {
    var viewModel: ChatViewModel

    var body: some View {
        if !viewModel.pendingConfirmations.isEmpty {
            VStack(spacing: 8) {
                ForEach(viewModel.pendingConfirmations) { confirmation in
                    ConfirmationCard(confirmation: confirmation, viewModel: viewModel)
                }
            }
            .padding(10)
            .background(Color.yellow.opacity(0.16))
            .accessibilityIdentifier("pending-confirmations-banner")
        }
    }
}

private struct ConfirmationCard: View {
    let confirmation: ChatPendingConfirmation
    var viewModel: ChatViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(confirmation.toolName, systemImage: "hand.raised")
                    .font(.headline)
                Spacer()
                if let remaining = confirmation.timeRemainingSeconds {
                    Text("\(Int(remaining))s")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Text(confirmation.confirmationPrompt)
                .font(.subheadline)
            if !confirmation.args.isEmpty {
                Text(JSONValue.object(confirmation.args).jsonString)
                    .font(.caption.monospaced())
                    .lineLimit(4)
                    .foregroundStyle(.secondary)
            }
            if let error = confirmation.errorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            HStack {
                Button(role: .destructive) {
                    Task { await viewModel.confirm(confirmation, approved: false) }
                } label: {
                    Label("Reject", systemImage: "xmark")
                }
                .buttonStyle(.bordered)
                .accessibilityIdentifier("approval-reject-\(confirmation.requestID)")

                Button {
                    Task { await viewModel.confirm(confirmation, approved: true) }
                } label: {
                    Label("Approve", systemImage: "checkmark")
                }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("approval-approve-\(confirmation.requestID)")
            }
        }
        .padding(10)
        .background(Color(.systemBackground), in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct NativeMarkdownView: View {
    let markdown: String

    var body: some View {
        let _ = Document(parsing: markdown)
        if let attributed = try? AttributedString(
            markdown: markdown,
            options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        ) {
            Text(attributed)
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            Text(markdown)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct ToolGroupView: View {
    let toolCalls: [ChatToolCall]
    var viewModel: ChatViewModel
    @State private var collapsedCompleted = true

    private var shouldCollapse: Bool {
        collapsedCompleted && toolCalls.allSatisfy { $0.status == .complete }
    }

    var body: some View {
        DisclosureGroup(isExpanded: Binding(get: { !shouldCollapse }, set: { collapsedCompleted = !$0 })) {
            VStack(spacing: 8) {
                ForEach(toolCalls) { toolCall in
                    ToolCallCard(toolCall: toolCall, viewModel: viewModel)
                }
            }
            .padding(.top, 6)
        } label: {
            Label("\(toolCalls.count) tool \(toolCalls.count == 1 ? "call" : "calls")", systemImage: "wrench.and.screwdriver")
                .font(.subheadline.bold())
        }
        .accessibilityIdentifier("tool-group")
    }
}

private struct ToolCallCard: View {
    let toolCall: ChatToolCall
    var viewModel: ChatViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(toolCall.name, systemImage: icon)
                    .font(.subheadline.bold())
                Spacer()
                Text(toolCall.status.rawValue)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text(toolCall.argumentsText)
                .font(.caption.monospaced())
                .lineLimit(6)
                .foregroundStyle(.secondary)
            if let result = toolCall.resultText {
                NativeMarkdownView(markdown: result)
                    .font(.caption)
            }
            if !toolCall.attachments.isEmpty {
                AttachmentStrip(attachments: toolCall.attachments, viewModel: viewModel)
            }
        }
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 8))
        .accessibilityIdentifier("tool-call-\(toolCall.id)")
    }

    private var icon: String {
        switch toolCall.status {
        case .running:
            "hourglass"
        case .awaitingApproval:
            "hand.raised"
        case .approved:
            "checkmark.circle"
        case .rejected, .failed:
            "xmark.octagon"
        case .complete:
            "checkmark.circle"
        }
    }
}

private struct AttachmentStrip: View {
    let attachments: [ChatAttachment]
    var viewModel: ChatViewModel

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach(attachments) { attachment in
                    AttachmentPreview(attachment: attachment, viewModel: viewModel)
                }
            }
        }
    }
}

private struct AttachmentPreview: View {
    let attachment: ChatAttachment
    var viewModel: ChatViewModel
    @State private var shareURL: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if attachment.type == .image {
                AuthenticatedAttachmentImage(attachment: attachment, viewModel: viewModel)
                    .frame(width: 160, height: 110)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                Image(systemName: attachment.type == .document ? "doc.text" : "paperclip")
                    .font(.largeTitle)
                    .frame(width: 160, height: 80)
                    .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 8))
            }
            HStack {
                Text(attachment.name)
                    .font(.caption)
                    .lineLimit(1)
                Spacer()
                Button {
                    Task {
                        shareURL = try? await viewModel.downloadAttachment(attachment)
                    }
                } label: {
                    Image(systemName: "square.and.arrow.down")
                }
                .accessibilityLabel("Download \(attachment.name)")
            }
        }
        .frame(width: 170)
        .sheet(item: Binding(
            get: { shareURL.map(ShareURL.init(url:)) },
            set: { if $0 == nil { shareURL = nil } }
        )) { item in
            ShareSheet(activityItems: [item.url])
        }
        .accessibilityIdentifier("attachment-preview-\(attachment.id)")
    }
}

private struct AuthenticatedAttachmentImage: View {
    let attachment: ChatAttachment
    var viewModel: ChatViewModel
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
            } else if failed {
                Image(systemName: "photo")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color(.secondarySystemBackground))
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color(.secondarySystemBackground))
            }
        }
        .task(id: attachment.contentURL) {
            do {
                let data = try await viewModel.authenticatedImageData(for: attachment)
                image = UIImage(data: data)
            } catch {
                failed = true
            }
        }
    }
}

private struct LoadingDotsView: View {
    @State private var phase = 0

    var body: some View {
        Text(String(repeating: ".", count: phase + 1))
            .font(.title3.monospaced())
            .onAppear {
                Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { _ in
                    phase = (phase + 1) % 3
                }
            }
    }
}

private struct ShareURL: Identifiable {
    let url: URL
    var id: URL { url }
}

private struct ShareSheet: UIViewControllerRepresentable {
    let activityItems: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: activityItems, applicationActivities: nil)
    }

    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}

private extension UTType {
    static let markdown = UTType(filenameExtension: "md") ?? .plainText
}
