import Markdown
import PhotosUI
import SwiftUI
import UIKit
import UniformTypeIdentifiers

struct ChatRootView: View {
    @State private var viewModel: ChatViewModel
    // Drives which column the compact (iPhone) split view shows. Bound so the
    // launch decision (restore a thread vs. land on the list) is honored
    // deterministically and stays in sync as the user opens threads / taps Back,
    // rather than letting the selection-before-data-loads race decide.
    @State private var preferredColumn: NavigationSplitViewColumn
    let routeConversationID: String?
    let initialPrompt: String?

    init(
        authManager: AuthManager,
        conversationID: String?,
        initialPrompt: String?
    ) {
        let model = ChatViewModel(
            authManager: authManager,
            conversationID: conversationID,
            initialPrompt: initialPrompt
        )
        _viewModel = State(initialValue: model)
        _preferredColumn = State(initialValue: model.conversationSelection == nil ? .sidebar : .detail)
        routeConversationID = conversationID
        self.initialPrompt = initialPrompt
    }

    var body: some View {
        NavigationSplitView(preferredCompactColumn: $preferredColumn) {
            ConversationListView(viewModel: viewModel)
                .navigationTitle("Chats")
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            viewModel.startNewConversation()
                        } label: {
                            Label("New Chat", systemImage: "square.and.pencil")
                        }
                    }
                }
        } detail: {
            ChatThreadView(viewModel: viewModel)
                .navigationTitle("Chat")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    if !viewModel.liveUpdatesConnected {
                        ToolbarItem(placement: .topBarLeading) {
                            LiveUpdatesIndicator(viewModel: viewModel)
                        }
                    }
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
        .onChange(of: viewModel.conversationSelection) { _, selection in
            // Opening a thread reveals the detail column; tapping Back to the
            // list clears the selection and returns to the sidebar.
            preferredColumn = selection == nil ? .sidebar : .detail
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
        List(selection: Binding(
            get: { viewModel.conversationSelection },
            set: { viewModel.updateSelection($0) }
        )) {
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
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(spacing: 0) {
            PendingConfirmationsBanner(viewModel: viewModel)
            if scenePhase == .active {
                messageScrollArea
            } else {
                // Only lay out the chat thread when the scene is active. SwiftUI
                // reports .background on an offscreen background launch (push /
                // state restoration / snapshot) and .inactive during restoration,
                // snapshots, and foreground/background transitions; laying out a
                // long restored thread in either state overruns the ~10s
                // scene-update watchdog and the app is killed with 0x8BADF00D. The
                // real list renders when the scene becomes active. See
                // docs/design/ios-chat-layout-watchdog-crash.md.
                Color.clear
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            Divider()
            ChatComposerView(viewModel: viewModel)
        }
        .onChange(of: scenePhase) { oldPhase, newPhase in
            // Returning to the foreground from the BACKGROUND: re-establish the
            // live-updates follow stream and catch up persisted history. A turn
            // that finished while the app was backgrounded (the follow Task is
            // suspended/torn down by the OS) would otherwise strand until a manual
            // refresh. With the SSE-independent catch-up in `reconnectLiveUpdates`,
            // the thread recovers even if the fresh SSE connect itself fails.
            //
            // Gate on `oldPhase == .background` so a transient `.inactive → .active`
            // blip (Control Center, the app switcher, a notification banner) does
            // not needlessly tear down and restart a healthy follow connection.
            if oldPhase == .background, newPhase == .active {
                Task { await viewModel.reconnectLiveUpdates() }
            }
        }
    }

    private var messageScrollArea: some View {
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
                    if viewModel.hasEarlierMessages {
                        Button("Load earlier messages") {
                            viewModel.showEarlierMessages()
                        }
                        .font(.subheadline)
                        .padding(.vertical, 4)
                        .accessibilityIdentifier("chat-load-earlier")
                    }
                    ForEach(viewModel.visibleGroupedMessages) { message in
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
            .onAppear {
                // The thread can mount already populated (restored thread, or
                // returning from a backgrounded scene where the list was skipped),
                // in which case the last-message onChange below never fires. Land
                // at the bottom so the latest message is visible.
                if let lastID = viewModel.visibleGroupedMessages.last?.id {
                    proxy.scrollTo(lastID, anchor: .bottom)
                }
            }
            .onChange(of: viewModel.visibleGroupedMessages.last?.id) { _, newValue in
                if let lastID = newValue {
                    withAnimation(.easeOut(duration: 0.2)) {
                        proxy.scrollTo(lastID, anchor: .bottom)
                    }
                }
            }
        }
    }
}

/// Whether `text` contains anything that could render as formatted markdown.
///
/// Used as a cheap fast path: plain prose (the common reply, and most of every
/// streamed reply before any syntax appears) skips the markdown parser entirely
/// and renders as plain `Text`. It is deliberately conservative — it returns
/// `true` whenever any markdown construct *could* be present, so formatting is
/// never silently dropped; only genuinely plain text takes the fast path.
func containsMarkdownSyntax(_ text: String) -> Bool {
    // Inline markers can appear anywhere on a line.
    let inlineMarkers: Set<Character> = ["*", "_", "`", "[", "]", "|", "~", "<"]
    if text.contains(where: inlineMarkers.contains) {
        return true
    }
    // Block markers only count at the start of a (whitespace-stripped) line:
    // headings (#), blockquotes (>), unordered lists (-, +) and ordered lists
    // (digits followed by . or )).
    for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
        let line = rawLine.drop { $0 == " " || $0 == "\t" }
        guard let first = line.first else {
            continue
        }
        if first == "#" || first == ">" || first == "-" || first == "+" {
            return true
        }
        if first.isNumber {
            let afterDigits = line.drop(while: \.isNumber)
            if let marker = afterDigits.first, marker == "." || marker == ")" {
                return true
            }
        }
    }
    return false
}

private struct MessageBubble: View {
    let message: ChatMessage
    var viewModel: ChatViewModel

    private var isUser: Bool {
        message.role == .user
    }

    var body: some View {
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
                if containsMarkdownSyntax(message.text) {
                    NativeMarkdownView(markdown: message.text)
                } else {
                    // Plain prose skips the markdown parser entirely. Streamed
                    // deltas are coalesced (see ChatViewModel) and completed
                    // text is memoized, so live-formatted rendering stays off
                    // the main thread long enough to avoid the layout watchdog.
                    Text(message.text)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if !message.toolCalls.isEmpty {
                ToolGroupView(toolCalls: message.toolCalls, viewModel: viewModel)
            }
            if !message.attachments.isEmpty {
                AttachmentStrip(attachments: message.attachments, viewModel: viewModel)
            }
        }
        // One textSelection for the whole bubble. It applies to every Text in the
        // subtree, so per-element copies are redundant and only add layout work.
        .textSelection(.enabled)
        .padding(12)
        .background(isUser ? Color.accentColor.opacity(0.16) : Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        // Cap the bubble to a single concrete width, then push it to its side with
        // alignment rather than a flexible Spacer. The old HStack + Spacer(minLength:)
        // + maxWidth:680 combination made the bubble width depend on a flexible
        // sibling, so SwiftUI re-proposed candidate widths and re-measured the
        // (selectable, possibly markdown) text repeatedly — a layout-watchdog hazard.
        .frame(maxWidth: 680, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: isUser ? .trailing : .leading)
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

    private var sendButtonEnabled: Bool {
        viewModel.isStreaming || viewModel.canSendDraft
    }

    var body: some View {
        VStack(spacing: 8) {
            if !viewModel.draftAttachments.isEmpty {
                DraftAttachmentStrip(viewModel: viewModel)
            }
            HStack(alignment: .bottom, spacing: 4) {
                PhotosPicker(selection: $selectedPhotoItem, matching: .images) {
                    Image(systemName: "photo")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                        .frame(width: 32, height: 36)
                }
                .accessibilityLabel("Add Photo")

                Button {
                    importsFiles = true
                } label: {
                    Image(systemName: "paperclip")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                        .frame(width: 32, height: 36)
                }
                .accessibilityLabel("Add File")

                TextField(
                    "Message",
                    text: Binding(
                        get: { viewModel.draftText },
                        set: { viewModel.draftText = $0 }
                    ),
                    axis: .vertical
                )
                    .textFieldStyle(.plain)
                    .lineLimit(1...6)
                    .frame(minHeight: 36)
                    .padding(.horizontal, 4)
                    .accessibilityIdentifier("chat-composer")

                Button {
                    if viewModel.isStreaming {
                        viewModel.cancelStream()
                    } else {
                        Task { await viewModel.sendDraft() }
                    }
                } label: {
                    Image(systemName: viewModel.isStreaming ? "stop.fill" : "arrow.up")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.white)
                        .frame(width: 30, height: 30)
                        .background(sendButtonEnabled ? Color.accentColor : Color.secondary.opacity(0.4))
                        .clipShape(Circle())
                }
                .buttonStyle(.plain)
                .disabled(!viewModel.isStreaming && !viewModel.canSendDraft)
                .padding(.vertical, 3)
                .accessibilityIdentifier(viewModel.isStreaming ? "chat-stop-button" : "chat-send-button")
            }
            .padding(.horizontal, 6)
            .background(
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .fill(Color(.secondarySystemBackground))
            )
        }
        .padding(.horizontal)
        .padding(.vertical, 6)
        .background(.bar)
        .onChange(of: selectedPhotoItem) { _, item in
            guard let item else { return }
            Task {
                defer { selectedPhotoItem = nil }
                guard let contentType = Self.supportedPhotoContentType(for: item),
                      let mimeType = contentType.preferredMIMEType
                else {
                    viewModel.reportAttachmentImportError("Selected image type is not supported.")
                    return
                }
                do {
                    guard let data = try await item.loadTransferable(type: Data.self) else {
                        viewModel.reportAttachmentImportError("Could not import the selected photo.")
                        return
                    }
                    let uploadData = try Self.uploadData(forPickedPhotoData: data, mimeType: mimeType)
                    let uploadMIMEType = ChatConstants.uploadMIMEType(forPickedPhotoMIMEType: mimeType)
                    let fileExtension = ChatConstants.uploadFilenameExtension(
                        forPickedPhotoMIMEType: mimeType,
                        fallback: contentType.preferredFilenameExtension
                    )
                    await viewModel.addImageData(
                        uploadData,
                        filename: "\(UUID().uuidString).\(fileExtension)",
                        mimeType: uploadMIMEType
                    )
                } catch {
                    viewModel.reportAttachmentImportError("Could not import the selected photo. \(error.localizedDescription)")
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
            return ChatConstants.allowedPhotoPickerMIMETypes.contains(mimeType)
        }
    }

    private static func uploadData(forPickedPhotoData data: Data, mimeType: String) throws -> Data {
        guard ChatConstants.photoPickerTranscodedMIMETypes.contains(mimeType) else {
            return data
        }
        guard let image = UIImage(data: data),
              let jpegData = image.jpegData(compressionQuality: 0.9)
        else {
            throw ChatAPIError.validation("Could not convert the selected photo to JPEG.")
        }
        return jpegData
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
                        .disabled(attachment.uploadState == .uploading)
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

private struct LiveUpdatesIndicator: View {
    var viewModel: ChatViewModel

    var body: some View {
        Button {
            Task { await viewModel.reconnectLiveUpdates() }
        } label: {
            Image(systemName: "wifi.slash")
                .foregroundStyle(.orange)
        }
        .accessibilityLabel("Live updates disconnected. Tap to reconnect.")
        .accessibilityIdentifier("chat-live-updates-disconnected")
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

indirect enum NativeMarkdownBlock: Equatable {
    case paragraph(String)
    case heading(level: Int, text: String)
    case unorderedList([NativeMarkdownListItem])
    case orderedList(startIndex: UInt, items: [NativeMarkdownListItem])
    case blockQuote([NativeMarkdownBlock])
    case codeBlock(language: String?, code: String)
    case table(header: [String], rows: [[String]])
    case thematicBreak
    case fallback(String)
}

struct NativeMarkdownListItem: Equatable {
    let checkbox: Checkbox?
    let blocks: [NativeMarkdownBlock]
}

enum NativeMarkdownRenderer {
    // Parsing markdown is expensive, and SwiftUI re-evaluates a bubble's body
    // far more often than its text changes (scrolling a long thread, sibling
    // updates, layout passes). Caching by exact source string keeps a given
    // message from being re-parsed on every render. Parsing is pure, so caching
    // by the source string is always correct.
    private final class BlocksBox {
        let blocks: [NativeMarkdownBlock]
        init(_ blocks: [NativeMarkdownBlock]) { self.blocks = blocks }
    }

    private final class AttributedBox {
        let value: AttributedString?
        init(_ value: AttributedString?) { self.value = value }
    }

    private static let blockCache: NSCache<NSString, BlocksBox> = {
        let cache = NSCache<NSString, BlocksBox>()
        cache.countLimit = 256
        return cache
    }()

    private static let inlineCache: NSCache<NSString, AttributedBox> = {
        let cache = NSCache<NSString, AttributedBox>()
        cache.countLimit = 512
        return cache
    }()

    static func blocks(from markdown: String) -> [NativeMarkdownBlock] {
        let key = markdown as NSString
        if let cached = blockCache.object(forKey: key) {
            return cached.blocks
        }
        let document = Document(parsing: markdown)
        let parsed = document.children.flatMap(blocks(from:))
        let result = parsed.isEmpty ? [.paragraph(markdown)] : parsed
        blockCache.setObject(BlocksBox(result), forKey: key)
        return result
    }

    static func inlineAttributedString(from markdown: String) -> AttributedString? {
        let key = markdown as NSString
        if let cached = inlineCache.object(forKey: key) {
            return cached.value
        }
        let value = try? AttributedString(
            markdown: markdown,
            options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )
        inlineCache.setObject(AttributedBox(value), forKey: key)
        return value
    }

    private static func blocks(from markup: Markup) -> [NativeMarkdownBlock] {
        switch markup {
        case let heading as Heading:
            return [.heading(level: heading.level, text: heading.plainText)]
        case let paragraph as Paragraph:
            return [.paragraph(inlineMarkdown(from: paragraph))]
        case let unorderedList as UnorderedList:
            return [.unorderedList(unorderedList.listItems.map(listItem(from:)))]
        case let orderedList as OrderedList:
            return [.orderedList(startIndex: orderedList.startIndex, items: orderedList.listItems.map(listItem(from:)))]
        case let blockQuote as BlockQuote:
            return [.blockQuote(blockQuote.children.flatMap(blocks(from:)))]
        case let codeBlock as CodeBlock:
            return [.codeBlock(language: codeBlock.language, code: codeBlock.code.trimmingCharacters(in: CharacterSet.newlines))]
        case let table as Markdown.Table:
            return [.table(header: table.head.cells.map { $0.plainText }, rows: table.body.rows.map { $0.cells.map { $0.plainText } })]
        case is ThematicBreak:
            return [.thematicBreak]
        default:
            let fallback = inlineMarkdown(from: markup)
            return fallback.isEmpty ? [] : [.fallback(fallback)]
        }
    }

    private static func listItem(from item: ListItem) -> NativeMarkdownListItem {
        NativeMarkdownListItem(checkbox: item.checkbox, blocks: item.children.flatMap(blocks(from:)))
    }

    private static func inlineMarkdown(from markup: Markup) -> String {
        var formatter = MarkupFormatter()
        formatter.visit(markup.detachedFromParent)
        return formatter.result.trimmingCharacters(in: CharacterSet.whitespacesAndNewlines)
    }
}

private struct NativeMarkdownView: View {
    let markdown: String

    private var blocks: [NativeMarkdownBlock] {
        NativeMarkdownRenderer.blocks(from: markdown)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                MarkdownBlockView(block: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct MarkdownBlockView: View {
    let block: NativeMarkdownBlock

    var body: some View {
        switch block {
        case let .paragraph(markdown), let .fallback(markdown):
            inlineText(markdown)
                .frame(maxWidth: .infinity, alignment: .leading)
        case let .heading(level, text):
            Text(text)
                .font(headingFont(for: level))
                .frame(maxWidth: .infinity, alignment: .leading)
        case let .unorderedList(items):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    MarkdownListItemView(marker: marker(for: item), item: item)
                }
            }
        case let .orderedList(startIndex, items):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { offset, item in
                    MarkdownListItemView(marker: "\(Int(startIndex) + offset).", item: item)
                }
            }
        case let .blockQuote(blocks):
            HStack(alignment: .top, spacing: 8) {
                Rectangle()
                    .fill(Color.secondary.opacity(0.35))
                    .frame(width: 3)
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                        MarkdownBlockView(block: block)
                    }
                }
            }
            .foregroundStyle(.secondary)
        case let .codeBlock(language, code):
            VStack(alignment: .leading, spacing: 4) {
                if let language, !language.isEmpty {
                    Text(language)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                }
                ScrollView(.horizontal, showsIndicators: false) {
                    Text(code)
                        .font(.caption.monospaced())
                        .padding(8)
                }
            }
            .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 6))
        case let .table(header, rows):
            ScrollView(.horizontal, showsIndicators: false) {
                Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                    markdownTableRow(header, isHeader: true)
                    ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                        markdownTableRow(row, isHeader: false)
                    }
                }
            }
        case .thematicBreak:
            Divider()
        }
    }

    private func marker(for item: NativeMarkdownListItem) -> String {
        switch item.checkbox {
        case .checked:
            "[x]"
        case .unchecked:
            "[ ]"
        case nil:
            "•"
        }
    }

    private func headingFont(for level: Int) -> Font {
        switch level {
        case 1:
            .title3.bold()
        case 2:
            .headline
        default:
            .subheadline.bold()
        }
    }

    @ViewBuilder
    private func markdownTableRow(_ cells: [String], isHeader: Bool) -> some View {
        GridRow {
            ForEach(Array(cells.enumerated()), id: \.offset) { _, cell in
                // Cells size to their content. `.frame(maxWidth: .infinity)` here
                // is a non-converging width proposal inside the enclosing
                // horizontal ScrollView (which proposes unbounded width), which can
                // hang the layout engine; the Grid's `.leading` alignment handles
                // column alignment instead.
                inlineText(cell)
                    .font(isHeader ? .caption.bold() : .caption)
                    .multilineTextAlignment(.leading)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                    .background(isHeader ? Color(.tertiarySystemFill) : Color(.secondarySystemFill).opacity(0.45))
                    .border(Color(.separator), width: 0.5)
            }
        }
    }

    private func inlineText(_ markdown: String) -> SwiftUI.Text {
        if let attributed = NativeMarkdownRenderer.inlineAttributedString(from: markdown) {
            return Text(attributed)
        }
        return Text(markdown)
    }
}

private struct MarkdownListItemView: View {
    let marker: String
    let item: NativeMarkdownListItem

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Text(marker)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .frame(width: 24, alignment: .trailing)
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(item.blocks.enumerated()), id: \.offset) { _, block in
                    MarkdownBlockView(block: block)
                }
            }
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
                        shareURL = await viewModel.downloadAttachmentForSharing(attachment)
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
            image = nil
            failed = false
            do {
                let data = try await viewModel.authenticatedImageData(for: attachment)
                guard let decodedImage = UIImage(data: data) else {
                    image = nil
                    failed = true
                    return
                }
                image = decodedImage
                failed = false
            } catch {
                image = nil
                failed = true
            }
        }
    }
}

private struct LoadingDotsView: View {
    @State private var phase = 0
    @State private var timer: Timer?

    var body: some View {
        Text(String(repeating: ".", count: phase + 1))
            .font(.title3.monospaced())
            .onAppear {
                timer?.invalidate()
                timer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { _ in
                    phase = (phase + 1) % 3
                }
            }
            .onDisappear {
                timer?.invalidate()
                timer = nil
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
