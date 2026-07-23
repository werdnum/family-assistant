import Combine
import Markdown
import PhotosUI
import SwiftUI
import UIKit
import UniformTypeIdentifiers

struct ChatRootView: View {
    @Environment(SharedAttachmentInbox.self) private var sharedAttachmentInbox
    @Environment(NotificationManager.self) private var notificationManager
    @State private var viewModel: ChatViewModel
    // Drives which column the compact (iPhone) split view shows. Bound so the
    // launch decision (restore a thread vs. land on the list) is honored
    // deterministically and stays in sync as the user opens threads / taps Back,
    // rather than letting the selection-before-data-loads race decide.
    @State private var preferredColumn: NavigationSplitViewColumn
    let route: ChatRoute

    init(
        authManager: AuthManager,
        route: ChatRoute
    ) {
        let model = ChatViewModel(
            authManager: authManager,
            conversationID: route.conversationID,
            initialPrompt: route.initialPrompt,
            startsNewConversation: route.newConversationRequestID != nil
        )
        _viewModel = State(initialValue: model)
        _preferredColumn = State(initialValue: model.conversationSelection == nil ? .sidebar : .detail)
        self.route = route
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
                    if viewModel.syncPresentation != .live {
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
            await viewModel.bootstrap(initialPrompt: route.initialPrompt)
            await importSharedAttachments(for: route)
            // `onChange` below does not fire for a hint already pending at mount
            // (e.g. a push delivered during auth bootstrap or before this view
            // mounted), so consume the current value once the chat UI is ready.
            consumePendingPushHint()
        }
        .onChange(of: route) { _, newRoute in
            Task {
                await viewModel.applyRoute(
                    conversationID: newRoute.conversationID,
                    initialPrompt: newRoute.initialPrompt,
                    newConversationRequestID: newRoute.newConversationRequestID
                )
                await importSharedAttachments(for: newRoute)
            }
        }
        .onChange(of: viewModel.conversationSelection) { _, selection in
            // Opening a thread reveals the detail column; tapping Back to the
            // list clears the selection and returns to the sidebar.
            preferredColumn = selection == nil ? .sidebar : .detail
        }
        .onChange(of: notificationManager.pendingPushHint) { _, _ in
            consumePendingPushHint()
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

    /// Deliver a foreground push hint (§4.6): targeted refresh of the referenced
    /// conversation + recent list, consumed once so a repeat push (a fresh id)
    /// re-fires. Backgrounded delivery is filtered in the coordinator. Called both
    /// from the `pendingPushHint` observer and once on mount, because `onChange`
    /// does not fire for a value already set before the view appeared.
    private func consumePendingPushHint() {
        guard let hint = notificationManager.pendingPushHint else {
            return
        }
        viewModel.pushHintReceived(conversationID: hint.conversationID)
        notificationManager.clearPendingPushHint()
    }

    private func importSharedAttachments(for route: ChatRoute) async {
        guard let batchID = route.sharedAttachmentBatchID,
              let batch = sharedAttachmentInbox.consume(batchID: batchID)
        else {
            return
        }
        await viewModel.addSharedAttachments(batch)
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
        .contextMenu {
            Button {
                UIPasteboard.general.string = conversation.conversationID
            } label: {
                Label("Copy Conversation ID", systemImage: "doc.on.doc")
            }
        }
    }
}

private struct ChatThreadView: View {
    var viewModel: ChatViewModel

    var body: some View {
        VStack(spacing: 0) {
            PendingConfirmationsBanner(viewModel: viewModel)
            ChatScenePhaseMountGate(viewModel: viewModel) {
                messageScrollArea
            }

            ThreadInlineBanner(viewModel: viewModel)
            Divider()
            ChatComposerView(viewModel: viewModel)
        }
    }

    private var messageScrollArea: some View {
        // Auto-follow is centralized in `StickyBottomScroll`: it lands at the
        // bottom on appear and follows new content only while the user is near
        // the bottom — a passive reply that arrives while they have scrolled up
        // to read history must not yank them down (nor force a non-settling
        // bottom-anchored re-measure that trips the scene-update watchdog).
        // Sending a message is a user-initiated event that always pins to the
        // bottom, signalled explicitly via `scrollToLatestRequestID` (it can't be
        // inferred from the last bubble, which is the assistant loading
        // placeholder right after a send). Passive follow is gated on application
        // state inside `StickyBottomScroll`, without making this heavy parent
        // observe `scenePhase`; a background phase change must not invalidate the
        // eager message stack. `forceFollowTrigger` is not gated — it only ever
        // changes on an active user send.
        StickyBottomScroll(
            followTrigger: viewModel.visibleGroupedMessages.last?.id,
            forceFollowTrigger: viewModel.scrollToLatestRequestID,
            canFollow: { UIApplication.shared.applicationState == .active }
        ) {
            VStack(spacing: 14) {
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
                if viewModel.hasNewerMessages {
                    Button("Load newer messages") {
                        viewModel.showNewerMessages()
                    }
                    .font(.subheadline)
                    .padding(.vertical, 4)
                    .accessibilityIdentifier("chat-load-newer")
                }
            }
            .padding()
        }
        .overlay {
            if viewModel.isLoadingMessages && viewModel.messages.isEmpty {
                ProgressView("Loading messages...")
            }
        }
    }
}

/// Keeps scene lifecycle observation out of `ChatThreadView`, whose body owns the
/// bounded eager message stack. A phase change should update only a lightweight
/// observer; invalidating the parent while backgrounding can spend the entire
/// scene-update watchdog budget rebuilding dynamic properties for every row.
private struct ChatScenePhaseMountGate<Content: View>: View {
    var viewModel: ChatViewModel
    @ViewBuilder let content: () -> Content

    @State private var hasMountedContent = false

    var body: some View {
        Group {
            if hasMountedContent {
                content()
            } else {
                // Offscreen background launch (push / state restoration /
                // snapshot): laying out a restored thread before the scene has
                // ever been active overruns the scene-update watchdog.
                Color.clear
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background {
            ChatScenePhaseObserver { oldPhase, newPhase in
                if !hasMountedContent, viewModel.shouldRenderThread(
                    isActive: newPhase == .active,
                    hasMountedBefore: false
                ) {
                    hasMountedContent = true
                }

                // A turn can finish while the app is suspended. The coordinator
                // latches the observed background and runs the resync on the next
                // real resume — catching the two-firing `.background → .inactive →
                // .active` sequence a single-transition predicate used to miss —
                // while transient `.inactive → .active` blips are ignored.
                viewModel.scenePhaseChanged(old: oldPhase, new: newPhase)
            }
        }
    }
}

private struct ChatScenePhaseObserver: View {
    @Environment(\.scenePhase) private var scenePhase
    let phaseChanged: (_ oldPhase: ScenePhase, _ newPhase: ScenePhase) -> Void

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .accessibilityHidden(true)
            .onChange(of: scenePhase, initial: true) { oldPhase, newPhase in
                phaseChanged(oldPhase, newPhase)
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
                // Always route through the budgeted renderer. It decides plain vs
                // markdown on a bounded prefix, so a huge plain response can't
                // render as one unbounded `Text` and the syntax scan stays bounded
                // — both layout-watchdog hazards if the plain fast path lived here.
                NativeMarkdownView(markdown: message.text)
            }
            if !message.toolCalls.isEmpty {
                ToolGroupView(toolCalls: message.toolCalls, viewModel: viewModel)
            }
            if !message.attachments.isEmpty {
                AttachmentStrip(attachments: message.attachments, viewModel: viewModel)
            }
            if let failed = viewModel.failedSend(for: message) {
                Button {
                    Task { await viewModel.retryFailedSend(turnID: failed.turnID) }
                } label: {
                    Label("Retry", systemImage: "arrow.clockwise")
                        .font(.caption.bold())
                }
                .buttonStyle(.bordered)
                .tint(.red)
                // Retrying tears down the current transport before resubscribing, so
                // it must not fire while a newer turn is actively streaming (it would
                // cancel that unrelated in-flight reply). Mirrors the composer's
                // send guard.
                .disabled(viewModel.isStreaming)
                .accessibilityIdentifier("chat-retry-\(message.id)")
            }
        }
        // One textSelection for the whole bubble. It applies to every Text in the
        // subtree, so per-element copies are redundant and only add layout work.
        .textSelection(.enabled)
        // Long-press copies the entire message in one tap. textSelection stays
        // enabled alongside it, so press-and-drag partial selection (and the
        // selection menu's Select All) remain available for sub-message copies.
        .contextMenu {
            if !message.text.isEmpty {
                Button {
                    UIPasteboard.general.string = message.text
                } label: {
                    Label("Copy", systemImage: "doc.on.doc")
                }
            }
        }
        .padding(12)
        .background(isUser ? Color.accentColor.opacity(0.16) : Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay {
            if message.status == .failed {
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(Color.red.opacity(0.5), lineWidth: 1)
            }
        }
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

#if DEBUG
/// Test seam for the chat-layout budget/fuzz harness (`ChatLayoutBudgetTests`).
/// Renders the production message-list layout — `ScrollView` + bounded `VStack` +
/// `MessageBubble` — over an explicit message array so a hosting controller can
/// force a content-sizing pass and time it. This also covers the fixed maximum
/// eager window used to stay below the process-exit watchdog. Mirrors
/// `ChatThreadView.messageScrollArea` without the scroll-position plumbing. The
/// invariant the harness enforces is that this layout stays bounded (well under
/// the scene-update watchdog) for any message shape; see
/// docs/design/ios-chat-layout-watchdog-crash.md.
struct ChatMessageListLayoutProbe: View {
    let messages: [ChatMessage]
    let viewModel: ChatViewModel

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                ForEach(messages) { message in
                    MessageBubble(message: message, viewModel: viewModel)
                        .id(message.id)
                }
            }
            .padding()
        }
    }
}
#endif

/// A vertically scrolling container that keeps the newest content pinned to the
/// bottom as it grows — but only when the caller's `shouldFollow` gate allows,
/// and NEVER with animation.
///
/// This is the single choke point for every auto-following list in the app (the
/// chat thread and the voice transcript), mirroring how `MarkdownRenderBudget`
/// is the single choke point for per-message layout cost. Both invariants exist
/// to keep the app clear of the scene-update layout watchdog (0x8BADF00D), which
/// is a *class* of bug rather than one shape:
///
/// - **Never animate the follow scroll.** An animated `scrollTo(.bottom)` past a
///   tall row re-places the message stack on every animation frame without
///   settling — a main-thread wedge. (This is why the voice transcript, which
///   previously animated on every streamed token, is routed through here.)
/// - **Only follow when the gate allows.** A passive arrival while the user has
///   scrolled up must not fire a bottom-anchored `scrollTo`, which forces the
///   stack to re-resolve its bottom anchor and re-measure backwards over the
///   visible rows without converging (scratch/FamilyAssistant-2026-07-07-090155.ips).
///
/// Near-bottom is tracked with a zero-height sentinel at the end of the content:
/// it is laid out only when the true bottom is on screen, so its appearance
/// state is exactly "is the user at the bottom". `shouldFollow` receives that
/// state and returns whether to follow. A user-initiated event that should
/// always pin to the bottom regardless of scroll position (e.g. the user
/// sending a message) is signalled separately via `forceFollowTrigger`, because
/// that intent is an event, not something derivable from the content.
/// See docs/design/ios-chat-layout-watchdog-crash.md.
struct StickyBottomScroll<Content: View>: View {
    /// Changes whenever new content that may warrant a follow has arrived (the
    /// newest row's id, its streamed text, …). A `nil` value never follows.
    let followTrigger: AnyHashable?
    /// Changes whenever a user-initiated event should scroll to the bottom
    /// unconditionally (ignoring `shouldFollow`) — e.g. the user just sent a
    /// message and expects to see it. `nil`/unchanged never scrolls.
    let forceFollowTrigger: AnyHashable?
    /// Given whether the user is currently near the bottom, whether to follow a
    /// `followTrigger` change. Defaults to "only when near the bottom".
    let shouldFollow: (_ isNearBottom: Bool) -> Bool
    /// Read at trigger time so lifecycle suppression does not require an
    /// `@Environment(\.scenePhase)` dependency in the heavy scrolling subtree.
    let canFollow: () -> Bool
    @ViewBuilder let content: () -> Content

    @State private var isNearBottom = true
    @State private var hasPendingFollow = false

    init(
        followTrigger: AnyHashable?,
        forceFollowTrigger: AnyHashable? = nil,
        shouldFollow: @escaping (_ isNearBottom: Bool) -> Bool = { $0 },
        canFollow: @escaping () -> Bool = { true },
        @ViewBuilder content: @escaping () -> Content
    ) {
        self.followTrigger = followTrigger
        self.forceFollowTrigger = forceFollowTrigger
        self.shouldFollow = shouldFollow
        self.canFollow = canFollow
        self.content = content
    }

    /// Stable id of the bottom sentinel. Scrolling to it (anchor `.bottom`)
    /// places the last real row fully on screen.
    private static var bottomAnchorID: String { "sticky-bottom-anchor" }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                content()
                Color.clear
                    .frame(height: 1)
                    .id(Self.bottomAnchorID)
                    .accessibilityHidden(true)
                    .onAppear { isNearBottom = true }
                    .onDisappear { isNearBottom = false }
            }
            .onAppear {
                // The list can mount already populated (a restored thread, or
                // returning from a backgrounded scene). Land at the bottom so the
                // newest content is visible. No animation.
                proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom)
            }
            .onChange(of: followTrigger) {
                guard followTrigger != nil, shouldFollow(isNearBottom) else {
                    return
                }
                guard canFollow() else {
                    hasPendingFollow = true
                    return
                }
                hasPendingFollow = false
                proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom)
            }
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in
                guard hasPendingFollow, canFollow() else {
                    return
                }
                hasPendingFollow = false
                proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom)
            }
            .onChange(of: forceFollowTrigger) {
                guard forceFollowTrigger != nil else {
                    return
                }
                hasPendingFollow = false
                proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom)
            }
        }
    }
}

private struct ChatComposerView: View {
    var viewModel: ChatViewModel
    @State private var selectedPhotoItem: PhotosPickerItem?
    @State private var importsFiles = false
    @State private var handledFocusRequestID: UUID?
    @FocusState private var isComposerFocused: Bool

    // While a turn runs the main composer doubles as the steer input: an empty
    // box → Stop, text in the box → Steer. When idle it sends a normal message.
    private var hasComposerText: Bool {
        !viewModel.draftText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var sendButtonEnabled: Bool {
        viewModel.isStreaming || viewModel.canSendDraft
    }

    private var actionButtonImage: String {
        guard viewModel.isStreaming else { return "arrow.up" }
        return hasComposerText ? "arrow.up" : "stop.fill"
    }

    private var actionButtonIdentifier: String {
        guard viewModel.isStreaming else { return "chat-send-button" }
        return hasComposerText ? "chat-steer-button" : "chat-stop-button"
    }

    var body: some View {
        VStack(spacing: 8) {
            // Steering sends text only, so attachments can't ride along on a
            // steer. Hide the attachment UI while a turn runs to avoid a picked
            // file being silently dropped from the steer and then sent with the
            // next normal message.
            if !viewModel.isStreaming, !viewModel.draftAttachments.isEmpty {
                DraftAttachmentStrip(viewModel: viewModel)
            }
            if let steerError = viewModel.steerErrorMessage {
                Text(steerError)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("chat-steer-error")
            }
            if let stopWarning = viewModel.stopWarningMessage {
                Text(stopWarning)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("chat-stop-warning")
            }
            HStack(alignment: .bottom, spacing: 4) {
                if !viewModel.isStreaming {
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

                    // The composer's TextField only accepts text pastes, so a
                    // copied screenshot or photo needs an explicit paste
                    // target. PasteButton reads the pasteboard without the
                    // system paste-permission prompt and disables itself while
                    // the pasteboard holds no image.
                    PasteButton(payloadType: PastedChatImage.self) { images in
                        Task { @MainActor in
                            for image in images {
                                await viewModel.addImageData(
                                    image.data,
                                    filename: "\(UUID().uuidString).\(image.filenameExtension)",
                                    mimeType: image.mimeType
                                )
                            }
                        }
                    }
                    .labelStyle(.iconOnly)
                    .buttonBorderShape(.capsule)
                    .frame(height: 36)
                    .accessibilityLabel("Paste Image")
                    .accessibilityIdentifier("chat-paste-button")
                }

                TextField(
                    viewModel.isStreaming ? "Steer the assistant" : "Message",
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
                    .focused($isComposerFocused)

                Button {
                    if viewModel.isStreaming {
                        if hasComposerText {
                            Task { await viewModel.sendSteerDraft() }
                        } else {
                            Task { await viewModel.stopTurn() }
                        }
                    } else {
                        Task { await viewModel.sendDraft() }
                    }
                } label: {
                    Image(systemName: actionButtonImage)
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.white)
                        .frame(width: 30, height: 30)
                        .background(sendButtonEnabled ? Color.accentColor : Color.secondary.opacity(0.4))
                        .clipShape(Circle())
                }
                .buttonStyle(.plain)
                .disabled(!viewModel.isStreaming && !viewModel.canSendDraft)
                .padding(.vertical, 3)
                .accessibilityIdentifier(actionButtonIdentifier)
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
        .onChange(of: viewModel.composerFocusRequestID, initial: true) { _, requestID in
            guard let requestID, handledFocusRequestID != requestID else {
                return
            }
            handledFocusRequestID = requestID
            isComposerFocused = true
        }
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

/// An image imported from the system pasteboard via the composer's
/// `PasteButton`. Formats the backend accepts are uploaded as-is; anything
/// else `UIImage` can decode (HEIC from Photos, TIFF via Universal Clipboard,
/// …) is transcoded to JPEG, mirroring the photo picker's HEIC handling.
/// Representations are tried in declaration order, so the pass-through
/// formats win over the generic transcoding fallback.
struct PastedChatImage: Transferable, Equatable {
    let data: Data
    let mimeType: String
    let filenameExtension: String

    static var transferRepresentation: some TransferRepresentation {
        DataRepresentation(importedContentType: .png) {
            PastedChatImage(data: $0, mimeType: "image/png", filenameExtension: "png")
        }
        DataRepresentation(importedContentType: .jpeg) {
            PastedChatImage(data: $0, mimeType: "image/jpeg", filenameExtension: "jpg")
        }
        DataRepresentation(importedContentType: .gif) {
            PastedChatImage(data: $0, mimeType: "image/gif", filenameExtension: "gif")
        }
        DataRepresentation(importedContentType: .webP) {
            PastedChatImage(data: $0, mimeType: "image/webp", filenameExtension: "webp")
        }
        DataRepresentation(importedContentType: .image) {
            try PastedChatImage.transcodedToJPEG(data: $0)
        }
    }

    static func transcodedToJPEG(data: Data) throws -> PastedChatImage {
        guard let image = UIImage(data: data),
              let jpegData = image.jpegData(compressionQuality: 0.9)
        else {
            throw ChatAPIError.validation("Pasted image format is not supported.")
        }
        return PastedChatImage(data: jpegData, mimeType: "image/jpeg", filenameExtension: "jpg")
    }
}

private struct DraftAttachmentStrip: View {
    var viewModel: ChatViewModel

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(viewModel.draftAttachments) { attachment in
                    VStack(alignment: .leading, spacing: 2) {
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
                        if let error = attachment.errorMessage {
                            Text(error)
                                .foregroundStyle(.red)
                                .lineLimit(2)
                                .accessibilityIdentifier("draft-attachment-error-\(attachment.id)")
                        }
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

/// Presentation-driven connection affordance. Each non-`.live` sync state renders
/// a distinct symbol/label/identifier and its own tap action, instead of
/// collapsing degraded/offline/authRequired into one false "no connection" icon.
/// `.live` and `.suspended` (background) render nothing.
private struct LiveUpdatesIndicator: View {
    var viewModel: ChatViewModel

    private struct Style {
        let systemImage: String
        let tint: Color
        let label: String
        let identifier: String
        let action: (ChatViewModel) -> Void
    }

    private var style: Style? {
        switch viewModel.syncPresentation {
        case .live, .suspended:
            return nil
        case .syncing:
            return Style(
                systemImage: "arrow.triangle.2.circlepath",
                tint: .secondary,
                label: "Syncing live updates.",
                identifier: "chat-live-updates-syncing",
                action: { model in Task { await model.requestManualReconnect() } }
            )
        case .degraded:
            return Style(
                systemImage: "wifi.exclamationmark",
                tint: .orange,
                label: "Live updates degraded. Tap to reconnect.",
                identifier: "chat-live-updates-degraded",
                action: { model in Task { await model.requestManualReconnect() } }
            )
        case .offline:
            return Style(
                systemImage: "wifi.slash",
                tint: .orange,
                label: "No connection. Tap to reconnect.",
                identifier: "chat-live-updates-offline",
                action: { model in Task { await model.requestManualReconnect() } }
            )
        case .authRequired:
            return Style(
                systemImage: "person.crop.circle.badge.exclamationmark",
                tint: .red,
                label: "Sign-in required. Tap to sign in.",
                identifier: "chat-live-updates-auth-required",
                action: { model in model.requestReauthentication() }
            )
        }
    }

    var body: some View {
        if let style {
            Button {
                style.action(viewModel)
            } label: {
                if viewModel.syncPresentation == .syncing {
                    ProgressView()
                        .controlSize(.small)
                } else {
                    Image(systemName: style.systemImage)
                        .foregroundStyle(style.tint)
                }
            }
            .accessibilityLabel(style.label)
            .accessibilityIdentifier(style.identifier)
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

/// A small non-modal banner above the composer for the thread-scoped inline
/// surfaces the classifier routes here (§4.5): a 403 access-changed, a 404-gone
/// conversation, a user-initiated read that failed, or a rate-limited action.
/// Distinct from the modal error alert — it never blocks the UI and is dismissible.
private struct ThreadInlineBanner: View {
    var viewModel: ChatViewModel

    var body: some View {
        if let message = viewModel.threadInlineMessage {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "exclamationmark.circle")
                    .foregroundStyle(.orange)
                Text(message)
                    .font(.caption)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button {
                    viewModel.threadInlineMessage = nil
                } label: {
                    Image(systemName: "xmark")
                        .font(.caption)
                }
                .accessibilityLabel("Dismiss")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color.orange.opacity(0.12))
            .accessibilityIdentifier("chat-thread-inline-banner")
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

/// Bounds the layout cost of a single rendered message.
///
/// A message can be arbitrarily large (a long assistant answer, or an expanded
/// tool result that dumped a whole document), and a large message is not just
/// many top-level blocks — it can be one block with thousands of children (a
/// huge list/table) or one enormous `Text` (a giant code block or unbroken
/// string). Rendering any of these unbounded builds a view tree the enclosing
/// ScrollView must size in one main-thread pass; under the stack layout's
/// multi-proposal sizing that overruns the scene-update watchdog (0x8BADF00D)
/// and the app is killed. See docs/design/ios-chat-layout-watchdog-crash.md.
///
/// `leafBudget` caps the total leaf views (paragraphs, list items, table rows,
/// …) realized; content beyond it is paged in behind "Show more". `textCharCap`
/// clamps any single `Text` so one giant string can't dominate a layout pass on
/// its own (truncation is marked inline with an ellipsis, never silent).
enum MarkdownRenderBudget {
    static let leafBudget = 150
    static let textCharCap = 2000
    static let maxTableColumns = 16
    /// Deeply nested lists/quotes are cheap by leaf *count* but layout cost grows
    /// super-linearly with nesting *depth* (each level re-proposes sizes), so cap
    /// the depth independently and collapse anything deeper to an ellipsis.
    static let maxNestingDepth = 6
    /// Raw markdown parsed per page. Bounds parse cost (and, transitively, any
    /// shape we haven't reasoned about) before structural bounding even runs.
    static let charsPerPage = 16384
    /// Upper bound on "Show more" pages. Each page grows the parsed prefix and the
    /// leaf/text budget, so without a ceiling repeated taps could rebuild the very
    /// thousands-node tree this budget exists to prevent — re-arming the watchdog.
    /// At the cap the affordance is hidden and the tail stays truncated (ellipsis).
    /// The worst realized size is therefore `maxPages * leafBudget` leaves /
    /// `maxPages * charsPerPage` parsed characters.
    static let maxPages = 6

    enum RenderPlan {
        // `canPageMore`: more content is revealable by tapping "Show more" (the
        // source prefix / leaf budget / per-Text cap all grow with pages).
        // `truncatedPermanently`: content was dropped that paging can NEVER reveal
        // (table columns past `maxTableColumns`, nesting past `maxNestingDepth`),
        // so it gets a static truncation indicator instead of a no-op button.
        case markdown(blocks: [NativeMarkdownBlock], canPageMore: Bool, truncatedPermanently: Bool)
        case plain(text: String, canPageMore: Bool)
    }

    /// A bounded prefix of `markdown` for `pages` worth of budget, plus whether the
    /// source was truncated. O(prefix), never O(markdown.count): `body` is
    /// re-evaluated many times per message (scroll, animation, stack layout), so an
    /// O(n) scan over a multi-hundred-KB message here is itself a main-thread hang.
    /// `prefix` stops after charLimit+1 Characters.
    static func boundedSource(_ markdown: String, pages: Int) -> (text: String, truncated: Bool) {
        let charLimit = charsPerPage * max(1, min(pages, maxPages))
        let head = markdown.prefix(charLimit + 1)
        let truncated = head.count > charLimit
        return (truncated ? String(head.prefix(charLimit)) : markdown, truncated)
    }

    /// The single choke point that bounds every layout cost — parse size (char
    /// prefix), structure (leaf budget + table cells), nesting depth, and
    /// single-`Text` length. The plain-vs-markdown decision is made on the FIRST
    /// page's prefix (not the full message and not the current page), so the
    /// syntax scan stays bounded AND the render mode can't flip across pages (which
    /// would otherwise make "Show more" *shrink* already-visible text). Shared by
    /// `NativeMarkdownView` and the layout-budget tests.
    static func renderPlan(for markdown: String, pages: Int) -> RenderPlan {
        let clampedPages = max(1, min(pages, maxPages))
        // Mode is decided once, on the first page, so paging only ever reveals
        // more — it never re-classifies the message.
        let modeIsMarkdown = containsMarkdownSyntax(boundedSource(markdown, pages: 1).text)
        let source = boundedSource(markdown, pages: clampedPages)
        guard modeIsMarkdown else {
            return .plain(text: source.text, canPageMore: source.truncated)
        }
        let parsed = NativeMarkdownRenderer.blocks(from: source.text)
        // Grow the per-Text cap with pages too, so "Show more" reveals the rest of
        // a single oversized block (a long paragraph or code dump), not just more
        // structure.
        let bounded = boundedMarkdownBlocks(
            parsed,
            leafBudget: leafBudget * clampedPages,
            textCharCap: textCharCap * clampedPages,
            depth: 0
        )
        return .markdown(
            blocks: bounded.blocks,
            canPageMore: bounded.outcome.canPageMore || source.truncated,
            truncatedPermanently: bounded.outcome.truncatedPermanently
        )
    }
}

private func cappedMarkdownText(_ text: String, cap: Int) -> (text: String, truncated: Bool) {
    guard text.count > cap else {
        return (text, false)
    }
    return (String(text.prefix(cap)) + "…", true)
}

/// Approximate number of leaf views a block realizes, used to spend the budget.
private func markdownLeafCount(_ block: NativeMarkdownBlock) -> Int {
    switch block {
    case .paragraph, .fallback, .heading, .thematicBreak, .codeBlock:
        return 1
    case .unorderedList(let items), .orderedList(_, let items):
        return items.reduce(0) { $0 + 1 + $1.blocks.reduce(0) { $0 + markdownLeafCount($1) } }
    case .blockQuote(let blocks):
        return max(1, blocks.reduce(0) { $0 + markdownLeafCount($1) })
    case .table(let header, let rows):
        return (rows.count + 1) * max(1, header.count)
    }
}

/// Why content was dropped during bounding. `canPageMore` means tapping "Show
/// more" (which grows the source/leaf/text budgets) would reveal more.
/// `truncatedPermanently` means it never would — table columns past
/// `maxTableColumns` or nesting past `maxNestingDepth` are clamped regardless of
/// pages — so the view shows a static truncation indicator, not a no-op button.
struct MarkdownBudgetOutcome {
    var canPageMore = false
    var truncatedPermanently = false

    mutating func merge(_ other: MarkdownBudgetOutcome) {
        canPageMore = canPageMore || other.canPageMore
        truncatedPermanently = truncatedPermanently || other.truncatedPermanently
    }
}

/// Truncate a block tree to `leafBudget` leaves, capping single-`Text` content to
/// `textCharCap`. Returns the bounded blocks and an outcome distinguishing
/// pageable truncation (leaf/text budget) from permanent truncation (depth).
func boundedMarkdownBlocks(
    _ blocks: [NativeMarkdownBlock],
    leafBudget: Int,
    textCharCap: Int,
    depth: Int
) -> (blocks: [NativeMarkdownBlock], outcome: MarkdownBudgetOutcome) {
    if depth > MarkdownRenderBudget.maxNestingDepth {
        // Nesting depth is fixed; paging never reveals the collapsed subtree.
        var outcome = MarkdownBudgetOutcome()
        outcome.truncatedPermanently = !blocks.isEmpty
        return (blocks.isEmpty ? [] : [.paragraph("…")], outcome)
    }
    var remaining = leafBudget
    var result: [NativeMarkdownBlock] = []
    var outcome = MarkdownBudgetOutcome()
    for block in blocks {
        if remaining <= 0 {
            outcome.canPageMore = true
            break
        }
        let bounded = boundOneMarkdownBlock(block, leafBudget: remaining, textCharCap: textCharCap, depth: depth)
        result.append(bounded.block)
        remaining -= bounded.cost
        outcome.merge(bounded.outcome)
    }
    if result.count < blocks.count {
        outcome.canPageMore = true
    }
    return (result, outcome)
}

private func boundOneMarkdownBlock(
    _ block: NativeMarkdownBlock,
    leafBudget: Int,
    textCharCap: Int,
    depth: Int
) -> (block: NativeMarkdownBlock, cost: Int, outcome: MarkdownBudgetOutcome) {
    func pageable(_ truncated: Bool) -> MarkdownBudgetOutcome {
        var outcome = MarkdownBudgetOutcome()
        outcome.canPageMore = truncated
        return outcome
    }
    // A single-`Text` block clamped by the char cap is *pageable* (paging grows
    // `textCharCap`, see renderPlan), so the rest of an oversized paragraph/code
    // block stays reachable rather than stranded behind a bare ellipsis.
    switch block {
    case .paragraph(let text):
        let capped = cappedMarkdownText(text, cap: textCharCap)
        return (.paragraph(capped.text), 1, pageable(capped.truncated))
    case .fallback(let text):
        let capped = cappedMarkdownText(text, cap: textCharCap)
        return (.fallback(capped.text), 1, pageable(capped.truncated))
    case .heading(let level, let text):
        let capped = cappedMarkdownText(text, cap: textCharCap)
        return (.heading(level: level, text: capped.text), 1, pageable(capped.truncated))
    case .thematicBreak:
        return (.thematicBreak, 1, MarkdownBudgetOutcome())
    case .codeBlock(let language, let code):
        let capped = cappedMarkdownText(code, cap: textCharCap)
        return (.codeBlock(language: language, code: capped.text), 1, pageable(capped.truncated))
    case .unorderedList(let items):
        let bounded = boundedMarkdownItems(items, leafBudget: leafBudget, textCharCap: textCharCap, depth: depth)
        return (.unorderedList(bounded.items), bounded.cost, bounded.outcome)
    case .orderedList(let startIndex, let items):
        let bounded = boundedMarkdownItems(items, leafBudget: leafBudget, textCharCap: textCharCap, depth: depth)
        return (.orderedList(startIndex: startIndex, items: bounded.items), bounded.cost, bounded.outcome)
    case .blockQuote(let blocks):
        let bounded = boundedMarkdownBlocks(blocks, leafBudget: leafBudget, textCharCap: textCharCap, depth: depth + 1)
        let cost = max(1, bounded.blocks.reduce(0) { $0 + markdownLeafCount($1) })
        return (.blockQuote(bounded.blocks), cost, bounded.outcome)
    case .table(let header, let rows):
        // Bound BOTH dimensions: a wide table builds rows×cols cells in a Grid
        // under a horizontal ScrollView, which is super-linear to lay out.
        let columnCount = max(1, header.count)
        let keptColumns = min(columnCount, MarkdownRenderBudget.maxTableColumns)
        let maxRows = max(0, leafBudget / keptColumns - 1)
        var outcome = MarkdownBudgetOutcome()
        let keptRows = rows.prefix(maxRows).map { row in
            row.prefix(keptColumns).map { cell -> String in
                let capped = cappedMarkdownText(cell, cap: textCharCap)
                outcome.canPageMore = outcome.canPageMore || capped.truncated
                return capped.text
            }
        }
        let cappedHeader = header.prefix(keptColumns).map { cell -> String in
            let capped = cappedMarkdownText(cell, cap: textCharCap)
            outcome.canPageMore = outcome.canPageMore || capped.truncated
            return capped.text
        }
        // Rows grow with the leaf budget (pageable); columns are capped at
        // `maxTableColumns` regardless of pages (permanent), so a >16-column table
        // gets a truncation indicator rather than a no-op "Show more".
        if keptRows.count < rows.count {
            outcome.canPageMore = true
        }
        if keptColumns < columnCount {
            outcome.truncatedPermanently = true
        }
        return (.table(header: cappedHeader, rows: keptRows), (keptRows.count + 1) * keptColumns, outcome)
    }
}

private func boundedMarkdownItems(
    _ items: [NativeMarkdownListItem],
    leafBudget: Int,
    textCharCap: Int,
    depth: Int
) -> (items: [NativeMarkdownListItem], cost: Int, outcome: MarkdownBudgetOutcome) {
    var remaining = leafBudget
    var result: [NativeMarkdownListItem] = []
    var outcome = MarkdownBudgetOutcome()
    for item in items {
        if remaining <= 1 {
            outcome.canPageMore = true
            break
        }
        remaining -= 1
        let bounded = boundedMarkdownBlocks(item.blocks, leafBudget: remaining, textCharCap: textCharCap, depth: depth + 1)
        remaining -= bounded.blocks.reduce(0) { $0 + markdownLeafCount($1) }
        result.append(NativeMarkdownListItem(checkbox: item.checkbox, blocks: bounded.blocks))
        outcome.merge(bounded.outcome)
    }
    if result.count < items.count {
        outcome.canPageMore = true
    }
    let cost = max(1, result.reduce(0) { $0 + 1 + $1.blocks.reduce(0) { $0 + markdownLeafCount($1) } })
    return (result, cost, outcome)
}

private struct NativeMarkdownView: View {
    let markdown: String

    @State private var pages = 1

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            switch MarkdownRenderBudget.renderPlan(for: markdown, pages: pages) {
            case let .markdown(blocks, canPageMore, truncatedPermanently):
                ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                    MarkdownBlockView(block: block)
                }
                truncationAffordance(canPageMore: canPageMore, truncatedPermanently: truncatedPermanently)
            case let .plain(text, canPageMore):
                Text(text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                truncationAffordance(canPageMore: canPageMore, truncatedPermanently: false)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Trailing affordance for truncated content:
    /// - "Show more" only when paging would actually reveal more and we are below
    ///   the page ceiling (so repeated taps can't grow the realized tree without
    ///   bound — see maxPages).
    /// - Otherwise, a static indicator when content was dropped that paging cannot
    ///   reveal (columns/nesting) or that remains at the ceiling, so the tail is
    ///   never silently dropped behind a no-op control.
    @ViewBuilder
    private func truncationAffordance(canPageMore: Bool, truncatedPermanently: Bool) -> some View {
        let atCeiling = pages >= MarkdownRenderBudget.maxPages
        if canPageMore, !atCeiling {
            Button {
                pages += 1
            } label: {
                Label("Show more", systemImage: "chevron.down")
                    .font(.caption)
            }
            .buttonStyle(.borderless)
            .accessibilityIdentifier("markdown-show-more")
        } else if truncatedPermanently || (canPageMore && atCeiling) {
            Label("Message truncated", systemImage: "ellipsis.circle")
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("markdown-truncated")
        }
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
