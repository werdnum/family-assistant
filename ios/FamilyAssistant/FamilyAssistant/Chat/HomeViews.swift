import SwiftUI

private struct HomeThreadsKey: EnvironmentKey {
    static let defaultValue: HomeThreadsStore? = nil
}

extension EnvironmentValues {
    /// Home's threads, present inside Home's navigation stack. Message bubbles
    /// read it to draw a `start_thread` call as the thread's card.
    var homeThreads: HomeThreadsStore? {
        get { self[HomeThreadsKey.self] }
        set { self[HomeThreadsKey.self] = newValue }
    }
}

/// The Chat tab's root: Home when the server gives the caller one, otherwise
/// the conversation list exactly as before threads.
struct ChatEntryView: View {
    let authManager: AuthManager
    let route: ChatRoute

    @State private var discovery: HomeDiscovery
    @State private var hasRefreshed = false
    /// The conversation list shown while the lookup has failed, so what the
    /// user typed there can move into Home if Retry finds it.
    @State private var provisionalDraft = ProvisionalDraftSource()
    /// What the conversation list held when Retry found Home.
    @State private var inheritedDraft: ChatDraft?

    init(authManager: AuthManager, route: ChatRoute) {
        self.authManager = authManager
        self.route = route
        _discovery = State(initialValue: HomeDiscovery(
            serverURL: authManager.validatedServerURL()?.absoluteString,
            lookUp: { try await ChatAPIClient(authManager: authManager).getHome() },
            reportFailure: { ErrorReporter.shared.report($0, component: "Chat.home") }
        ))
    }

    var body: some View {
        Group {
            switch discovery.state {
            case .unknown:
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .disabled, .failed:
                // One view for both, so a failed lookup that resolves to "off"
                // keeps the same conversation list. While the lookup has failed
                // the list keeps chat usable but is not given the route: a routed
                // prompt or shared file waits for whichever root the retry
                // settles on (`HomeDiscovery.deliverableRoute`).
                ChatRootView(
                    authManager: authManager,
                    route: HomeDiscovery.deliverableRoute(route, state: discovery.state),
                    draftSource: provisionalDraft
                )
                .safeAreaInset(edge: .top, spacing: 0) {
                    if discovery.state == .failed {
                        // A snapshot taken mid-upload would carry an attachment
                        // that never finishes, so Retry waits for uploads.
                        HomeLookupFailedBanner(
                            holdsRoute: route != ChatRoute(),
                            canRetry: !provisionalDraft.hasUploadInProgress
                        ) {
                            Task { await retry() }
                        }
                    }
                }
            case let .enabled(homeConversationID):
                HomeRootView(
                    authManager: authManager,
                    route: route,
                    homeConversationID: homeConversationID,
                    inheritedDraft: inheritedDraft
                )
                .id(homeConversationID)
            }
        }
        .task {
            guard !hasRefreshed else {
                return
            }
            hasRefreshed = true
            await discovery.refresh()
        }
        .onChange(of: authManager.authRequired) { wasRequired, isRequired in
            // Signing in again in place may be as someone else, whose Home is
            // not the one showing. Start over: tear the root down, look Home up
            // afresh, and carry nothing across.
            if wasRequired, !isRequired {
                provisionalDraft.model = nil
                inheritedDraft = nil
                Task { await discovery.restart() }
            }
        }
    }

    private func retry() async {
        guard !provisionalDraft.hasUploadInProgress else {
            return
        }
        let draft = provisionalDraft.model?.currentDraft
        await discovery.retryIfFailed()
        if case .enabled = discovery.state {
            inheritedDraft = draft
        }
    }
}

/// The conversation model behind the fallback conversation list, held weakly
/// so its draft can be read when Retry replaces it with Home.
@MainActor
@Observable
final class ProvisionalDraftSource {
    weak var model: ChatViewModel?

    var hasUploadInProgress: Bool {
        model?.draftAttachments.contains { $0.uploadState == .uploading } ?? false
    }
}

/// Home's first moments: the route is applied as soon as Home itself is ready,
/// while the thread list (which only the cards need) loads alongside.
enum HomeStartup {
    @MainActor
    static func run(
        bootstrap: () async -> Void,
        refreshThreads: @escaping @MainActor () async -> Void,
        applyRoute: () async -> Void
    ) async {
        let threads = Task { @MainActor in await refreshThreads() }
        await bootstrap()
        await applyRoute()
        await threads.value
    }
}

/// A routed prompt is held until Home can take it; say so rather than hold it
/// silently.
private struct HeldRoutedPromptNote: View {
    var viewModel: ChatViewModel

    var body: some View {
        if let prompt = viewModel.heldRoutedPrompts.first {
            Label("Will send when ready: “\(String(prompt.prefix(80)))”", systemImage: "clock")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.bar)
                .accessibilityIdentifier("held-routed-prompt")
        }
    }
}

/// Atop Home: what is running right now, however it was started. A thread the
/// user started has no card in Home's transcript, and one with a card scrolls
/// away. One thread opens directly; several open the chat list (nil).
private struct WorkingThreadsBar: View {
    let threads: [ChatThreadSummary]
    let onSelect: (ChatThreadSummary?) -> Void

    var body: some View {
        if !threads.isEmpty {
            Button {
                onSelect(threads.count == 1 ? threads[0] : nil)
            } label: {
                HStack(spacing: 8) {
                    ThreadStateDot(state: .working)
                    Text(verbatim: threads.count == 1 ? threads[0].title : "\(threads.count) threads working")
                        .font(.footnote.weight(.semibold))
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: "chevron.right")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(.bar)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(
                threads.count == 1 ? "\(threads[0].title), Working" : "\(threads.count) threads working"
            )
            .accessibilityIdentifier("home-working-threads")
        }
    }
}

/// Shown when the load that describes Home or a conversation over it failed:
/// sending stays closed until it loads, so the user needs a way to try again.
private struct ConversationLoadRetryBanner: View {
    var viewModel: ChatViewModel

    var body: some View {
        if viewModel.conversationMetadataLoadFailed {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text("Couldn’t load this conversation. Sending is paused until it loads.")
                    .font(.footnote)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button("Retry") {
                    Task { await viewModel.retryConversationLoad() }
                }
                .font(.footnote.weight(.semibold))
                .disabled(viewModel.isLoadingMessages)
                .accessibilityIdentifier("conversation-load-retry")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(.bar)
            .accessibilityIdentifier("conversation-load-failed")
        }
    }
}

private struct HomeLookupFailedBanner: View {
    let holdsRoute: Bool
    let canRetry: Bool
    let retry: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(holdsRoute ? "Couldn’t check for Home. Your request will open once it’s checked." : "Couldn’t check for Home.")
                .font(.footnote)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button("Retry", action: retry)
                .font(.footnote.weight(.semibold))
                .disabled(!canRetry)
                .accessibilityIdentifier("home-lookup-retry")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.bar)
        .accessibilityIdentifier("home-lookup-failed")
    }
}

/// Home: the single landing stream, with threads and other conversations
/// pushed over it. Home stays mounted underneath, so going back returns to it
/// where the reader left it.
struct HomeRootView: View {
    @Environment(SharedAttachmentInbox.self) private var sharedAttachmentInbox
    @Environment(NotificationManager.self) private var notificationManager
    @State private var viewModel: ChatViewModel
    @State private var store: HomeThreadsStore
    /// The conversations open over Home.
    @State private var path: [String]
    @State private var showsSwitcher = false
    let authManager: AuthManager
    let route: ChatRoute

    init(
        authManager: AuthManager,
        route: ChatRoute,
        homeConversationID: String,
        inheritedDraft: ChatDraft? = nil
    ) {
        self.authManager = authManager
        self.route = route
        let model = ChatViewModel(
            authManager: authManager,
            conversationID: homeConversationID,
            role: .home
        )
        if let inheritedDraft {
            model.inheritDraft(inheritedDraft)
        }
        _viewModel = State(initialValue: model)
        _store = State(initialValue: HomeThreadsStore(
            homeConversationID: homeConversationID,
            apiClient: ChatAPIClient(authManager: authManager)
        ))
        switch HomeRouteAction.resolve(route, homeConversationID: homeConversationID) {
        case let .open(conversationID):
            _path = State(initialValue: [conversationID])
        case .showHome, .composeInHome, .sendInHome:
            _path = State(initialValue: [])
        }
    }

    var body: some View {
        NavigationStack(path: $path) {
            ChatThreadView(viewModel: viewModel, keepsScrollPositionOnReappear: true)
                .safeAreaInset(edge: .top, spacing: 0) {
                    VStack(spacing: 0) {
                        ConversationLoadRetryBanner(viewModel: viewModel)
                        HeldRoutedPromptNote(viewModel: viewModel)
                        WorkingThreadsBar(threads: store.workingThreads) { thread in
                            if let thread {
                                open(thread.conversationID)
                            } else {
                                showsSwitcher = true
                            }
                        }
                    }
                }
                .navigationTitle("Home")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { homeToolbar }
                .navigationDestination(for: String.self) { conversationID in
                    PushedConversationView(
                        conversationID: conversationID,
                        authManager: authManager,
                        store: store
                    )
                }
        }
        .alert(
            store.actionFailure?.title ?? "",
            isPresented: Binding(
                get: { store.actionFailure != nil },
                set: { if !$0 { store.actionFailure = nil } }
            ),
            presenting: store.actionFailure
        ) { _ in
            Button("OK", role: .cancel) {}
        } message: { failure in
            Text(verbatim: failure.message)
        }
        .environment(\.homeThreads, store)
        .sheet(isPresented: $showsSwitcher) {
            ThreadSwitcherView(
                store: store,
                homeModel: viewModel,
                homePreview: homePreview,
                onSelect: { conversationID in
                    showsSwitcher = false
                    open(conversationID)
                }
            )
        }
        .sheet(item: Binding(
            get: { store.presentedSheet },
            set: { store.presentedSheet = $0 }
        )) { sheet in
            threadSheet(sheet)
        }
        .task {
            // Only the path binding and Home's id are captured: the store holds
            // this callback, so capturing the view would keep the store and
            // Home's model alive after Home is removed.
            let path = $path
            let homeConversationID = store.homeConversationID
            store.homeModel = viewModel
            store.openConversation = { conversationID in
                Self.open(conversationID, homeConversationID: homeConversationID, path: path)
            }
            viewModel.onListRefresh = { [weak store] in
                await store?.refreshForActivity()
            }
            await HomeStartup.run(
                bootstrap: { await viewModel.bootstrap() },
                refreshThreads: { await store.refreshThreads() },
                applyRoute: {
                    await applyInHome(route)
                    consumePendingPushHint()
                }
            )
        }
        .onChange(of: route) { _, newRoute in
            apply(newRoute)
        }
        .onChange(of: showsSwitcher || store.presentedSheet != nil) { _, covered in
            viewModel.setCoveredBySheet(covered)
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

    @ToolbarContentBuilder
    private var homeToolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .topBarLeading) {
            Button {
                store.presentedSheet = .newThread
            } label: {
                Label("New Thread", systemImage: "square.and.pencil")
            }
            .accessibilityIdentifier("home-new-thread")
            if viewModel.syncPresentation != .live {
                LiveUpdatesIndicator(viewModel: viewModel)
            }
        }
        ToolbarItem(placement: .principal) {
            HomeTitlePill()
        }
        ToolbarItemGroup(placement: .topBarTrailing) {
            if viewModel.isConversationDescribed {
                IntelligencePickerView(viewModel: viewModel)
            }
            ThreadsButton(waitingCount: store.waitingCount) {
                showsSwitcher = true
            }
        }
    }

    @ViewBuilder
    private func threadSheet(_ sheet: HomeThreadSheet) -> some View {
        switch sheet {
        case .newThread:
            StartThreadSheet(mode: .newThread, homeModel: viewModel, store: store) { thread in
                store.presentedSheet = nil
                open(thread.conversationID)
            }
        case .sendAsThread:
            StartThreadSheet(mode: .sendAsThread, homeModel: viewModel, store: store)
        case let .continueFrom(anchorMessageID, quote):
            StartThreadSheet(
                mode: .continueFrom(anchorMessageID: anchorMessageID, quote: quote),
                homeModel: viewModel,
                store: store
            ) { thread in
                store.presentedSheet = nil
                open(thread.conversationID)
            }
        case let .move(messageID, quote):
            MoveToThreadSheet(messageID: messageID, quote: quote, homeModel: viewModel, store: store)
        }
    }

    private var homePreview: String? {
        viewModel.messages.last { !$0.text.isEmpty }.map { String($0.text.prefix(200)) }
    }

    private func open(_ conversationID: String) {
        Self.open(conversationID, homeConversationID: store.homeConversationID, path: $path)
    }

    private static func open(
        _ conversationID: String,
        homeConversationID: String,
        path: Binding<[String]>
    ) {
        if conversationID == homeConversationID {
            path.wrappedValue = []
        } else if path.wrappedValue.last != conversationID {
            path.wrappedValue = [conversationID]
        }
    }

    private func apply(_ route: ChatRoute) {
        switch HomeRouteAction.resolve(route, homeConversationID: store.homeConversationID) {
        case .showHome, .composeInHome, .sendInHome:
            path = []
            Task { await applyInHome(route) }
        case let .open(conversationID):
            open(conversationID)
        }
    }

    /// The parts of a route that act on Home itself: a shared hand-off goes
    /// into Home's composer, a prompt is sent there, and a request for a new
    /// chat brings the keyboard up there. Safe while Home is
    /// still bootstrapping: the model holds a routed prompt until Home has
    /// loaded and adopted its profile, then sends it once. The other route
    /// kinds only change the navigation path, which needs no loaded Home.
    private func applyInHome(_ route: ChatRoute) async {
        if let batchID = route.sharedAttachmentBatchID,
           let batch = sharedAttachmentInbox.consume(batchID: batchID)
        {
            await viewModel.addSharedAttachments(batch)
        }
        switch HomeRouteAction.resolve(route, homeConversationID: store.homeConversationID) {
        case let .sendInHome(prompt):
            await viewModel.sendPromptInPlace(prompt)
        case .composeInHome:
            viewModel.requestComposerFocus()
        case .showHome, .open:
            break
        }
    }

    private func consumePendingPushHint() {
        guard let hint = notificationManager.pendingPushHint else {
            return
        }
        viewModel.pushHintReceived(conversationID: hint.conversationID)
        if let pushed = store.pushedModel, pushed.conversationID == hint.conversationID {
            pushed.pushHintReceived(conversationID: hint.conversationID)
        }
        notificationManager.clearPendingPushHint()
        Task { await store.refreshThreads() }
    }
}

private struct HomeTitlePill: View {
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "house.fill")
                .font(.footnote)
                .frame(width: 26, height: 26)
                .background(Color(.tertiarySystemFill), in: Circle())
            VStack(alignment: .leading, spacing: 0) {
                Text("Family Assistant")
                    .font(.subheadline.weight(.semibold))
                Text("Home")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Family Assistant, Home")
        .accessibilityIdentifier("home-title")
    }
}

private struct ThreadsButton: View {
    let waitingCount: Int
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            // The count sits beside the icon rather than over its corner: a
            // toolbar button clips anything drawn outside its own bounds.
            HStack(spacing: 4) {
                Image(systemName: "list.bullet.rectangle")
                if waitingCount > 0 {
                    Text(waitingCount > 99 ? "99+" : "\(waitingCount)")
                        .font(.caption2.bold())
                        .foregroundStyle(.white)
                        .padding(.horizontal, 5)
                        .frame(minWidth: 18, minHeight: 18)
                        .background(ThreadStateStyle.waitingColor, in: Capsule())
                        .accessibilityIdentifier("threads-waiting-badge")
                }
            }
        }
        .accessibilityIdentifier("threads-button")
        .accessibilityLabel(waitingCount > 0 ? "Chats, \(waitingCount) waiting on you" : "Chats")
    }
}

/// A conversation pushed over Home: a thread, or an ordinary conversation from
/// before threads. An ordinary one stays on its profile, since switching would
/// start a new conversation outside Home and its threads.
private struct PushedConversationView: View {
    @State private var viewModel: ChatViewModel
    let store: HomeThreadsStore
    let authManager: AuthManager

    init(conversationID: String, authManager: AuthManager, store: HomeThreadsStore) {
        self.store = store
        self.authManager = authManager
        _viewModel = State(initialValue: ChatViewModel(
            authManager: authManager,
            conversationID: conversationID,
            role: .pushed
        ))
    }

    /// The store's copy first: it is the one activity refreshes and "Mark
    /// done" updates. The conversation's own description covers a thread the
    /// list has not caught up with.
    private var thread: ChatThreadSummary? {
        store.thread(conversationID: viewModel.conversationID) ?? viewModel.threadSummary
    }

    var body: some View {
        ChatThreadView(viewModel: viewModel)
            .safeAreaInset(edge: .top, spacing: 0) {
                ConversationLoadRetryBanner(viewModel: viewModel)
            }
            .navigationTitle(thread?.title ?? "Chat")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if let thread {
                    ToolbarItem(placement: .principal) {
                        ThreadNavigationTitle(thread: thread)
                    }
                }
                if viewModel.syncPresentation != .live {
                    ToolbarItem(placement: .topBarLeading) {
                        LiveUpdatesIndicator(viewModel: viewModel)
                    }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    if viewModel.isConfirmedOrdinaryConversation, thread == nil {
                        ConversationShareToolbar(
                            authManager: authManager,
                            conversationID: viewModel.shareableConversationID
                        )
                    }
                    // Its tiers are the profile's, which is known only once the
                    // conversation is described.
                    if viewModel.isConversationDescribed {
                        IntelligencePickerView(viewModel: viewModel)
                    }
                    if let thread, thread.canMarkDone {
                        ThreadActionsMenu(thread: thread, store: store)
                    }
                }
            }
            .task {
                store.pushedModel = viewModel
                viewModel.onMarkedRead = { [weak store] conversationID, unreadCount in
                    store?.applyUnreadCount(unreadCount, for: conversationID)
                }
                await viewModel.bootstrap()
            }
            .onAppear { viewModel.setScreenVisible(true) }
            .onDisappear { viewModel.setScreenVisible(false) }
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

private struct ThreadNavigationTitle: View {
    let thread: ChatThreadSummary

    var body: some View {
        VStack(spacing: 0) {
            Text(thread.title)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
            Text("Thread · from Home \(thread.createdAt.formatted(date: .omitted, time: .shortened))")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("thread-title")
    }
}

/// Atop a thread: where it came from and what the assistant asked of it. Drawn
/// from the thread's record, never part of the thread's own history.
struct ThreadOriginBanner: View {
    @Environment(\.homeThreads) private var homeThreads
    var viewModel: ChatViewModel

    private static let seedPreviewLimit = 400

    var body: some View {
        if let thread = viewModel.threadSummary {
            VStack(alignment: .leading, spacing: 4) {
                Label("Started from Home", systemImage: "arrow.turn.down.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                // A continued thread quotes the message it grew from while Home
                // still holds it. Moved away since, it simply has no quote.
                if let quote = homeThreads?.anchorQuote(for: thread) {
                    Text(verbatim: "“\(String(quote.prefix(Self.seedPreviewLimit)))”")
                        .font(.footnote)
                        .italic()
                        .lineLimit(4)
                }
                if let seed = thread.seedRequest, !seed.isEmpty {
                    Text("Assistant asked: \(String(seed.prefix(Self.seedPreviewLimit)))")
                        .font(.footnote)
                        .lineLimit(4)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(Color(.separator), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("thread-origin-banner")
        }
    }
}

/// A pushed thread's actions. "Mark done" closes it as the user; it reopens by
/// itself when anything new arrives in it.
private struct ThreadActionsMenu: View {
    let thread: ChatThreadSummary
    let store: HomeThreadsStore

    var body: some View {
        Menu {
            Button {
                Task { await store.markDone(thread.conversationID) }
            } label: {
                Label("Mark Done", systemImage: "checkmark.circle")
            }
            .disabled(store.markingDone.contains(thread.conversationID))
            .accessibilityIdentifier("thread-mark-done")
        } label: {
            Label("Thread Actions", systemImage: "ellipsis.circle")
        }
        .accessibilityIdentifier("thread-actions-menu")
    }
}

/// Atop a Home message that echoes a finished thread's outcome: where it came
/// from, and the way into that thread.
struct EchoedThreadHeader: View {
    let threadID: String
    let store: HomeThreadsStore?

    var body: some View {
        let title = store?.thread(conversationID: threadID)?.title
        let text = title.map { "From thread: \($0)" } ?? "From a thread"
        if let store {
            Button {
                store.open(threadID)
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "arrow.turn.down.left")
                    Text(text)
                        .lineLimit(1)
                    Image(systemName: "chevron.right")
                        .font(.caption2)
                }
                .font(.caption.weight(.semibold))
                .foregroundStyle(Color.accentColor)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(text)
            .accessibilityHint("Opens the thread")
            .accessibilityIdentifier("echo-header-\(threadID)")
        } else {
            Label(text, systemImage: "arrow.turn.down.left")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
        }
    }
}

/// The caller's unread count on a card or switcher row; nothing when zero.
struct UnreadBadge: View {
    let count: Int

    var body: some View {
        if count > 0 {
            Text(count > 99 ? "99+" : "\(count)")
                .font(.caption2.bold())
                .foregroundStyle(.white)
                .padding(.horizontal, 5)
                .frame(minWidth: 18, minHeight: 18)
                .background(Color.accentColor, in: Capsule())
                .accessibilityLabel("\(count) unread")
                .accessibilityIdentifier("unread-badge")
        }
    }
}

/// Colours for `ThreadPresentationState`; nil is a thread not loaded yet.
enum ThreadStateStyle {
    static let waitingColor = Color(red: 0.49, green: 0.36, blue: 1.0)
    static let workingColor = Color(red: 0.96, green: 0.72, blue: 0.24)

    static func color(for state: ThreadPresentationState?) -> Color {
        switch state {
        case .waiting: waitingColor
        case .working: workingColor
        case .failed: .red
        case .done: .green
        case .quiet, nil: .secondary
        }
    }
}

struct ThreadStateDot: View {
    let state: ThreadPresentationState?

    var body: some View {
        Group {
            if state == .done {
                Image(systemName: "checkmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.green)
            } else if state == .failed {
                Image(systemName: "exclamationmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
            } else {
                Circle()
                    .fill(ThreadStateStyle.color(for: state))
                    .frame(width: 10, height: 10)
            }
        }
        .frame(width: 14, height: 14)
        .accessibilityHidden(true)
    }
}

/// A thread in Home's transcript, where the assistant started it: its state,
/// title and latest line, updated in place. Tapping opens the thread.
struct ThreadCardView: View {
    let call: ChatToolCall
    let store: HomeThreadsStore

    private static let previewLimit = 300

    var body: some View {
        let thread = store.thread(forSourceToolCallID: call.id)
        let title = thread?.title ?? ChatThreadTool.requestedTitle(from: call.argumentsText) ?? "New thread"
        Button {
            if let thread {
                store.open(thread.conversationID)
            }
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    ThreadStateDot(state: thread?.presentationState)
                    Text(title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text(ThreadPresentationState.label(for: thread?.presentationState))
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(ThreadStateStyle.color(for: thread?.presentationState))
                        .lineLimit(1)
                        .fixedSize()
                }
                if let line = thread?.detailLine {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(String(line.prefix(Self.previewLimit)))
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        if let thread {
                            UnreadBadge(count: thread.unreadCount)
                        }
                    }
                    .padding(.leading, 22)
                } else if let thread, thread.unreadCount > 0 {
                    UnreadBadge(count: thread.unreadCount)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(.systemBackground), in: RoundedRectangle(cornerRadius: 14))
            .overlay {
                RoundedRectangle(cornerRadius: 14)
                    .strokeBorder(Color(.separator))
            }
        }
        .buttonStyle(.plain)
        .disabled(thread == nil)
        .contextMenu {
            if let thread, thread.canMarkDone {
                Button {
                    Task { await store.markDone(thread.conversationID) }
                } label: {
                    Label("Mark Done", systemImage: "checkmark.circle")
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            thread?.accessibilitySummary
                ?? "\(title), \(ThreadPresentationState.label(for: nil))"
        )
        .accessibilityIdentifier("thread-card-\(call.id)")
    }
}

/// The conversation navigator when threads are on: Home, Home's threads by what
/// they need, then everything else under "Earlier".
struct ThreadSwitcherView: View {
    var store: HomeThreadsStore
    var homeModel: ChatViewModel
    let homePreview: String?
    let onSelect: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var startsThread = false
    @State private var searchText = ""
    @State private var searchResults: (query: String, conversations: [ChatConversationSummary])?
    @State private var searchFailed = false

    private static let searchDebounce: Duration = .milliseconds(300)

    private var searchQuery: String {
        searchText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            List {
                if searchQuery.isEmpty {
                    browseSections
                } else {
                    searchSection
                }
            }
            .navigationTitle("Chats")
            .navigationBarTitleDisplayMode(.large)
            .searchable(text: $searchText, prompt: "Search chats")
            .task(id: searchQuery) {
                await runSearch(for: searchQuery)
            }
            .refreshable {
                async let threads: Void = store.refreshThreads()
                await store.refreshConversations()
                await threads
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        startsThread = true
                    } label: {
                        Label("New Thread", systemImage: "square.and.pencil")
                    }
                    .accessibilityIdentifier("thread-switcher-new-thread")
                }
            }
            .sheet(isPresented: $startsThread) {
                StartThreadSheet(mode: .newThread, homeModel: homeModel, store: store) { thread in
                    startsThread = false
                    onSelect(thread.conversationID)
                }
            }
        }
        .task {
            store.listsConversations = true
            async let threads: Void = store.refreshThreads()
            await store.refreshConversations()
            await threads
        }
        .onDisappear {
            store.listsConversations = false
        }
    }

    @ViewBuilder
    private var browseSections: some View {
        let sections = store.sections
        if store.threadsRefreshFailed || store.conversationsRefreshFailed {
            Label("Couldn’t refresh. Pull to try again.", systemImage: "exclamationmark.triangle")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("thread-switcher-refresh-failed")
        }
        Section {
            Button {
                onSelect(store.homeConversationID)
            } label: {
                HStack(spacing: 11) {
                    Image(systemName: "house.fill")
                        .font(.footnote)
                        .foregroundStyle(.white)
                        .frame(width: 30, height: 30)
                        .background(Color.accentColor, in: RoundedRectangle(cornerRadius: 9))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Home")
                            .font(.body.weight(.semibold))
                        if let homePreview {
                            Text(homePreview)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                }
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("thread-switcher-home")
        }
        ForEach(sections.groups) { group in
            Section(group.title) {
                ForEach(group.threads) { thread in
                    Button {
                        onSelect(thread.conversationID)
                    } label: {
                        ThreadSwitcherRow(thread: thread)
                    }
                    .buttonStyle(.plain)
                    .swipeActions(edge: .trailing) {
                        if thread.canMarkDone {
                            Button("Mark Done") {
                                Task { await store.markDone(thread.conversationID) }
                            }
                            .tint(.green)
                        }
                    }
                    .accessibilityIdentifier("thread-row-\(thread.conversationID)")
                }
            }
        }
        if !sections.earlier.isEmpty {
            Section("Earlier") {
                ForEach(sections.earlier) { conversation in
                    conversationButton(conversation)
                }
            }
        } else if store.isLoadingConversations {
            Section("Earlier") {
                ProgressView()
            }
        }
    }

    @ViewBuilder
    private var searchSection: some View {
        if searchFailed {
            Label("Couldn’t search chats. Edit the search to try again.", systemImage: "exclamationmark.triangle")
                .font(.footnote)
                .foregroundStyle(.secondary)
        } else if let searchResults, searchResults.query == searchQuery {
            if searchResults.conversations.isEmpty {
                ContentUnavailableView.search(text: searchQuery)
            } else {
                ForEach(searchResults.conversations) { conversation in
                    conversationButton(conversation)
                }
            }
        } else {
            ProgressView()
        }
    }

    private func conversationButton(_ conversation: ChatConversationSummary) -> some View {
        Button {
            onSelect(conversation.conversationID)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(conversation.conversationID == store.homeConversationID ? "Home" : conversation.displayTitle)
                    .font(.body)
                    .lineLimit(2)
                if let matchExcerpt = conversation.matchExcerpt, !matchExcerpt.isEmpty {
                    Text(matchExcerpt)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                Text(conversation.lastTimestamp, style: .date)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("conversation-row-\(conversation.conversationID)")
    }

    private func runSearch(for query: String) async {
        searchFailed = false
        guard !query.isEmpty else {
            searchResults = nil
            return
        }
        do {
            try await Task.sleep(for: Self.searchDebounce)
            let conversations = try await store.searchConversations(matching: query)
            searchResults = (query, conversations)
        } catch {
            if !Task.isCancelled {
                searchFailed = true
            }
        }
    }
}

private struct ThreadSwitcherRow: View {
    let thread: ChatThreadSummary

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            ThreadStateDot(state: thread.presentationState)
                .padding(.top, 3)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline) {
                    Text(thread.title)
                        .font(.body.weight(.semibold))
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text(thread.updatedAt.formatted(.relative(presentation: .named)))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .fixedSize()
                }
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(String((thread.detailLine ?? "").prefix(200)))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    UnreadBadge(count: thread.unreadCount)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(thread.accessibilitySummary)
    }
}
