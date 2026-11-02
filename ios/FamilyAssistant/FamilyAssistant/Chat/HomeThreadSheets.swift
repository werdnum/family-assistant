import SwiftUI

/// Starts a thread of Home on a profile the user picks: "New thread" from the
/// switcher, send-as-thread from Home's composer, or "Continue in a thread"
/// from a Home message.
struct StartThreadSheet: View {
    enum Mode: Equatable {
        case newThread
        /// The composer's message is the opening message; only the profile is asked.
        case sendAsThread
        case continueFrom(anchorMessageID: Int, quote: String)
    }

    let mode: Mode
    var homeModel: ChatViewModel
    let store: HomeThreadsStore
    /// Called with the thread once it has started; the caller dismisses the
    /// sheet and opens the thread. Not called for send-as-thread, whose
    /// start runs from the composer once the sheet has gone.
    let onStarted: (ChatThreadSummary) -> Void

    @Environment(\.dismiss) private var dismiss
    /// Nil until the user picks: the default then follows the profile list,
    /// which may arrive after the sheet opens.
    @State private var chosenProfileID: String?
    @State private var prompt = ""
    @State private var includeContext = true
    /// The last start sent, so retrying it unchanged after a failure is the
    /// same start to the server and cannot leave two threads behind.
    @State private var lastAttempt: HomeThreadStart?
    @State private var isStarting = false
    @State private var failure: ThreadActionFailure?

    init(
        mode: Mode,
        homeModel: ChatViewModel,
        store: HomeThreadsStore,
        onStarted: @escaping (ChatThreadSummary) -> Void = { _ in }
    ) {
        self.mode = mode
        self.homeModel = homeModel
        self.store = store
        self.onStarted = onStarted
    }

    private var profileID: String {
        chosenProfileID ?? homeModel.defaultProfileID
    }

    private var profiles: [ChatProfile] {
        homeModel.profiles
    }

    private var selectedProfile: ChatProfile? {
        profiles.first { $0.id == profileID }
    }

    private var anchorMessageID: Int? {
        if case let .continueFrom(anchor, _) = mode {
            return anchor
        }
        return nil
    }

    private var options: ThreadStartOptions {
        ThreadStartOptions(
            anchorMessageID: anchorMessageID,
            profileID: profileID,
            householdContextProfileIDs: homeModel.householdContextProfileIDs
        )
    }

    private var carriesContext: Bool {
        options.carriesContext(includeContext: includeContext)
    }

    private var canStart: Bool {
        guard !isStarting, selectedProfile != nil else {
            return false
        }
        switch mode {
        case .sendAsThread:
            return homeModel.canSendDraftAsThread
        case .newThread, .continueFrom:
            return options.canStart(prompt: prompt, includeContext: includeContext)
        }
    }

    var body: some View {
        NavigationStack {
            Form {
                if case let .continueFrom(_, quote) = mode {
                    Section("Continuing from") {
                        Text(verbatim: quote)
                            .font(.footnote)
                            .lineLimit(4)
                            .accessibilityIdentifier("start-thread-quote")
                    }
                }
                if mode == .sendAsThread {
                    Section("Message") {
                        Text(verbatim: homeModel.draftText)
                            .lineLimit(4)
                        if !homeModel.draftAttachments.isEmpty {
                            let count = homeModel.draftAttachments.count
                            Label(count == 1 ? "1 attachment" : "\(count) attachments", systemImage: "paperclip")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                Section {
                    profileRow
                } footer: {
                    if let selectedProfile, !selectedProfile.description.isEmpty {
                        Text(verbatim: selectedProfile.description)
                    }
                }
                if anchorMessageID != nil {
                    contextSection
                }
                if mode != .sendAsThread {
                    Section {
                        TextField(
                            carriesContext ? "Opening message (optional)" : "Opening message",
                            text: $prompt,
                            axis: .vertical
                        )
                        .lineLimit(3...8)
                        .accessibilityIdentifier("start-thread-prompt")
                    }
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .task {
                await homeModel.ensureProfilesLoaded()
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isStarting {
                        ProgressView()
                            .accessibilityIdentifier("start-thread-progress")
                    } else {
                        Button(mode == .sendAsThread ? "Send" : "Start") {
                            Task { await start() }
                        }
                        .disabled(!canStart)
                        .accessibilityIdentifier("start-thread-confirm")
                    }
                }
            }
            .alert(
                failure?.title ?? "",
                isPresented: Binding(
                    get: { failure != nil },
                    set: { if !$0 { failure = nil } }
                ),
                presenting: failure
            ) { _ in
                Button("OK", role: .cancel) {}
            } message: { failure in
                Text(verbatim: failure.message)
            }
        }
    }

    @ViewBuilder
    private var profileRow: some View {
        switch homeModel.profileListState {
        case .ready:
            Picker(
                "Profile",
                selection: Binding(
                    get: { profileID },
                    set: { chosenProfileID = $0 }
                )
            ) {
                ForEach(profiles) { profile in
                    Text(verbatim: profile.id).tag(profile.id)
                }
            }
            .accessibilityIdentifier("start-thread-profile")
        case .loading:
            HStack {
                Text("Loading profiles…")
                    .foregroundStyle(.secondary)
                Spacer()
                ProgressView()
            }
            .accessibilityIdentifier("start-thread-profiles-loading")
        case .unavailable:
            HStack {
                Text("Couldn’t load profiles.")
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Retry") {
                    Task { await homeModel.ensureProfilesLoaded() }
                }
                .accessibilityIdentifier("start-thread-profiles-retry")
            }
        }
    }

    @ViewBuilder
    private var contextSection: some View {
        Section {
            if options.offersContext {
                Toggle("Bring this conversation", isOn: $includeContext)
                    .accessibilityIdentifier("start-thread-include-context")
            } else {
                Label {
                    Text(verbatim: "\(profileID) doesn’t take Home conversations, so the thread starts from your message alone.")
                } icon: {
                    Image(systemName: "lock")
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("start-thread-request-only")
            }
        } footer: {
            if carriesContext {
                Text("Copies this exchange and up to four before it into the thread.")
            }
        }
    }

    private var title: String {
        switch mode {
        case .newThread: "New Thread"
        case .sendAsThread: "Send as Thread"
        case .continueFrom: "Continue in Thread"
        }
    }

    private func start() async {
        if mode == .sendAsThread {
            let profileID = profileID
            let homeModel = homeModel
            let store = store
            dismiss()
            await homeModel.sendDraftAsThread(profileID: profileID) { start in
                await store.startThreadAndOpen(start)
            }
            return
        }
        isStarting = true
        defer { isStarting = false }
        let start = options.start(turnID: UUID().uuidString, prompt: prompt, includeContext: includeContext)
            .retrying(lastAttempt)
        lastAttempt = start
        switch await store.startThread(start) {
        case let .success(thread):
            onStarted(thread)
        case let .failure(failure):
            self.failure = failure
        case nil:
            break
        }
    }
}

/// "Move to thread…": picks the thread a Home turn moves into. Threads that
/// can't take it are listed disabled, with the reason.
struct MoveToThreadSheet: View {
    let messageID: Int
    let quote: String
    var homeModel: ChatViewModel
    let store: HomeThreadsStore

    @Environment(\.dismiss) private var dismiss
    @State private var movingTo: String?
    @State private var failure: ThreadActionFailure?

    private var destinations: [ThreadMoveDestination] {
        ThreadMoveDestination.list(
            threads: store.threads,
            householdContextProfileIDs: homeModel.householdContextProfileIDs
        )
    }

    var body: some View {
        NavigationStack {
            List {
                Section("Exchange") {
                    Text(verbatim: quote)
                        .font(.footnote)
                        .lineLimit(4)
                }
                Section {
                    if destinations.isEmpty {
                        Text("No threads yet. Start one with Continue in Thread.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    ForEach(destinations) { destination in
                        Button {
                            Task { await move(to: destination) }
                        } label: {
                            MoveDestinationRow(
                                destination: destination,
                                isMoving: movingTo == destination.id
                            )
                        }
                        .buttonStyle(.plain)
                        .disabled(!destination.isAvailable || movingTo != nil)
                        .accessibilityIdentifier("move-destination-\(destination.id)")
                    }
                } header: {
                    Text("Threads")
                } footer: {
                    Text("The whole exchange moves, and Home keeps a link to it.")
                }
            }
            .navigationTitle("Move to Thread")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .task {
                async let profiles: Void = homeModel.ensureProfilesLoaded()
                await store.refreshThreads()
                await profiles
            }
            .alert(
                failure?.title ?? "",
                isPresented: Binding(
                    get: { failure != nil },
                    set: { if !$0 { failure = nil } }
                ),
                presenting: failure
            ) { _ in
                Button("OK", role: .cancel) {}
            } message: { failure in
                Text(verbatim: failure.message)
            }
        }
    }

    private func move(to destination: ThreadMoveDestination) async {
        guard destination.isAvailable, movingTo == nil else {
            return
        }
        // The menu was offered at rest, but a reply may have started since.
        guard homeModel.isAtRest else {
            failure = ThreadActionFailure(
                title: "Couldn’t Move to Thread",
                message: "Wait for the reply in progress to finish, then move the turn."
            )
            return
        }
        movingTo = destination.id
        defer { movingTo = nil }
        switch await store.moveTurn(messageID: messageID, to: destination.id) {
        case .success:
            dismiss()
        case let .failure(failure):
            self.failure = failure
        case nil:
            break
        }
    }
}

private struct MoveDestinationRow: View {
    let destination: ThreadMoveDestination
    let isMoving: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            ThreadStateDot(state: destination.thread.presentationState)
                .padding(.top, 3)
            VStack(alignment: .leading, spacing: 2) {
                Text(verbatim: destination.thread.title)
                    .font(.body.weight(.semibold))
                    .foregroundStyle(destination.isAvailable ? .primary : .secondary)
                    .lineLimit(1)
                Text(verbatim: destination.unavailableReason ?? destination.thread.profileID)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if isMoving {
                ProgressView()
            }
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

/// In Home, where a moved turn was: the thread it went to, and the way into it.
struct MovedTurnMarker: View {
    let threadID: String
    let store: HomeThreadsStore?

    var body: some View {
        let title = store?.thread(conversationID: threadID)?.title
        let text = title.map { "Moved to \($0)" } ?? "Moved to a thread"
        Button {
            store?.open(threadID)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "arrow.turn.down.right")
                Text(verbatim: text)
                    .lineLimit(1)
                if store != nil {
                    Image(systemName: "chevron.right")
                        .font(.caption2)
                }
            }
            .font(.footnote.weight(.semibold))
            .foregroundStyle(store == nil ? Color.secondary : Color.accentColor)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color(.secondarySystemBackground), in: Capsule())
        }
        .buttonStyle(.plain)
        .disabled(store == nil)
        .frame(maxWidth: .infinity, alignment: .center)
        .accessibilityLabel(text)
        .accessibilityHint("Opens the thread")
        .accessibilityIdentifier("moved-marker-\(threadID)")
    }
}
