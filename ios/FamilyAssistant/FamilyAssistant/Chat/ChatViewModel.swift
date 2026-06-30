import Foundation
import Observation
import os

@MainActor
@Observable
final class ChatViewModel {
    var conversations: [ChatConversationSummary] = []
    var messages: [ChatMessage] = []
    // Upper bound on how many grouped bubbles are realized into the chat view at
    // once. The full thread is still loaded into `messages`; only a recent window
    // is shown, and older bubbles page in via `showEarlierMessages()`. See
    // `visibleGroupedMessages`.
    var displayedMessageLimit = ChatViewModel.initialDisplayedMessageCount
    var profiles: [ChatProfile] = []
    var defaultProfileID = "default_assistant"
    // The profile the active conversation runs under: it drives the picker label
    // and is sent on every turn. The backend partitions a conversation's history
    // by `processing_profile_id` (see `get_recent_history`), so a follow-up sent
    // under a different profile than the thread was built in loads NONE of its
    // prior history. Opening an existing conversation therefore adopts that
    // conversation's profile rather than carrying over a stale global selection.
    var selectedProfileID: String
    // The profile to use for NEW conversations, persisted across launches. Set
    // when the user picks from the profile picker (which starts a new chat). Kept
    // separate from `selectedProfileID` so viewing an existing conversation in a
    // different profile doesn't overwrite the user's preferred profile for new
    // chats.
    @ObservationIgnored private var preferredProfileID: String
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
    var steerErrorMessage: String?
    var stopWarningMessage: String?
    var composerFocusRequestID: UUID?

    var canSendDraft: Bool {
        let prompt = draftText.trimmingCharacters(in: .whitespacesAndNewlines)
        let hasContent = !prompt.isEmpty || !draftAttachments.isEmpty
        let attachmentsReady = draftAttachments.allSatisfy { $0.uploadState == .uploaded }
        // Block sending while a conversation's history is loading: until it
        // completes, `selectConversation` hasn't adopted the thread's profile yet,
        // so a send would post under the previous profile and the backend would
        // filter this thread's history out of the turn's context.
        return hasContent && attachmentsReady && !isLoadingMessages
    }

    @ObservationIgnored private let apiClient: ChatAPIClient
    // Injectable so tests can observe the diagnostic breadcrumbs emitted on a
    // stream drop without going through the shared singleton.
    @ObservationIgnored private let errorReporter: ErrorReporter
    // Diagnostic logger for the SSE streaming paths. Pairs with the
    // ErrorReporter breadcrumbs emitted on a stream drop: os.Logger is the live
    // (tethered) view, ErrorReporter persists the same facts to the backend
    // error log / diagnostics export.
    @ObservationIgnored private let streamLogger = Logger(
        subsystem: "com.familyassistant.app",
        category: "chat-stream"
    )
    @ObservationIgnored private var streamTask: Task<Void, Never>?
    // Identifies the active send. A superseded (cancelled) streamTask resuming
    // across an await must not clobber the turn that replaced it, so its tail
    // work is gated on this token still matching.
    @ObservationIgnored private var currentStreamToken: UUID?
    @ObservationIgnored private var liveEventsTask: Task<Void, Never>?
    @ObservationIgnored private var activityStreamTask: Task<Void, Never>?
    @ObservationIgnored private var pendingConfirmationsTask: Task<Void, Never>?
    @ObservationIgnored private var activeTurn: ActiveChatTurn?
    // In-flight optimistic summaries, keyed by the owning turn id (value is the
    // conversation id). While a conversation has any pending turn,
    // `refreshRecentConversations` keeps its held optimistic row over a stale
    // server summary; the entry is cleared when that turn settles via any path
    // (a lifecycle signal, not a wall-clock comparison a skewed client clock
    // could keep "newer" forever). Keying by turn id means a superseding re-send
    // to the same conversation doesn't clobber the new turn's mark when the old
    // turn unwinds.
    @ObservationIgnored private var optimisticPendingByTurnID: [String: String] = [:]
    @ObservationIgnored private var registeredTurnIDs: Set<String> = []
    @ObservationIgnored private var pendingStopTurnIDs: Set<String> = []
    @ObservationIgnored private var stopAfterRegistrationByTurnID: [String: String] = [:]
    @ObservationIgnored private var stopRequestedTurnIDs: Set<String> = []
    @ObservationIgnored private var pendingSteersByTurnID: [String: [String]] = [:]
    @ObservationIgnored private var inFlightSteers: [String] = []
    @ObservationIgnored private var awaitingEchoSteers: [String] = []
    @ObservationIgnored private var queuedFollowUpSteers: [String] = []
    @ObservationIgnored private var localUserInputConversationIDByMessageID: [String: String] = [:]
    @ObservationIgnored private var representedPersistedUserInputEchoCounts: [UserInputEchoKey: Int] = [:]
    @ObservationIgnored private var lastProcessedInitialPrompt: String?
    // Highest stream seq applied for the active conversation, threaded into the
    // follow subscribe's `ack_seq` and the `/ack` POST after a turn_ended so the
    // server suppresses the disconnect push for events this client has seen.
    // Reset whenever the active conversation changes.
    @ObservationIgnored private var highestAppliedSeq: Int?
    // Maps a turn id observed live on the always-on follow stream to the local
    // assistant bubble its tokens render into. Lets a turn started elsewhere
    // (another device, or after our own send task gave up) stream live into the
    // right bubble. The `local_` prefixed bubble is reconciled to persisted
    // history on `turn_ended`. Reset whenever the active conversation changes.
    @ObservationIgnored private var liveFollowBubbleByTurnID: [String: String] = [:]
    // Turn ids whose `turn_ended` has already been seen (from either the send
    // path or the follow stream). The follow stream and a send connection both
    // carry a turn's tokens out of step, so after a send finishes a stray
    // late-delivered token for it could otherwise spawn a duplicate live bubble;
    // skip rendering follow tokens for an already-ended turn. Reset per
    // conversation.
    @ObservationIgnored private var endedTurnIDs: Set<String> = []
    @ObservationIgnored private var endedTurnStatusByTurnID: [String: String] = [:]

    private enum Keys {
        static let lastConversationID = "lastConversationId"
        static let lastConversationActiveAt = "lastConversationActiveAt"
        static let selectedProfileID = "selectedProfileId"
    }

    private struct ActiveChatTurn: Equatable {
        let turnID: String
        let conversationID: String
    }

    private struct UserInputEchoKey: Hashable {
        let turnID: String
        let text: String
    }

    // How recently the last conversation must have been active for it to reopen
    // on launch. Past this, launch lands on the conversation list instead of
    // restoring (then bouncing away from) a thread the user has moved on from.
    static let conversationRestoreWindow: TimeInterval = 15 * 60

    // Windowing for the chat thread. A long thread is loaded in full, but only a
    // recent window of grouped bubbles is realized into the view at once: a
    // LazyVStack still measures every row it is given to size its scroll content
    // (LazyStack.measureEstimates is on the watchdog crash stacks), so an
    // unbounded thread means an unbounded first layout pass — the foreground
    // layout hang. `showEarlierMessages()` grows the window a page at a time.
    static let initialDisplayedMessageCount = 30
    private static let displayedMessagePageSize = 30

    // Max characters of an optimistic conversation-list preview, matching the
    // server's `last_message` truncation so the row doesn't jump in length when
    // the authoritative summary replaces it.
    private static let conversationPreviewMaxLength = 100

    // Page size for incremental `after`-timestamp message loads. The server's
    // paginated path ignores `after` when limit=0, so a bounded page is needed;
    // `mergeNewMessages` pages through on `hasMoreAfter`.
    private static let messageDeltaPageSize = 100

    // Backoff bounds for auto-reconnecting the live-updates follow stream after
    // an unexpected drop. A mobile SSE connection is dropped routinely
    // (backgrounding, network changes, idle proxy timeouts, backend redeploys);
    // without an automatic retry the disconnected indicator stays stuck until
    // the user taps it. Injectable so tests can drive reconnects without waiting
    // out the production delay.
    @ObservationIgnored private let liveReconnectInitialDelaySeconds: Double
    @ObservationIgnored private let liveReconnectMaxDelaySeconds: Double

    // While a send is in flight the watch stream is resumed across mid-turn drops
    // (see `runSendTurn`). This caps the number of *consecutive* resumes that end
    // immediately with no new events before the client gives up and reloads
    // history, so an already-gone turn can't reconnect forever. A resume that
    // applies new events OR is held open by the server (proof the turn is still
    // running — `follow=false` only blocks while a turn is live) resets the
    // streak, so a healthy turn — even a quiet one mid tool-call — streams on.
    @ObservationIgnored private let maxConsecutiveStreamResumes: Int
    // A resume that stayed connected at least this long without new events was
    // held open by the server for a still-running turn (it blocks on the live
    // queue), not an instant drain-and-close of a finished/gone turn — so it must
    // not count toward the give-up streak above. Injectable for tests.
    @ObservationIgnored private let streamResumeLivenessSeconds: Double

    // Streamed assistant text arrives as many small deltas. Appending each one to
    // `messages` directly would re-render and re-lay-out the whole thread per
    // token; instead deltas are buffered per assistant message and flushed on a
    // timer (and synchronously before any turn finalizes), capping main-thread
    // work regardless of token rate. Keyed by assistant message id so a
    // superseded turn's buffer can't leak into a different bubble.
    @ObservationIgnored private var pendingTextByMessageID: [String: String] = [:]
    @ObservationIgnored private var textFlushTask: Task<Void, Never>?
    @ObservationIgnored private let streamTextFlushInterval: Duration

    #if DEBUG
    /// Test-only: the currently buffered (not-yet-flushed) streamed text. Lets
    /// tests wait deterministically for a delta to be received and buffered
    /// instead of racing on a fixed delay.
    var bufferedStreamTextForTesting: String {
        pendingTextByMessageID.values.joined()
    }
    #endif

    init(
        authManager: AuthManager,
        conversationID: String? = nil,
        initialPrompt: String? = nil,
        startsNewConversation: Bool = false,
        liveReconnectInitialDelaySeconds: Double = 2,
        liveReconnectMaxDelaySeconds: Double = 30,
        maxConsecutiveStreamResumes: Int = 5,
        streamResumeLivenessSeconds: Double = 2,
        streamTextFlushInterval: Duration = .milliseconds(50),
        errorReporter: ErrorReporter = .shared
    ) {
        self.liveReconnectInitialDelaySeconds = liveReconnectInitialDelaySeconds
        self.liveReconnectMaxDelaySeconds = liveReconnectMaxDelaySeconds
        self.maxConsecutiveStreamResumes = maxConsecutiveStreamResumes
        self.streamResumeLivenessSeconds = streamResumeLivenessSeconds
        self.streamTextFlushInterval = streamTextFlushInterval
        self.errorReporter = errorReporter
        apiClient = ChatAPIClient(authManager: authManager)
        let storedProfileID = UserDefaults.standard.string(forKey: Keys.selectedProfileID) ?? "default_assistant"
        preferredProfileID = storedProfileID
        // Starts at the preferred profile; if launch restores a conversation,
        // `bootstrap` reopens it via `selectConversation`, which adopts that
        // conversation's own profile.
        selectedProfileID = storedProfileID
        if startsNewConversation {
            self.conversationID = Self.generateConversationID()
            conversationSelection = self.conversationID
            composerFocusRequestID = UUID()
        } else if let initialPrompt, !initialPrompt.isEmpty {
            // Launched to start a brand-new chat (share extension / App Intent).
            self.conversationID = Self.generateConversationID()
            conversationSelection = self.conversationID
            draftText = initialPrompt
            persistConversationID()
        } else if let conversationID {
            // Explicit route / deep link: open that thread regardless of age.
            self.conversationID = conversationID
            conversationSelection = conversationID
            persistConversationID()
        } else if let restored = Self.recentlyActiveConversationID() {
            // Reopened within the restore window: resume the prior conversation.
            self.conversationID = restored
            conversationSelection = restored
            persistConversationID()
        } else {
            // No recent conversation: open a fresh thread and focus the composer
            // for quick capture. Leave the stored last conversation untouched so
            // merely opening the app does not make an empty draft the restored
            // thread on the next launch.
            self.conversationID = Self.generateConversationID()
            conversationSelection = self.conversationID
            composerFocusRequestID = UUID()
        }
    }

    deinit {
        streamTask?.cancel()
        liveEventsTask?.cancel()
        activityStreamTask?.cancel()
        pendingConfirmationsTask?.cancel()
        textFlushTask?.cancel()
    }

    func bootstrap(initialPrompt: String? = nil) async {
        await loadProfiles()
        await refreshConversations()
        // Only open the thread when launch decided to restore a selection.
        // A nil selection means we deliberately landed on the conversation list
        // (no recent conversation), so opening one here would defeat that.
        if let conversationID, conversationSelection != nil {
            await selectConversation(conversationID, shouldLoadMessages: true)
        }
        startPendingConfirmationsPolling()
        startActivityStream()
        if let initialPrompt, shouldProcessInitialPrompt(initialPrompt) {
            lastProcessedInitialPrompt = initialPrompt
            draftText = initialPrompt
            await sendDraft()
        } else if shouldProcessInitialPrompt(draftText) {
            lastProcessedInitialPrompt = draftText
            await sendDraft()
        }
    }

    func applyRoute(
        conversationID: String?,
        initialPrompt: String?,
        startsNewConversation: Bool = false
    ) async {
        if startsNewConversation {
            startNewConversation()
            if let initialPrompt, shouldProcessInitialPrompt(initialPrompt) {
                lastProcessedInitialPrompt = initialPrompt
                draftText = initialPrompt
                await sendDraft()
            }
            return
        }
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
            errorReporter.report(error, component: "Chat.conversations")
        }
        isLoadingConversations = false
    }

    /// Refresh only the most recent page of conversation summaries.
    ///
    /// Used after a turn or live event: the changed conversation surfaces on the
    /// first (most-recent-first) page, so paging the entire history each time is
    /// wasteful. Merges the page over the held list, preserving older summaries
    /// the page didn't include.
    ///
    /// Lifecycle guard: for a conversation whose send is still in flight (in
    /// ``optimisticPendingConversationIDs``), keep the held optimistic row over
    /// the server's summary, so a stale in-flight refresh — which still carries
    /// the old preview/position for an existing thread, or omits a brand-new one
    /// entirely — can't undo the optimistic insert/bump. The pending mark is
    /// cleared when the turn settles (not by a client/server timestamp compare,
    /// which a skewed clock could keep "newer" forever), after which the server
    /// row is authoritative. The whole list is sorted most-recent-first so a kept
    /// optimistic row stays at the top.
    private func refreshRecentConversations() async {
        do {
            let recent = try await apiClient.listRecentConversations()
            let recentIDs = Set(recent.map(\.conversationID))
            let heldByID = Dictionary(
                conversations.map { ($0.conversationID, $0) },
                uniquingKeysWith: { first, _ in first }
            )
            let pendingIDs = Set(optimisticPendingByTurnID.values)
            let merged = recent.map { serverRow -> ChatConversationSummary in
                if pendingIDs.contains(serverRow.conversationID),
                   let held = heldByID[serverRow.conversationID]
                {
                    return held
                }
                return serverRow
            }
            let untouched = conversations.filter { !recentIDs.contains($0.conversationID) }
            conversations = (merged + untouched).sorted { $0.lastTimestamp > $1.lastTimestamp }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
            errorReporter.report(error, component: "Chat.recentConversations")
        }
    }

    /// Optimistically surface the active conversation in the list the instant the
    /// user sends, so a brand-new chat appears immediately (and an existing one
    /// bumps to the top) without waiting for a server round-trip or notification.
    ///
    /// The authoritative `refreshRecentConversations` that runs on turn
    /// completion reconciles this row by `conversationID` — the server summary
    /// replaces it, so there is no duplication — and if that refresh ever races
    /// ahead of persistence the optimistic row is preserved as `untouched`, so
    /// the new chat never flickers out of the list.
    private func upsertLocalConversationSummary(conversationID: String, lastMessage: String) {
        let preview = String(lastMessage.prefix(Self.conversationPreviewMaxLength))
        let existing = conversations.first { $0.conversationID == conversationID }
        let summary = ChatConversationSummary(
            conversationID: conversationID,
            lastMessage: preview,
            lastTimestamp: Date(),
            messageCount: (existing?.messageCount ?? 0) + 1
        )
        conversations.removeAll { $0.conversationID == conversationID }
        conversations.insert(summary, at: 0)
    }

    /// Undo an optimistic summary when starting the turn failed before anything
    /// was persisted. A brand-new conversation (`previous == nil`) is dropped so
    /// it doesn't linger as a phantom; an existing conversation is restored to
    /// its pre-send summary (preview/timestamp), re-inserted in recency order, so
    /// the failed bump doesn't stay pinned at the top with never-persisted text
    /// (the freshness guard in `refreshRecentConversations` would otherwise keep
    /// the newer optimistic row).
    /// Roll back the optimistic summary unless a superseding send for the same
    /// conversation (a different in-flight turn) still owns it — that turn will
    /// reconcile or roll back its own row. ``turnID`` is the failing turn, whose
    /// own pending entry is ignored (it is cleared by ``runSendTurn``'s defer).
    private func rollbackOptimisticSummaryIfUnowned(
        conversationID: String,
        turnID: String,
        to previous: ChatConversationSummary?
    ) {
        let ownedByAnotherTurn = optimisticPendingByTurnID.contains {
            $0.key != turnID && $0.value == conversationID
        }
        if ownedByAnotherTurn {
            return
        }
        rollbackOptimisticSummary(conversationID: conversationID, to: previous)
    }

    private func rollbackOptimisticSummary(
        conversationID: String,
        to previous: ChatConversationSummary?
    ) {
        conversations.removeAll { $0.conversationID == conversationID }
        guard let previous else {
            return
        }
        let insertIndex =
            conversations.firstIndex { $0.lastTimestamp < previous.lastTimestamp }
            ?? conversations.endIndex
        conversations.insert(previous, at: insertIndex)
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
        // The main composer doubles as the steer input and is shared across
        // conversations, so on an actual switch clear it: steer text typed for
        // the previous turn must not leak into — and be sent in — the newly
        // selected thread. Restoring the current conversation (id unchanged) keeps
        // any draft the user is typing.
        let isSwitchingConversation = id != conversationID
        let queuedStopTurnID = activeTurn.flatMap { activeTurn in
            stopAfterRegistrationByTurnID[activeTurn.turnID] == nil ? nil : activeTurn.turnID
        }
        cancelStream(sendQueuedStopCancel: false)
        // Tear down the previous conversation's follow loop NOW, before the
        // `await loadMessages` below suspends. `cancelStream` only cancels the
        // send task; without this the old conversation's still-live follow loop
        // would keep delivering events across the suspension and mutate the new
        // conversation's shared state (merging its rows, polluting the ack cursor,
        // appending stray bubbles). `startLiveEvents` re-cancels and restarts it.
        liveEventsTask?.cancel()
        highestAppliedSeq = nil
        liveFollowBubbleByTurnID.removeAll()
        endedTurnIDs.removeAll()
        endedTurnStatusByTurnID.removeAll()
        resetTurnControlState()
        displayedMessageLimit = Self.initialDisplayedMessageCount
        if isSwitchingConversation {
            draftText = ""
        }
        conversationID = id
        conversationSelection = id
        persistConversationID()
        mobileShowsConversationList = false
        // Mark loading synchronously, before the first suspension below, so the
        // composer is gated (see `canSendDraft`) for the entire switch — the
        // thread's profile isn't adopted until `loadMessages` returns, and a send
        // before then would go out under the previous profile. `loadMessages`
        // clears this flag when it finishes.
        if shouldLoadMessages {
            isLoadingMessages = true
        }
        if let queuedStopTurnID {
            _ = await cancelStopQueuedBeforeRegistration(for: queuedStopTurnID)
        }
        // Load persisted history BEFORE starting the live-events follow loop. The
        // follow stream now renders live tokens into local bubbles, and
        // `loadMessages` does a full `messages =` replace — starting the loop
        // first lets a token bubble render and then be wiped by the history load
        // (its mapping left dangling). Loading first means live tokens only ever
        // append onto already-loaded history.
        if shouldLoadMessages {
            await loadMessages(conversationID: id)
            // Adopt the opened conversation's profile so follow-ups continue in
            // it. Guarded on the id still being active because `loadMessages`
            // suspends and the user may have switched away meanwhile.
            if conversationID == id {
                adoptConversationProfile()
            }
        }
        startLiveEvents()
    }

    /// Set the active profile from the conversation just loaded into `messages`.
    ///
    /// The backend filters a conversation's LLM history by `processing_profile_id`
    /// (see `get_recent_history`), so the active profile must match the one the
    /// thread's turns were sent under or the assistant loads none of the prior
    /// history. The most recent USER message carries the entry profile the user
    /// actually selected for the thread (a delegated sub-turn's reply may be
    /// tagged with the delegate's profile, so user messages are the reliable
    /// signal). An empty conversation, or one whose messages predate profile
    /// tagging, falls back to the preferred profile.
    private func adoptConversationProfile() {
        let conversationProfile = messages.last {
            $0.role == .user && $0.processingProfileID != nil
        }?.processingProfileID
        selectedProfileID = conversationProfile ?? preferredProfileID
    }

    func startNewConversation() {
        cancelStream()
        liveEventsTask?.cancel()
        highestAppliedSeq = nil
        liveFollowBubbleByTurnID.removeAll()
        endedTurnIDs.removeAll()
        endedTurnStatusByTurnID.removeAll()
        resetTurnControlState()
        displayedMessageLimit = Self.initialDisplayedMessageCount
        conversationID = Self.generateConversationID()
        conversationSelection = conversationID
        messages = []
        draftText = ""
        draftAttachments = []
        composerFocusRequestID = UUID()
        mobileShowsConversationList = false
        // A brand-new conversation has no history to load, so it is never in a
        // loading state. Clear the flag explicitly: starting a new conversation
        // supersedes any in-flight `selectConversation` whose `loadMessages` will
        // early-return (leaving the flag set) now that it is no longer the active
        // conversation. Without this the composer would stay gated and a deep-link
        // auto-send would be dropped (see `canSendDraft`/`sendDraft`).
        isLoadingMessages = false
        // A brand-new conversation runs under the user's preferred profile, not
        // whatever an existing thread we were viewing was pinned to.
        selectedProfileID = preferredProfileID
        persistConversationID()
        startLiveEvents()
    }

    private func resetTurnControlState() {
        activeTurn = nil
        registeredTurnIDs.removeAll()
        pendingStopTurnIDs.removeAll()
        stopRequestedTurnIDs.removeAll()
        pendingSteersByTurnID.removeAll()
        inFlightSteers.removeAll()
        awaitingEchoSteers.removeAll()
        queuedFollowUpSteers.removeAll()
        localUserInputConversationIDByMessageID.removeAll()
        representedPersistedUserInputEchoCounts.removeAll()
        steerErrorMessage = nil
        stopWarningMessage = nil
    }

    func changeProfile(to profileID: String) {
        // Skip only a true no-op: the chosen profile is both the preferred one and
        // already active. Picking the current preferred while viewing a
        // conversation pinned to a different profile must still start a fresh chat
        // in the chosen profile rather than silently doing nothing.
        guard preferredProfileID != profileID || selectedProfileID != profileID else {
            return
        }
        preferredProfileID = profileID
        UserDefaults.standard.set(profileID, forKey: Keys.selectedProfileID)
        // startNewConversation sets `selectedProfileID` to the preferred profile.
        startNewConversation()
    }

    func loadMessages(conversationID: String? = nil) async {
        guard let id = conversationID ?? self.conversationID else {
            return
        }
        isLoadingMessages = true
        do {
            let backendMessages = try await apiClient.getMessages(conversationID: id)
            // The user may have switched conversations during the network await;
            // applying this thread's rows now would clobber the one they moved to.
            // (`self.` is required: the `conversationID` parameter shadows it.)
            guard self.conversationID == id else {
                return
            }
            messages = withLiveFollowBubbles(Self.renderMessages(from: backendMessages))
            errorMessage = nil
        } catch {
            guard self.conversationID == id else {
                return
            }
            errorMessage = error.localizedDescription
            errorReporter.report(error, component: "Chat.messages")
        }
        isLoadingMessages = false
    }

    /// Bubbles for live-follow turns still held — mapped in
    /// `liveFollowBubbleByTurnID` because their canonical persisted row hasn't
    /// arrived yet. Held across a reload/merge so a streaming (or just-finished)
    /// reply isn't wiped before its persisted row reconciles it.
    private func heldLiveFollowBubbles() -> [ChatMessage] {
        let ids = Set(liveFollowBubbleByTurnID.values)
        return messages.filter { ids.contains($0.id) }
    }

    /// Drop the mapping for any live-follow turn whose canonical persisted row has
    /// now arrived in `persisted`, matched precisely by `turnID`. A
    /// `local_follow_<turnID>` bubble is the optimistic/live copy of that turn's
    /// reply; once the persisted row is present it is authoritative, so the local
    /// copy is released (and dropped by the `local_` filter on assignment). When
    /// the backend does not tag rows with a turn id (no `turnID` on any persisted
    /// row — a pre-`turn_id` server), fall back to releasing a turn once it has
    /// ended, so the optimistic bubble can't be left to duplicate the reply.
    private func reconcileLiveFollowBubbles(against persisted: [ChatMessage]) {
        let persistedTurnIDs = Set(persisted.compactMap(\.turnID))
        let backendTagsTurns = !persistedTurnIDs.isEmpty
        let reconciled = liveFollowBubbleByTurnID.keys.filter { turnID in
            persistedTurnIDs.contains(turnID)
                || (!backendTagsTurns && endedTurnIDs.contains(turnID))
        }
        for turnID in reconciled {
            if let bubbleID = liveFollowBubbleByTurnID.removeValue(forKey: turnID) {
                pendingTextByMessageID[bubbleID] = nil
            }
        }
    }

    /// Reconcile against the rendered persisted rows, then combine them with the
    /// still-held live-follow bubbles and local user-input echoes. A single
    /// in-progress reply lands last (its bubble was created after every persisted
    /// row), and two overlapping turns keep their relative order instead of the
    /// older one being forced after the newer finished one. A local user echo for
    /// the same turn is kept before its assistant bubble even when its timestamp
    /// is newer because it arrived after the first token. The index tiebreaker
    /// keeps a stable order for equal timestamps (preserving backend order).
    private func withLiveFollowBubbles(_ base: [ChatMessage]) -> [ChatMessage] {
        reconcileLiveFollowBubbles(against: base)
        let heldMessages = heldLocalUserInputEchoes(against: base) + heldLiveFollowBubbles()
        guard !heldMessages.isEmpty else {
            return base
        }
        let baseIDs = Set(base.map(\.id))
        let combined = base + heldMessages.filter { !baseIDs.contains($0.id) }
        return combined.enumerated()
            .sorted { lhs, rhs in
                let left = lhs.element
                let right = rhs.element
                if left.turnID == right.turnID, left.turnID != nil {
                    let leftIsUserEcho = isLocalUserInputEcho(left)
                    let rightIsUserEcho = isLocalUserInputEcho(right)
                    let leftIsFollowBubble = isLiveFollowBubble(left)
                    let rightIsFollowBubble = isLiveFollowBubble(right)
                    if leftIsUserEcho, rightIsFollowBubble {
                        return true
                    }
                    if leftIsFollowBubble, rightIsUserEcho {
                        return false
                    }
                }
                return (left.createdAt, lhs.offset) < (right.createdAt, rhs.offset)
            }
            .map(\.element)
    }

    private func heldLocalUserInputEchoes(against persisted: [ChatMessage]) -> [ChatMessage] {
        guard let conversationID else {
            return []
        }
        var persistedUserCounts: [UserInputEchoKey: Int] = [:]
        // Only persisted USER rows can stand in for a local user-input echo. An
        // assistant reply that happens to share the steer's turn id and text
        // (e.g. both are "OK") must not consume the echo's slot, or the user's
        // mid-turn steer bubble would vanish on the next history merge.
        for message in persisted where message.id.hasPrefix("msg_") && message.role == .user {
            guard let key = userInputEchoKey(for: message) else {
                continue
            }
            persistedUserCounts[key, default: 0] += 1
        }
        return messages.filter { message in
            guard isLocalUserInputEcho(message),
                  localUserInputConversationIDByMessageID[message.id] == conversationID
            else {
                return false
            }
            guard let key = userInputEchoKey(for: message) else {
                return true
            }
            guard let persistedCount = persistedUserCounts[key], persistedCount > 0 else {
                return true
            }
            persistedUserCounts[key] = persistedCount - 1
            representedPersistedUserInputEchoCounts[key, default: 0] += 1
            return false
        }
    }

    private func isLocalUserInputEcho(_ message: ChatMessage) -> Bool {
        message.id.hasPrefix("local_user_input_") && message.role == .user
    }

    private func userInputEchoKey(for message: ChatMessage) -> UserInputEchoKey? {
        userInputEchoKey(turnID: message.turnID, text: message.text)
    }

    private func userInputEchoKey(turnID: String?, text: String) -> UserInputEchoKey? {
        guard let turnID else {
            return nil
        }
        return UserInputEchoKey(turnID: turnID, text: text)
    }

    private func isLiveFollowBubble(_ message: ChatMessage) -> Bool {
        message.id.hasPrefix("local_follow_") && message.role == .assistant
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
            // A conversation switch during the await would otherwise merge this
            // thread's delta into the one the user moved to.
            guard conversationID == id else {
                return
            }
            guard !delta.isEmpty else {
                errorMessage = nil
                return
            }
            let rendered = Self.renderMessages(from: delta)
            // Drop optimistic local placeholders now that persisted copies exist,
            // then append the fetched delta. Still-running live-follow bubbles are
            // preserved and ordered by creation time (see withLiveFollowBubbles):
            // dropping them would strand a running turn and dangle its mapping. A
            // retired follow bubble is already unmapped (see
            // finalizeLiveFollowBubble), so it is dropped here.
            var merged = messages.filter { !$0.id.hasPrefix("local_") }
            let existingIDs = Set(merged.map(\.id))
            merged.append(contentsOf: rendered.filter { !existingIDs.contains($0.id) })
            messages = withLiveFollowBubbles(merged)
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
            errorReporter.report(error, component: "Chat.mergeMessages")
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
        pendingTextByMessageID[assistantMessageID] = nil
        messages.removeAll { $0.id == assistantMessageID }
    }

    func loadProfiles() async {
        isLoadingProfiles = true
        do {
            let response = try await apiClient.listProfiles()
            defaultProfileID = response.defaultProfileID
            profiles = response.profiles.filter { !$0.delegationOnly }
            // Deliberately do NOT reset the selection when it's absent from the
            // fetched list. The list is empty during a backend cold start
            // (`/v1/profiles` returns `{"profiles":[]}` before the registry is
            // populated), and resetting then would permanently overwrite the
            // user's persisted choice with the default. The backend already falls
            // back to its default for an unknown profile id on send, so keeping a
            // possibly-stale selection is safe. Matches the web frontend.
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
            errorReporter.report(error, component: "Chat.profiles")
        }
        isLoadingProfiles = false
    }

    func sendDraft() async {
        let prompt = draftText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty || !draftAttachments.isEmpty else {
            return
        }
        // Defensive: the send button is disabled while a conversation loads, but
        // never post a turn before its profile is adopted (see `canSendDraft`).
        guard !isLoadingMessages else {
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
            // startNewConversation clears the composer; restore the captured
            // prompt so the recursive send still has it.
            draftText = prompt
            return await sendDraft()
        }

        cancelStream()

        let turnID = UUID().uuidString
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
            errorTraceback: nil,
            turnID: turnID
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
            errorTraceback: nil,
            turnID: turnID
        )
        messages.append(userMessage)
        messages.append(assistantMessage)
        draftText = ""
        draftAttachments = []
        isStreaming = true
        persistConversationID()
        // The conversation's summary before the optimistic bump below, so a turn
        // that fails to start (nothing persisted) can roll back to it: nil for a
        // brand-new conversation (drop the row), the prior summary for an
        // existing one (restore preview/position).
        let previousSummary = conversations.first { $0.conversationID == id }
        upsertLocalConversationSummary(
            conversationID: id,
            lastMessage: prompt.isEmpty ? "Attachment" : prompt
        )
        // Mark this conversation optimistically pending for this turn so a stale
        // refresh keeps the row until the turn settles (cleared on every
        // runSendTurn return path, see its `defer`).
        optimisticPendingByTurnID[turnID] = id

        let streamToken = UUID()
        currentStreamToken = streamToken
        activeTurn = ActiveChatTurn(
            turnID: turnID,
            conversationID: id
        )
        steerErrorMessage = nil
        stopWarningMessage = nil
        streamTask = Task { [weak self] in
            guard let self else { return }
            await runSendTurn(
                turnID: turnID,
                prompt: prompt,
                conversationID: id,
                attachments: uploadedAttachments,
                assistantMessageID: assistantMessageID,
                streamToken: streamToken,
                previousSummary: previousSummary
            )
        }
    }

    /// Outcome of consuming (or attempting to consume) a turn subscription.
    ///
    /// The send-and-watch flow maps a dropped/interrupted stream to a history
    /// reload rather than a hard error, because the turn keeps running durably on
    /// the server whether or not this client stays connected — closing the app or
    /// losing the connection mid-turn must recover, not surface a spurious error.
    private enum TurnSubscriptionOutcome: Equatable {
        /// `turn_ended` seen — the turn finished while we were watching.
        case completed(status: String?)
        /// Server sent `stream_dropped` (subscriber overflow / shutdown), or the
        /// connect failed transiently (5xx). Resumable: resubscribe from the last
        /// applied seq.
        case dropped
        /// The stream closed (or the connection dropped mid-bytes) without
        /// `turn_ended`. The reply may still be in flight or already persisted;
        /// reload history rather than fabricate a completion.
        case interrupted
        /// The turn's events have rotated out of the hub buffer (410). Nothing to
        /// replay, but the reply is durably persisted; reload history.
        case reloadHistory
        /// A genuinely fatal failure (e.g. auth) that recovery can't paper over.
        case failed(String)

        var isCompleted: Bool {
            if case .completed = self {
                return true
            }
            return false
        }
    }

    private func runSendTurn(
        turnID: String,
        prompt: String,
        conversationID id: String,
        attachments: [ChatAttachment],
        assistantMessageID: String,
        streamToken: UUID,
        previousSummary: ChatConversationSummary?
    ) async {
        // Retire this turn's optimistic pending mark on EVERY return path —
        // completion, recovery, failure, cancellation, and the superseded early
        // returns below — so a row can never stay pending (and pinned over the
        // server summary) forever. The explicit removals on the completed/
        // recovered paths run earlier so their refresh already uses the server;
        // this is the catch-all and is idempotent.
        defer { optimisticPendingByTurnID.removeValue(forKey: turnID) }
        var lastSeq: Int?
        // Becomes true once startTurn returns: the backend persists the user
        // message inside start_turn, so a successful return means the
        // conversation is now server-backed and its optimistic row must NOT be
        // removed on a later failure.
        var startSucceeded = false
        do {
            let start = try await apiClient.startTurn(
                turnID: turnID,
                prompt: prompt,
                conversationID: id,
                profileID: selectedProfileID,
                attachments: attachments
            )
            startSucceeded = true
            let stopAfterRegistrationConversationID = stopAfterRegistrationByTurnID.removeValue(forKey: turnID)
            guard !Task.isCancelled, currentStreamToken == streamToken else {
                if let stopConversationID = stopAfterRegistrationConversationID {
                    let turnToCancel = ActiveChatTurn(turnID: turnID, conversationID: stopConversationID)
                    Task { [weak self] in
                        guard let self else {
                            return
                        }
                        do {
                            _ = try await self.requestStopWithRetry(turnToCancel)
                        } catch {
                            self.errorMessage = error.localizedDescription
                            self.errorReporter.report(error, component: "Chat.stopTurn")
                        }
                    }
                }
                return
            }
            registeredTurnIDs.insert(turnID)
            let pendingSteers = pendingSteersByTurnID.removeValue(forKey: turnID) ?? []
            let hadPendingStop = pendingStopTurnIDs.remove(turnID) != nil
                || stopAfterRegistrationConversationID != nil
            if start.alreadyComplete {
                if hadPendingStop || stopRequestedTurnIDs.contains(turnID) {
                    detachPendingSteers(pendingSteers, requeue: false)
                } else {
                    detachPendingSteers(pendingSteers, requeue: true)
                }
                // The retried turn finished durably but is not replayable from the
                // hub. Don't subscribe; reload persisted history to surface it.
                if start.incomplete {
                    appendStreamError(
                        "The assistant reply could not be recovered. Please try again.",
                        assistantMessageID: assistantMessageID
                    )
                } else {
                    // Turn settled (durably complete): retire the optimistic mark
                    // before the recovery refresh so it surfaces the authoritative
                    // server summary, matching the completed/410 paths.
                    optimisticPendingByTurnID.removeValue(forKey: turnID)
                    await recoverByReloadingHistory(
                        conversationID: id,
                        assistantMessageID: assistantMessageID
                    )
                }
                finishStreaming(streamToken)
                return
            }
            if hadPendingStop {
                detachPendingSteers(pendingSteers, requeue: false)
                let secured: Bool
                do {
                    secured = try await requestStopWithRetry(ActiveChatTurn(turnID: turnID, conversationID: id))
                } catch {
                    appendStreamError(error.localizedDescription, assistantMessageID: assistantMessageID)
                    finishStreaming(streamToken)
                    return
                }
                guard !Task.isCancelled, currentStreamToken == streamToken else {
                    return
                }
                if !secured {
                    stopWarningMessage = "Stop could not be confirmed. Pending approvals from this turn may still be active."
                }
            }
            if !hadPendingStop {
                for prompt in pendingSteers {
                    let activeTurn = ActiveChatTurn(turnID: turnID, conversationID: id)
                    let result: SteerSubmissionResult
                    do {
                        result = try await requestSteerWithRetry(activeTurn, prompt: prompt)
                    } catch {
                        removeInFlightSteer(prompt)
                        removeAwaitingEchoSteer(prompt)
                        steerErrorMessage = error.localizedDescription
                        errorReporter.report(error, component: "Chat.steerTurn")
                        continue
                    }
                    guard !Task.isCancelled, currentStreamToken == streamToken else {
                        return
                    }
                    await handleSteerSubmissionResult(
                        result,
                        prompt: prompt,
                        activeTurn: activeTurn
                    )
                }
            }

            var outcome = await runTurnSubscription(
                conversationID: id,
                fromSeq: start.firstSeq,
                ackSeq: lastSeq,
                turnID: turnID,
                assistantMessageID: assistantMessageID,
                lastSeq: &lastSeq
            )
            // Resume the live token stream across mid-turn drops instead of
            // stranding on the disconnected indicator. The turn keeps running
            // durably on the server and the hub replays events with
            // `seq >= from_seq`, so a severed connection — a `stream_dropped`
            // frame, a transient 5xx, or a bare EOF from a proxy request-timeout
            // (`.dropped`/`.interrupted`) — is recoverable: resubscribe from just
            // after the last applied seq (still acking `lastSeq`; resuming from
            // `lastSeq` would re-apply the last event) and keep streaming.
            //
            // The loop continues for the whole life of the turn. A resume only
            // counts toward giving up when it ends *immediately with no new
            // events* — an instant drain-and-close, which is how `follow=false`
            // reports a turn that has finished or rotated out of the hub. A resume
            // that applies new events, or that the server held open for a while
            // (it blocks on the live queue only while a turn is still running —
            // e.g. a quiet, long-running tool call between streamed frames),
            // resets the streak so a healthy turn is never abandoned mid-flight.
            // Only after `maxConsecutiveStreamResumes` instant no-progress closes
            // do we stop and fall back to a history reload. Back off only between
            // those closes so a healthy resume after a real drop is immediate.
            var noProgressResumes = 0
            var resumeDelay = liveReconnectInitialDelaySeconds
            while outcome == .dropped || outcome == .interrupted {
                guard !Task.isCancelled, currentStreamToken == streamToken,
                      noProgressResumes < maxConsecutiveStreamResumes
                else {
                    break
                }
                if noProgressResumes > 0 {
                    try? await Task.sleep(for: .seconds(resumeDelay))
                    guard !Task.isCancelled, currentStreamToken == streamToken else {
                        return
                    }
                }
                let seqBeforeResume = lastSeq
                let resumeStartedAt = Date()
                outcome = await runTurnSubscription(
                    conversationID: id,
                    fromSeq: lastSeq.map { $0 + 1 } ?? start.firstSeq,
                    ackSeq: lastSeq,
                    turnID: turnID,
                    assistantMessageID: assistantMessageID,
                    lastSeq: &lastSeq
                )
                let heldOpen = Date().timeIntervalSince(resumeStartedAt) >= streamResumeLivenessSeconds
                if lastSeq != seqBeforeResume || heldOpen {
                    noProgressResumes = 0
                    resumeDelay = liveReconnectInitialDelaySeconds
                } else {
                    noProgressResumes += 1
                    resumeDelay = min(resumeDelay * 2, liveReconnectMaxDelaySeconds)
                }
            }

            // A superseded send (newer send or conversation switch) must not apply
            // the tail work below — that belongs to the turn that replaced us.
            guard !Task.isCancelled, currentStreamToken == streamToken else {
                return
            }

            switch outcome {
            case .completed(let status):
                // Acknowledge the highest received seq so the server suppresses the
                // disconnect push for a reply we actually saw. Fire-and-forget so
                // UI completion isn't blocked on the ack.
                if let lastSeq {
                    let ackClient = apiClient
                    Task { try? await ackClient.acknowledge(conversationID: id, ackSeq: lastSeq) }
                }
                completeStream(assistantMessageID: assistantMessageID)
                // Turn settled: the server reflects this send, so retire the
                // optimistic mark before refreshing so the authoritative summary
                // (with the reply preview) replaces the held row.
                optimisticPendingByTurnID.removeValue(forKey: turnID)
                await refreshRecentConversations()
                await mergeNewMessages(conversationID: id)
                if status == "cancelled" || status == "failed" || stopRequestedTurnIDs.contains(turnID) {
                    clearRecoverableSteers()
                } else {
                    recoverUnconsumedSteers()
                }
            case .reloadHistory, .dropped, .interrupted:
                if stopRequestedTurnIDs.contains(turnID) {
                    clearRecoverableSteers()
                } else {
                    dropAcceptedSteersAwaitingEcho()
                }
                // The connection dropped before turn_ended, but the turn keeps
                // running durably. Mark it ended for the follow loop FIRST: the
                // always-on follow stream still carries this turn's later tokens,
                // and once `finishStreaming` flips `isStreaming` false it would
                // otherwise render that tail into a fresh `local_follow_` bubble —
                // a duplicate of the partial reply this reload surfaces as a `msg_`
                // row. Suppressing the live re-render lets the durable reply land
                // through history catch-up (and the completion push) instead.
                markTurnEnded(turnID, status: nil)
                // The durable turn has settled; retire the optimistic mark so the
                // reload below surfaces the authoritative server summary.
                optimisticPendingByTurnID.removeValue(forKey: turnID)
                // Reload persisted history silently. Don't write to `errorMessage`:
                // that drives a modal "Chat Error" alert, which would be spurious
                // for a reply that actually succeeded. The disconnected indicator
                // is the appropriate non-modal signal.
                await recoverByReloadingHistory(
                    conversationID: id,
                    assistantMessageID: assistantMessageID
                )
            case .failed(let message):
                // A fatal subscribe failure ends this send. Clear any steer the
                // user had in flight/awaiting echo (as the failed-completion path
                // does) so a stranded text entry doesn't keep blocking re-sending
                // the same steer as a normal draft.
                clearRecoverableSteers()
                appendStreamError(message, assistantMessageID: assistantMessageID)
            }
        } catch is CancellationError {
            _ = await cancelStopQueuedBeforeRegistration(for: turnID)
            // A kickoff cancelled before startTurn returned (switch/new chat right
            // after sending) persisted nothing, so roll back its optimistic row —
            // unless a superseding send for the same conversation now owns it.
            if !startSucceeded {
                rollbackOptimisticSummaryIfUnowned(
                    conversationID: id, turnID: turnID, to: previousSummary
                )
            }
            markStreamStopped(assistantMessageID: assistantMessageID)
        } catch {
            if !(await cancelStopQueuedBeforeRegistration(for: turnID)) {
                recoverPendingSteersAsDraft(for: turnID)
            }
            // Reaching here means starting the turn itself failed — the prompt was
            // never accepted, so there is no durable turn to recover; surface it.
            // Undo the optimistic list change: drop a brand-new conversation's
            // phantom row, or restore an existing conversation's pre-send summary
            // so the failed prompt doesn't stay pinned at the top (the freshness
            // guard would otherwise keep the newer optimistic row).
            if !startSucceeded {
                rollbackOptimisticSummaryIfUnowned(
                    conversationID: id, turnID: turnID, to: previousSummary
                )
            }
            appendStreamError(error.localizedDescription, assistantMessageID: assistantMessageID)
        }
        finishStreaming(streamToken)
    }

    /// Subscribe to a turn and consume its events, mapping connection failures to
    /// a ``TurnSubscriptionOutcome`` instead of throwing. Only a fatal,
    /// non-recoverable failure (e.g. auth) becomes `.failed`; a dropped or
    /// interrupted connection becomes a recoverable outcome because the durable
    /// turn keeps running server-side.
    private func runTurnSubscription(
        conversationID id: String,
        fromSeq: Int,
        ackSeq: Int?,
        turnID: String,
        assistantMessageID: String,
        lastSeq: inout Int?
    ) async -> TurnSubscriptionOutcome {
        var diagnostics = ChatStreamDiagnostics()
        do {
            let events = try await apiClient.subscribeToTurn(
                conversationID: id,
                fromSeq: fromSeq,
                ackSeq: ackSeq
            )
            diagnostics.connectedAt = Date()
            let outcome = try await consumeTurnStream(
                events,
                turnID: turnID,
                assistantMessageID: assistantMessageID,
                lastSeq: &lastSeq,
                diagnostics: &diagnostics
            )
            // A clean turn_ended is the happy path; only breadcrumb the cases the
            // recovery layer otherwise swallows silently (the reported symptom).
            if !outcome.isCompleted {
                reportStreamOutcome(
                    phase: "send-subscribe",
                    outcome: Self.outcomeLabel(outcome),
                    error: nil,
                    fromSeq: fromSeq,
                    diagnostics: diagnostics
                )
            }
            return outcome
        } catch is CancellationError {
            reportStreamOutcome(
                phase: "send-subscribe",
                outcome: "interrupted",
                error: CancellationError(),
                fromSeq: fromSeq,
                diagnostics: diagnostics
            )
            return .interrupted
        } catch let error as ChatAPIError {
            let outcome: TurnSubscriptionOutcome
            if case .server(let statusCode, _) = error, statusCode == 410 {
                outcome = .reloadHistory
            } else if case .server(let statusCode, _) = error, statusCode >= 500 {
                // The producer keeps running through a transient server error
                // on the subscribe GET; treat it as resumable rather than
                // failing the whole turn.
                outcome = .dropped
            } else {
                outcome = .failed(error.localizedDescription)
            }
            reportStreamOutcome(
                phase: "send-subscribe",
                outcome: Self.outcomeLabel(outcome),
                error: error,
                fromSeq: fromSeq,
                diagnostics: diagnostics
            )
            return outcome
        } catch {
            // A network drop establishing or reading the stream. The turn is
            // durable, so recover by reloading history.
            reportStreamOutcome(
                phase: "send-subscribe",
                outcome: "interrupted",
                error: error,
                fromSeq: fromSeq,
                diagnostics: diagnostics
            )
            return .interrupted
        }
    }

    /// Consume a turn's SSE events, applying them to the assistant bubble and
    /// tracking the highest seq seen. Throws only on a mid-stream connection drop.
    private func consumeTurnStream(
        _ events: AsyncThrowingStream<ChatStreamEvent, Error>,
        turnID: String,
        assistantMessageID: String,
        lastSeq: inout Int?,
        diagnostics: inout ChatStreamDiagnostics
    ) async throws -> TurnSubscriptionOutcome {
        for try await event in events {
            // Connection-level counters reflect real socket activity (any frame,
            // including heartbeats and other turns), so they track whether bytes
            // were still flowing before the drop — the idle-timeout signal.
            diagnostics.eventCount += 1
            diagnostics.lastEventAt = Date()
            if Task.isCancelled {
                break
            }
            // The hub dropped this subscriber (queue overflow / shutdown). Bail so
            // the caller can resubscribe from the last applied seq. Carries no
            // seq/turn id, so handle it before the turn filter and seq tracking.
            if event.type == .streamDropped {
                return .dropped
            }
            // The conversation stream carries every turn's events. In this
            // send-and-watch flow only apply events for the turn we started;
            // ignore a turn started concurrently elsewhere in the conversation.
            if let eventTurnID = event.turnID, eventTurnID != turnID {
                continue
            }
            // Renderable-signal diagnostics are scoped to our turn (after the turn
            // filter) so a concurrent turn's tool call can't set `saw_tool_call`
            // for the send we're diagnosing.
            diagnostics.lastEventType = event.type
            if event.type == .toolCall {
                diagnostics.sawToolCall = true
            }
            // Advance the ack/resume cursor ONLY for our own turn's events
            // (`turn_id == turnID`). A seq-bearing event that isn't ours — a
            // no-turn_id `message` nudge published by non-streaming saves, or a
            // concurrent turn's frame — must not inflate `ack_seq`: the hub treats
            // any turn with `ended_seq <= ack_seq` as delivered, so acking past a
            // skipped turn's `turn_ended` would suppress its disconnect push.
            // Resuming from our own seq still can't miss our events — the server
            // replays all seqs >= from_seq, so interleaved frames are re-filtered.
            // Connection-control frames (heartbeat, no-turn_id message) carry no
            // renderable content; `apply` is a no-op for them.
            if event.turnID == turnID, let seq = event.seq {
                if let current = lastSeq {
                    lastSeq = max(current, seq)
                } else {
                    lastSeq = seq
                }
                diagnostics.lastSeq = lastSeq
                recordAppliedSeq(seq)
            }
            apply(streamEvent: event, assistantMessageID: assistantMessageID)
            if event.type == .turnEnded {
                // Mark this turn ended so a late, out-of-step copy of one of its
                // tokens arriving on the always-on follow stream after the send
                // finishes can't spawn a duplicate live bubble for it.
                markTurnEnded(turnID, status: event.status)
                // A failed turn carries its error on the terminal event; surface
                // it so the bubble shows the failure rather than an empty reply.
                if event.status != "cancelled",
                   let message = event.errorMessage,
                   !message.isEmpty
                {
                    appendStreamError(message, assistantMessageID: assistantMessageID)
                }
                return .completed(status: event.status)
            }
        }
        // The loop only exits here when the stream closed (or was cancelled)
        // without a turn_ended or stream_dropped frame — i.e. interrupted.
        return .interrupted
    }

    /// Recover from an interrupted/already-complete/rotated-out turn by dropping
    /// the optimistic assistant placeholder and reloading persisted history, so
    /// the durably saved reply surfaces instead of a fabricated completion.
    ///
    /// Deliberately silent: it does not write to `errorMessage`, because a
    /// recovered disconnect is not a chat error and surfacing one would pop a
    /// spurious modal alert for a reply that actually succeeded.
    private func recoverByReloadingHistory(
        conversationID id: String,
        assistantMessageID: String
    ) async {
        removeLocalAssistantPlaceholder(assistantMessageID)
        await refreshRecentConversations()
        await mergeNewMessages(conversationID: id)
    }

    /// Reset shared streaming state, but only for the still-current send: a
    /// superseded task must not nil out the new turn's streamTask.
    private func finishStreaming(_ streamToken: UUID) {
        if currentStreamToken == streamToken {
            isStreaming = false
            streamTask = nil
            currentStreamToken = nil
            if let turnID = activeTurn?.turnID {
                registeredTurnIDs.remove(turnID)
                pendingStopTurnIDs.remove(turnID)
                stopAfterRegistrationByTurnID.removeValue(forKey: turnID)
                stopRequestedTurnIDs.remove(turnID)
                pendingSteersByTurnID[turnID] = nil
            }
            activeTurn = nil
            Task { [weak self] in
                await self?.sendNextQueuedFollowUpSteerIfReady()
            }
        }
    }

    func stopTurn() async {
        guard let activeTurn else {
            cancelStream()
            return
        }
        stopWarningMessage = nil
        stopRequestedTurnIDs.insert(activeTurn.turnID)
        guard registeredTurnIDs.contains(activeTurn.turnID) else {
            pendingStopTurnIDs.insert(activeTurn.turnID)
            stopAfterRegistrationByTurnID[activeTurn.turnID] = activeTurn.conversationID
            return
        }
        let secured: Bool
        do {
            secured = try await requestStopWithRetry(activeTurn)
        } catch {
            guard shouldSurfaceStopWarning(for: activeTurn) else {
                return
            }
            errorMessage = error.localizedDescription
            errorReporter.report(error, component: "Chat.stopTurn")
            return
        }
        if !secured {
            guard shouldSurfaceStopWarning(for: activeTurn) else {
                return
            }
            stopWarningMessage = "Stop could not be confirmed. Pending approvals from this turn may still be active."
        }
    }

    func sendSteerDraft() async {
        let prompt = draftText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else {
            return
        }
        steerErrorMessage = nil
        // No turn is running, so there is nothing to steer: the composer text is
        // just a normal message. Send it as one (sendDraft consumes and clears
        // the composer).
        guard let activeTurn else {
            await sendDraft()
            return
        }
        guard !hasPendingSteer(prompt) else {
            return
        }
        inFlightSteers.append(prompt)
        guard registeredTurnIDs.contains(activeTurn.turnID) else {
            pendingSteersByTurnID[activeTurn.turnID, default: []].append(prompt)
            return
        }
        do {
            let result = try await requestSteerWithRetry(activeTurn, prompt: prompt)
            await handleSteerSubmissionResult(result, prompt: prompt, activeTurn: activeTurn)
        } catch {
            removeInFlightSteer(prompt)
            removeAwaitingEchoSteer(prompt)
            steerErrorMessage = error.localizedDescription
            errorReporter.report(error, component: "Chat.steerTurn")
        }
    }

    private func handleSteerSubmissionResult(
        _ result: SteerSubmissionResult,
        prompt: String,
        activeTurn: ActiveChatTurn
    ) async {
        let isCurrentTurn = self.activeTurn == activeTurn
        let isSameConversation = conversationID == activeTurn.conversationID
        let originalTurnEnded = endedTurnIDs.contains(activeTurn.turnID)
        let originalTurnEndedCleanly = canRecoverSteerAfterTurnEnded(activeTurn.turnID)
        // A late result for a turn that is neither active nor ended was superseded
        // (cancelStream/a new send cleared THIS turn's steer arrays). The steer
        // collections are keyed by prompt text, so a newer turn may already hold an
        // identical prompt; removing by text here would untrack the newer turn's
        // steer. Don't touch the shared arrays — just drop a stale matching draft.
        guard isCurrentTurn || originalTurnEnded else {
            clearComposerIfMatching(prompt)
            return
        }
        switch result {
        case .accepted:
            guard removeInFlightSteer(prompt) else {
                return
            }
            if originalTurnEnded {
                guard originalTurnEndedCleanly, isCurrentTurn || isSameConversation else {
                    clearComposerIfMatching(prompt)
                    return
                }
                clearComposerIfMatching(prompt)
                queuedFollowUpSteers.append(prompt)
                await sendNextQueuedFollowUpSteerIfReady()
            } else {
                // Clear the composer as soon as the steer is accepted (matching
                // the web composer), not when the echo later arrives. The
                // match-guarded clear preserves a fresh edit the user typed while
                // the steer was in flight; awaitingEchoSteers still tracks the
                // prompt independently for recovery if the stream drops.
                clearComposerIfMatching(prompt)
                awaitingEchoSteers.append(prompt)
            }
        case .finished:
            removeInFlightSteer(prompt)
            removeAwaitingEchoSteer(prompt)
            guard isCurrentTurn || (isSameConversation && originalTurnEnded) else {
                clearComposerIfMatching(prompt)
                return
            }
            if originalTurnEnded, !originalTurnEndedCleanly {
                clearComposerIfMatching(prompt)
                return
            }
            clearComposerIfMatching(prompt)
            queuedFollowUpSteers.append(prompt)
            await sendNextQueuedFollowUpSteerIfReady()
        case .error:
            removeInFlightSteer(prompt)
            removeAwaitingEchoSteer(prompt)
            if isCurrentTurn {
                steerErrorMessage = "Could not steer the assistant. Please try again."
            } else if originalTurnEnded,
                      canRecoverSteerAfterTurnEnded(activeTurn.turnID, defaultWhenUnknown: true),
                      isSameConversation {
                recoverSteerAsDraft(prompt)
            }
        }
    }

    private enum SteerSubmissionResult {
        case accepted
        case finished
        case error
    }

    private func requestStopWithRetry(_ activeTurn: ActiveChatTurn) async throws -> Bool {
        let attempts = 5
        for attempt in 0..<attempts {
            do {
                _ = try await apiClient.cancelTurn(
                    turnID: activeTurn.turnID,
                    conversationID: activeTurn.conversationID
                )
                return true
            } catch let error as ChatAPIError {
                if !Self.isRetryableTurnControlError(error) {
                    errorReporter.report(error, component: "Chat.stopTurn")
                    return false
                }
            } catch let error as URLError {
                guard Self.isRetryableTurnTransportError(error) else {
                    throw error
                }
            }
            await sleepBeforeTurnControlRetry(attempt: attempt, attempts: attempts)
        }
        return false
    }

    private func cancelStopQueuedBeforeRegistration(for turnID: String) async -> Bool {
        guard let conversationID = stopAfterRegistrationByTurnID.removeValue(forKey: turnID) else {
            return false
        }
        detachPendingSteers(pendingSteersByTurnID.removeValue(forKey: turnID) ?? [], requeue: false)
        let turnToCancel = ActiveChatTurn(turnID: turnID, conversationID: conversationID)
        let cancellationTask = Task { [weak self] in
            guard let self else {
                return false
            }
            do {
                return try await self.requestStopWithRetry(turnToCancel)
            } catch {
                self.errorMessage = error.localizedDescription
                self.errorReporter.report(error, component: "Chat.stopTurn")
                return false
            }
        }
        let secured = await cancellationTask.value
        if !secured, shouldSurfaceStopWarning(for: turnToCancel) {
            stopWarningMessage = "Stop could not be confirmed. Pending approvals from this turn may still be active."
        }
        return true
    }

    private func requestSteerWithRetry(
        _ activeTurn: ActiveChatTurn,
        prompt: String
    ) async throws -> SteerSubmissionResult {
        let attempts = 5
        var lastWasNotFound = false
        var hadAmbiguousFailure = false
        for attempt in 0..<attempts {
            do {
                _ = try await apiClient.steerTurn(
                    turnID: activeTurn.turnID,
                    conversationID: activeTurn.conversationID,
                    prompt: prompt
                )
                return .accepted
            } catch let error as ChatAPIError {
                switch error {
                case .server(let statusCode, _) where statusCode == 409:
                    return hadAmbiguousFailure ? .error : .finished
                case .server(let statusCode, _) where statusCode == 404:
                    lastWasNotFound = true
                case .server(let statusCode, _) where statusCode < 500:
                    errorReporter.report(error, component: "Chat.steerTurn")
                    return .error
                default:
                    hadAmbiguousFailure = true
                    lastWasNotFound = false
                }
            } catch let error as URLError {
                guard Self.isRetryableTurnTransportError(error) else {
                    throw error
                }
                hadAmbiguousFailure = true
                lastWasNotFound = false
            }
            await sleepBeforeTurnControlRetry(attempt: attempt, attempts: attempts)
        }
        return lastWasNotFound && !hadAmbiguousFailure ? .finished : .error
    }

    private static func isRetryableTurnControlError(_ error: ChatAPIError) -> Bool {
        if case .server(let statusCode, _) = error {
            return statusCode == 404 || statusCode >= 500
        }
        return false
    }

    private static func isRetryableTurnTransportError(_ error: URLError) -> Bool {
        switch error.code {
        case .timedOut, .networkConnectionLost, .notConnectedToInternet,
             .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed,
             .badServerResponse, .resourceUnavailable:
            return true
        default:
            return false
        }
    }

    private func sleepBeforeTurnControlRetry(attempt: Int, attempts: Int) async {
        guard attempt < attempts - 1 else {
            return
        }
        try? await Task.sleep(for: .milliseconds(150 * (attempt + 1)))
    }

    private func recoverUnconsumedSteers() {
        guard !awaitingEchoSteers.isEmpty else {
            return
        }
        queuedFollowUpSteers.append(contentsOf: awaitingEchoSteers)
        awaitingEchoSteers.removeAll()
    }

    private func clearRecoverableSteers() {
        for prompt in inFlightSteers + awaitingEchoSteers + queuedFollowUpSteers {
            clearComposerIfMatching(prompt)
        }
        inFlightSteers.removeAll()
        awaitingEchoSteers.removeAll()
        queuedFollowUpSteers.removeAll()
    }

    private func dropAcceptedSteersAwaitingEcho() {
        for prompt in awaitingEchoSteers {
            clearComposerIfMatching(prompt)
        }
        awaitingEchoSteers.removeAll()
    }

    private func markTurnEnded(_ turnID: String, status: String?) {
        endedTurnIDs.insert(turnID)
        if let status {
            endedTurnStatusByTurnID[turnID] = status
        }
    }

    /// Whether a steer for a turn that has already ended can still be recovered
    /// (re-sent as a follow-up or restored to the draft). A turn that ended as
    /// `cancelled`/`failed` is never recoverable. `defaultWhenUnknown` is the
    /// answer when the turn ended without a recorded status (e.g. it was
    /// finalized off the always-on follow stream): an accepted steer treats an
    /// unknown status conservatively (false), a failed-steer recovery
    /// optimistically (true).
    private func canRecoverSteerAfterTurnEnded(
        _ turnID: String,
        defaultWhenUnknown: Bool = false
    ) -> Bool {
        guard let status = endedTurnStatusByTurnID[turnID] else {
            return defaultWhenUnknown
        }
        return status != "cancelled" && status != "failed"
    }

    private func shouldSurfaceStopWarning(for stoppedTurn: ActiveChatTurn) -> Bool {
        activeTurn == stoppedTurn
            || (activeTurn == nil && conversationID == stoppedTurn.conversationID)
    }

    /// Tear down steers that were queued before their turn registered: drop their
    /// in-flight/awaiting-echo tracking and clear a matching steer draft. With
    /// `requeue` true the prompt is re-queued as a normal follow-up (the turn
    /// finished durably before it could be steered); with false it is discarded
    /// (e.g. a stop superseded it).
    private func detachPendingSteers(_ prompts: [String], requeue: Bool) {
        for prompt in prompts {
            removeInFlightSteer(prompt)
            removeAwaitingEchoSteer(prompt)
            if requeue {
                queuedFollowUpSteers.append(prompt)
            }
            clearComposerIfMatching(prompt)
        }
    }

    private func hasPendingSteer(_ prompt: String) -> Bool {
        inFlightSteers.contains(prompt)
            || awaitingEchoSteers.contains(prompt)
            || queuedFollowUpSteers.contains(prompt)
            || pendingSteersByTurnID.values.contains { $0.contains(prompt) }
    }

    /// Ensure a steer prompt is present in the composer so the user can edit or
    /// resend it. The main composer doubles as the steer input, so "recovering a
    /// steer as a draft" just means making sure its text is in `draftText`;
    /// `appendPromptToDraft` is a no-op when the prompt is already there.
    private func recoverSteerAsDraft(_ prompt: String) {
        appendPromptToDraft(prompt)
    }

    /// Clear the composer if it still holds exactly this (consumed) steer text.
    /// The match guard is what stops a fresh edit the user typed while a steer
    /// was in flight from being clobbered.
    private func clearComposerIfMatching(_ prompt: String) {
        if draftText.trimmingCharacters(in: .whitespacesAndNewlines) == prompt {
            draftText = ""
        }
    }

    private func recoverPendingSteersAsDraft(for turnID: String) {
        let prompts = pendingSteersByTurnID.removeValue(forKey: turnID) ?? []
        guard !prompts.isEmpty else {
            return
        }
        for prompt in prompts {
            removeInFlightSteer(prompt)
            removeAwaitingEchoSteer(prompt)
            recoverSteerAsDraft(prompt)
        }
    }

    private func appendPromptToDraft(_ prompt: String) {
        let prompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else {
            return
        }
        let existingDraft = draftText.trimmingCharacters(in: .whitespacesAndNewlines)
        if existingDraft.isEmpty {
            draftText = prompt
            return
        }
        let existingLines = existingDraft
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        guard !existingLines.contains(prompt) else {
            return
        }
        draftText += draftText.hasSuffix("\n") ? prompt : "\n\(prompt)"
    }

    @discardableResult
    private func removeInFlightSteer(_ prompt: String) -> Bool {
        if let index = inFlightSteers.firstIndex(of: prompt) {
            inFlightSteers.remove(at: index)
            return true
        }
        return false
    }

    private func removeAwaitingEchoSteer(_ prompt: String) {
        if let index = awaitingEchoSteers.firstIndex(of: prompt) {
            awaitingEchoSteers.remove(at: index)
        }
    }

    private func sendNextQueuedFollowUpSteerIfReady() async {
        guard !isStreaming, !queuedFollowUpSteers.isEmpty else {
            return
        }
        let followUp = queuedFollowUpSteers.removeFirst()
        let remainingQueuedFollowUps = queuedFollowUpSteers
        queuedFollowUpSteers = []
        clearComposerIfMatching(followUp)
        let preservedDraftText = draftText
        let preservedDraftAttachments = draftAttachments
        draftText = followUp
        draftAttachments = []
        await sendDraft()
        draftText = preservedDraftText
        draftAttachments = preservedDraftAttachments
        queuedFollowUpSteers = remainingQueuedFollowUps + queuedFollowUpSteers
    }

    func cancelStream(sendQueuedStopCancel: Bool = true) {
        guard streamTask != nil || isStreaming else {
            return
        }
        streamTask?.cancel()
        streamTask = nil
        if sendQueuedStopCancel, let activeTurn,
           stopAfterRegistrationByTurnID[activeTurn.turnID] != nil {
            let turnID = activeTurn.turnID
            Task { [weak self] in
                await self?.cancelStopQueuedBeforeRegistration(for: turnID)
            }
        }
        isStreaming = false
        currentStreamToken = nil
        activeTurn = nil
        registeredTurnIDs.removeAll()
        pendingStopTurnIDs.removeAll()
        stopRequestedTurnIDs.removeAll()
        pendingSteersByTurnID.removeAll()
        clearRecoverableSteers()
        flushPendingTextNow()
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
            errorReporter.report(error, component: "Chat.importAttachment")
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
                errorReporter.report(error, component: "Chat.removeAttachment")
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
            errorReporter.report(error, component: "Chat.confirmTool")
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
            errorReporter.report(error, component: "Chat.downloadAttachment")
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
            errorReporter.report(error, component: "Chat.pendingApprovals")
        }
    }

    func reconnectLiveUpdates() async {
        // Don't optimistically flip the indicator to connected: `startLiveEvents`
        // only schedules the connect, so a fresh connect that immediately fails
        // (the hostile front door) would flash "connected" before the loop sets it
        // back to disconnected. A successful connect sets it true in
        // `handleLiveReconnect`; a failing one leaves the honest disconnected state.
        startLiveEvents()
        // The account-global activity stream is torn down with the scene on
        // background (its Task is suspended/killed by the OS); restart it on the
        // same foreground transition so list updates for OTHER conversations
        // resume, and refresh once to close any gap missed while backgrounded.
        startActivityStream()
    }

    /// Subscribe to the account-global conversation-activity stream and refresh
    /// the conversation list whenever any owned conversation changes.
    ///
    /// This is what keeps the list fresh for activity OUTSIDE the open thread —
    /// a brand-new chat, or a delegated/scheduled/background reply landing in a
    /// conversation the user isn't currently viewing — none of which the
    /// per-conversation follow stream (`startLiveEvents`) observes. The stream is
    /// advisory: every ping just triggers an authoritative
    /// `refreshRecentConversations`, and a (re)connect refreshes once to close
    /// any gap missed while disconnected. Mirrors `startLiveEvents`' capped
    /// exponential-backoff reconnect loop and weak-`self` discipline so logout/
    /// deinit tears the connection down.
    private func startActivityStream() {
        activityStreamTask?.cancel()
        let client = apiClient
        let initialDelay = liveReconnectInitialDelaySeconds
        let maxDelay = liveReconnectMaxDelaySeconds
        activityStreamTask = Task { [weak self] in
            var delay = initialDelay
            while !Task.isCancelled {
                do {
                    let stream = try await client.connectActivityStream()
                    // A successful (re)connect resets the backoff and refreshes
                    // once: pings are ephemeral, so anything that changed while
                    // we were disconnected is caught by this single pull.
                    delay = initialDelay
                    await self?.refreshRecentConversations()
                    for try await _ in stream {
                        if Task.isCancelled {
                            break
                        }
                        // `self` deallocated -> deinit already cancelled this
                        // task; stop instead of spinning reconnects.
                        guard self != nil else {
                            return
                        }
                        await self?.refreshRecentConversations()
                    }
                } catch {
                    // Connection failed or dropped; fall through to backoff+retry.
                }
                if Task.isCancelled {
                    break
                }
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, maxDelay)
            }
        }
    }

    /// The hub buffer rotated past this client's resume cursor (a 410). Clear it so
    /// the follow loop tails from the head until a fresh frame advances it again,
    /// instead of re-requesting the gone seq every reconnect.
    private func markFollowBufferRotated() {
        highestAppliedSeq = nil
    }

    /// Whether a scene-phase transition should trigger a live-updates reconnect.
    /// Only a real return from the BACKGROUND should: a transient
    /// `.inactive → .active` blip (Control Center, the app switcher, a
    /// notification banner) must not tear down and restart a healthy follow
    /// connection. Kept SwiftUI-agnostic (booleans) so the gating is unit-testable
    /// without importing the scene-phase type into the view model.
    func shouldReconnectOnForeground(cameFromBackground: Bool, isNowActive: Bool) -> Bool {
        cameFromBackground && isNowActive
    }

    /// Whether the chat thread's message list should be realized into the view
    /// hierarchy for the current scene phase.
    ///
    /// An offscreen background *launch* (push / state restoration / snapshot)
    /// must keep the `LazyVStack` out of the tree entirely: laying out a restored
    /// thread while inactive overruns the ~10 s `scene-update` watchdog
    /// (`docs/design/ios-chat-layout-watchdog-crash.md`).
    ///
    /// But once the thread has been realized while active, it must stay mounted
    /// across later background transitions. Tearing it down on every
    /// `.active → .background` swap runs a `LazyLayoutViewCache.updateItemPhases`
    /// teardown transaction at the exact moment iOS is trying to suspend the app,
    /// which overruns the tighter 5 s `process-exit` (suspend) watchdog and is
    /// killed with `0x8BADF00D` (the suspend-variant recurrence in build 21).
    /// Keeping it mounted means no fresh transaction is kicked at suspend time.
    ///
    /// Kept SwiftUI-agnostic (booleans) so the gating is unit-testable without
    /// importing the scene-phase type into the view model.
    func shouldRenderThread(isActive: Bool, hasMountedBefore: Bool) -> Bool {
        isActive || hasMountedBefore
    }

    /// The seq the follow loop should resume from on a (re)connect: just after the
    /// highest seq this client has applied, so a mid-turn reconnect replays the
    /// frames produced during the drop into the live bubble instead of tailing
    /// from the head and skipping them until `turn_ended`. Falls back to -1 (tail)
    /// before anything has been applied.
    private func followResumeFromSeq() -> Int {
        highestAppliedSeq.map { $0 + 1 } ?? -1
    }

    private func startLiveEvents() {
        liveEventsTask?.cancel()
        guard let conversationID else {
            return
        }
        // Capture the API client (a value type whose only reference is the
        // AuthManager, never the view model) and the backoff bounds up front.
        // The follow loop below never binds a strong `self` across its awaits —
        // every mutation goes through a weak `self?` hop — so releasing the view
        // model (e.g. on logout) lets `deinit` run, which cancels this task and
        // tears down the open SSE connection instead of stranding it.
        let client = apiClient
        let initialDelay = liveReconnectInitialDelaySeconds
        let maxDelay = liveReconnectMaxDelaySeconds
        liveEventsTask = Task { [weak self] in
            // Reconnect loop: a dropped follow stream self-heals with capped
            // exponential backoff instead of stranding the disconnected
            // indicator until a manual tap. The loop exits only on cancellation
            // (conversation change, logout/deinit) or a deliberate stop.
            var delay = initialDelay
            while !Task.isCancelled {
                // Re-read the cursor each attempt: `ackSeq` marks everything applied
                // as delivered (push suppression); `fromSeq` resumes the replay just
                // past it so a mid-turn reconnect doesn't skip frames produced during
                // the drop. A prior 410 cleared the cursor, so this tails the head
                // until a fresh frame advances it (no re-request of the gone seq).
                let ackSeq = self?.highestAppliedSeq ?? -1
                let fromSeq = self?.followResumeFromSeq() ?? -1
                var deliberateStop = false
                var connected = false
                var streamError: Error?
                do {
                    let stream = try await client.connectEvents(
                        conversationID: conversationID,
                        fromSeq: fromSeq,
                        ackSeq: ackSeq
                        // No `event_types` filter: the always-on follow stream now
                        // carries token frames too, so a turn started elsewhere (or
                        // after our own send task gave up) streams live into a
                        // bubble via `handleLiveEvent`, not just a history reload on
                        // completion.
                    )
                    // Call through the weak `self?` at each suspension point
                    // instead of binding a strong `self`: a binding would keep
                    // the view model (and its open SSE connection) alive across
                    // the indefinite follow loop, so logout/deinit could never
                    // tear it down.
                    connected = true
                    await self?.handleLiveReconnect(conversationID: conversationID)
                    // A successful (re)connect resets the backoff.
                    delay = initialDelay
                    for try await event in stream {
                        if Task.isCancelled {
                            break
                        }
                        let shouldContinue = await self?.handleLiveEvent(
                            event,
                            conversationID: conversationID,
                            client: client
                        )
                        // `self` deallocated mid-loop -> nil -> stop following.
                        guard let shouldContinue else { return }
                        if !shouldContinue {
                            deliberateStop = true
                            break
                        }
                    }
                } catch {
                    // Connection failed or dropped mid-stream; fall through to
                    // surface the disconnected state and retry after a backoff.
                    streamError = error
                    // A 410 means the resume cursor rotated out of the hub buffer.
                    // Clear it so the next attempt (and every one after, until a
                    // fresh frame) tails the head instead of re-requesting the gone
                    // seq; the catch-up reload below fills the gap.
                    if case ChatAPIError.server(let statusCode, _) = error, statusCode == 410 {
                        self?.markFollowBufferRotated()
                    }
                }

                if Task.isCancelled || deliberateStop {
                    break
                }
                // Breadcrumb every involuntary disconnect (the disconnected-
                // indicator cause), whether the stream threw or was closed cleanly
                // by an idle proxy / server shutdown (a clean EOF finishes the loop
                // without throwing). The error — or its absence — tells an idle
                // timeout from a connection loss from a clean server-side close.
                self?.reportLiveStreamDrop(conversationID: conversationID, error: streamError)
                self?.markLiveUpdatesDisconnectedIfActive()
                // Catch up persisted history over plain HTTP, but ONLY when the
                // connect itself failed. When the front door makes SSE unusable
                // (buffered/severed framing) the connect keeps throwing and
                // `handleLiveReconnect` never runs, so without this a finished turn
                // would strand until a manual refresh — a short GET succeeds where
                // the long-lived SSE doesn't. When the connect SUCCEEDED,
                // `handleLiveReconnect` already caught up and the next reconnect
                // will again, so catching up here too just doubles the GETs on a
                // healthy-but-flapping link.
                if !connected {
                    await self?.catchUpPersistedHistory(conversationID: conversationID)
                }
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, maxDelay)
            }
        }
    }

    private func handleLiveReconnect(conversationID: String) async {
        liveUpdatesConnected = true
        // Reload on (re)connect: the stream tails from the live head, so a
        // reconnect after a drop resumes at the new head and misses events
        // published while offline. Message content always comes from persisted
        // history, so a reload closes that gap.
        await catchUpPersistedHistory(conversationID: conversationID)
    }

    /// Reconcile persisted history over plain HTTP, independent of the SSE follow
    /// stream's health. Called both on a successful (re)connect and on every
    /// involuntary disconnect, so a turn that finishes while the follow stream is
    /// unusable (a front door that buffers/severs long-lived SSE) still surfaces
    /// within one backoff interval instead of stranding until a manual refresh.
    /// Skipped while a send is actively streaming: the send path owns the ack
    /// cursor and the history merge then (see ``runSendTurn``).
    private func catchUpPersistedHistory(conversationID: String) async {
        guard !isStreaming else {
            return
        }
        await mergeNewMessages(conversationID: conversationID)
        await refreshRecentConversations()
    }

    /// Handle one follow-stream event. Returns false when the loop should stop.
    private func handleLiveEvent(
        _ event: ChatStreamEvent,
        conversationID: String,
        client: ChatAPIClient
    ) async -> Bool {
        switch event.type {
        case .turnEnded, .message:
            // Finalize the live-rendered bubble FIRST (flush, mark complete,
            // unmap, record ended) so it stops spinning even when the merge below
            // is skipped (a turn that ends while our own send is streaming) or
            // returns an empty delta, and so the merge then sees an unmapped
            // `local_` bubble it can drop and replace with the persisted reply.
            if event.type == .turnEnded, let turnID = event.turnID {
                finalizeLiveFollowBubble(turnID: turnID, status: event.status)
            }
            // Only surface and acknowledge while this device is NOT actively
            // streaming its own turn: during a send the send path owns the ack
            // cursor, and advancing it / acking here for a turn we don't surface
            // would let the hub treat that turn as delivered (ended_seq <=
            // ack_seq) and suppress its disconnect push.
            if !isStreaming {
                if let seq = event.seq {
                    recordAppliedSeq(seq)
                }
                await mergeNewMessages(conversationID: conversationID)
                await refreshRecentConversations()
                // Acknowledge the turn_ended seq so the server marks the reply
                // delivered and suppresses the disconnect push. Fire-and-forget.
                if event.type == .turnEnded, let seq = event.seq {
                    Task { try? await client.acknowledge(conversationID: conversationID, ackSeq: seq) }
                }
            }
        case .userInput:
            // Skip a late follow-stream echo for a turn that has already ended
            // (its persisted steering row is reconciled via history), mirroring
            // the token path's `endedTurnIDs` guard, so a lagging copy can't
            // append a duplicate local user bubble.
            if !isStreaming, let turnID = event.turnID, !endedTurnIDs.contains(turnID) {
                if let seq = event.seq {
                    recordAppliedSeq(seq)
                }
                appendUserInput(event)
            }
        case .text, .toolCall, .toolResult, .attachment:
            // Render only visible reply content from the always-on follow stream.
            // `.error` and tool-confirmation frames are deliberately NOT applied
            // here: they drive global UI (a modal "Chat Error" alert, a tool
            // approval sheet) that must only fire for a turn THIS device is
            // actively driving — never for one observed passively on the follow
            // stream (started on another device or by a schedule). A failed
            // follow turn still surfaces via its `turn_ended` + history reload.
            applyLiveFollowToken(event)
        case .connected:
            liveUpdatesConnected = true
        case .heartbeat, .turnStarted, .streamDropped, .error,
             .toolConfirmationRequest, .toolConfirmationResult:
            break
        }
        return true
    }

    /// Render a token frame from the always-on follow stream into a per-turn
    /// assistant bubble, so a turn started elsewhere — another device, or one
    /// whose local send task already gave up to a history reload — streams live.
    ///
    /// Skipped while a local send is in flight (`isStreaming`): the send path owns
    /// rendering its own turn, and the follow stream carries the same tokens, so
    /// applying them here too would double-render. Also skipped once a turn has
    /// ended, so a late out-of-step token can't resurrect a finished turn.
    private func applyLiveFollowToken(_ event: ChatStreamEvent) {
        guard !isStreaming, let turnID = event.turnID, !endedTurnIDs.contains(turnID) else {
            return
        }
        // `makeLiveFollowBubble` is idempotent and recreates the bubble if a
        // history merge dropped it out from under a still-running turn: the cached
        // mapping alone would leave `apply` no-oping on a now-missing id and
        // silently discard every token after the drop.
        let bubbleID = makeLiveFollowBubble(for: turnID)
        if let seq = event.seq {
            recordAppliedSeq(seq)
        }
        apply(streamEvent: event, assistantMessageID: bubbleID)
    }

    /// Create (once) and map a local assistant placeholder bubble for a turn
    /// observed live on the follow stream. The `local_` prefix means the bubble is
    /// dropped and replaced by the persisted reply on the next history merge.
    private func makeLiveFollowBubble(for turnID: String) -> String {
        let bubbleID = "local_follow_\(turnID)"
        if !messages.contains(where: { $0.id == bubbleID }) {
            messages.append(
                ChatMessage(
                    id: bubbleID,
                    role: .assistant,
                    text: "",
                    createdAt: Date(),
                    toolCalls: [],
                    attachments: [],
                    isLoading: true,
                    status: .running,
                    // No profile: a turn observed passively on the follow stream may
                    // have run under a different profile than this device currently
                    // has selected, so don't stamp `selectedProfileID` and mislabel
                    // it. The persisted reply carries the real profile on merge.
                    processingProfileID: nil,
                    errorTraceback: nil,
                    turnID: turnID
                )
            )
        }
        liveFollowBubbleByTurnID[turnID] = bubbleID
        return bubbleID
    }

    /// Finalize a live-rendered turn: flush its buffered text and mark its bubble
    /// complete (so it stops spinning), and record the turn ended. The bubble is
    /// left HELD (still mapped): it is reconciled — dropped and replaced by the
    /// canonical persisted row — only once that row arrives, matched by turn id
    /// (see ``reconcileLiveFollowBubbles``). Holding it means a turn whose
    /// persisted reply lags (an empty or unrelated delta at `turn_ended`) keeps
    /// showing its completed streamed text rather than vanishing or stranding as a
    /// stuck spinner.
    private func finalizeLiveFollowBubble(turnID: String, status: String?) {
        markTurnEnded(turnID, status: status)
        guard let bubbleID = liveFollowBubbleByTurnID[turnID],
              let index = messages.firstIndex(where: { $0.id == bubbleID })
        else {
            return
        }
        if let pending = pendingTextByMessageID.removeValue(forKey: bubbleID) {
            messages[index].text += pending
        }
        messages[index].isLoading = false
        if messages[index].status != .failed {
            messages[index].status = .complete
        }
        // A turn stopped on another device finalizes here with no streamed text
        // if the persisted stopped row hasn't merged yet; show the same stopped
        // marker the send-stream path uses rather than an empty bubble.
        if status == "cancelled", messages[index].text.isEmpty {
            messages[index].text = "Response stopped."
        }
    }

    private func markLiveUpdatesDisconnectedIfActive() {
        // Don't surface the disconnected indicator while a send is actively
        // streaming: the send path owns the live connection then and resumes the
        // turn across drops on its own (see `runSendTurn`), so a transient
        // follow-stream drop here is not a user-visible disconnect.
        if !Task.isCancelled, !isStreaming {
            liveUpdatesConnected = false
        }
    }

    /// Emit a breadcrumb when a send-and-watch subscription ends without a clean
    /// `turn_ended`. Routed to both os.Logger (live, when tethered) and
    /// ErrorReporter (persisted to the backend error log / diagnostics export,
    /// survives the app being backgrounded). Diagnoses the reported "streaming
    /// stops after the first tool call": `url_error_code` distinguishes an idle
    /// timeout (proxy/buffering) from a connection loss (background/suspend) from
    /// an app-side cancel, and `saw_tool_call` confirms whether the drop landed
    /// right after a tool call.
    private func reportStreamOutcome(
        phase: String,
        outcome: String,
        error: Error?,
        fromSeq: Int,
        diagnostics: ChatStreamDiagnostics
    ) {
        let now = Date()
        let sinceConnect = diagnostics.connectedAt.map { now.timeIntervalSince($0) }
        let sinceLastEvent = diagnostics.lastEventAt.map { now.timeIntervalSince($0) }
        let (errorCode, errorDomain) = Self.describeStreamError(error)

        var extra: [String: String] = [
            "phase": phase,
            "outcome": outcome,
            "url_error_code": errorCode,
            "error_domain": errorDomain,
            "last_event_type": diagnostics.lastEventType?.rawValue ?? "none",
            "saw_tool_call": diagnostics.sawToolCall ? "true" : "false",
            "event_count": String(diagnostics.eventCount),
            "from_seq": String(fromSeq),
        ]
        if let last = diagnostics.lastSeq {
            extra["last_seq"] = String(last)
        }
        if let sinceConnect {
            extra["seconds_since_connect"] = String(format: "%.1f", sinceConnect)
        }
        if let sinceLastEvent {
            extra["seconds_since_last_event"] = String(format: "%.1f", sinceLastEvent)
        }

        streamLogger.error(
            """
            chat stream ended phase=\(phase, privacy: .public) \
            outcome=\(outcome, privacy: .public) error=\(errorCode, privacy: .public) \
            sawToolCall=\(diagnostics.sawToolCall, privacy: .public) \
            events=\(diagnostics.eventCount, privacy: .public) \
            sinceLastEvent=\(sinceLastEvent.map { String(format: "%.1f", $0) } ?? "n/a", privacy: .public)
            """
        )

        errorReporter.report(
            message: "Chat stream ended (\(outcome)) error=\(errorCode) sawToolCall=\(diagnostics.sawToolCall)",
            component: "Chat.streamDrop",
            errorType: .component,
            extraData: extra
        )
    }

    /// Breadcrumb a live-follow stream drop (the disconnected-indicator cause).
    private func reportLiveStreamDrop(conversationID: String, error: Error?) {
        // A clean EOF (the follow stream finished without throwing — an idle proxy
        // or server-side shutdown closing the socket) carries no error; record it
        // distinctly so it is not confused with a connect that never produced one.
        let (errorCode, errorDomain) = error == nil
            ? ("cleanEOF", "none")
            : Self.describeStreamError(error)
        streamLogger.error(
            """
            live follow stream dropped error=\(errorCode, privacy: .public) \
            isStreaming=\(self.isStreaming, privacy: .public)
            """
        )
        errorReporter.report(
            message: "Live follow stream dropped error=\(errorCode)",
            component: "Chat.liveStreamDrop",
            errorType: .component,
            extraData: [
                "phase": "live-follow",
                "url_error_code": errorCode,
                "error_domain": errorDomain,
                "is_streaming": isStreaming ? "true" : "false",
            ]
        )
    }

    private static func outcomeLabel(_ outcome: TurnSubscriptionOutcome) -> String {
        switch outcome {
        case .completed: "completed"
        case .dropped: "dropped"
        case .interrupted: "interrupted"
        case .reloadHistory: "reloadHistory"
        case .failed: "failed"
        }
    }

    /// A compact (code, domain) description of a stream error for telemetry. The
    /// URLError code is mapped to its symbolic name so the breadcrumb is readable
    /// at a glance (timedOut vs networkConnectionLost vs cancelled).
    private static func describeStreamError(_ error: Error?) -> (code: String, domain: String) {
        guard let error else {
            return ("none", "none")
        }
        if error is CancellationError {
            return ("swiftCancellation", "Swift.CancellationError")
        }
        if let urlError = error as? URLError {
            return (urlErrorName(urlError.code), "URLError")
        }
        if let apiError = error as? ChatAPIError {
            if case .server(let statusCode, _) = apiError {
                return ("http\(statusCode)", "ChatAPIError")
            }
            return ("validation", "ChatAPIError")
        }
        let nsError = error as NSError
        return (String(nsError.code), nsError.domain)
    }

    private static func urlErrorName(_ code: URLError.Code) -> String {
        switch code {
        case .timedOut: "timedOut"
        case .networkConnectionLost: "networkConnectionLost"
        case .cancelled: "cancelled"
        case .notConnectedToInternet: "notConnectedToInternet"
        case .cannotConnectToHost: "cannotConnectToHost"
        case .cannotFindHost: "cannotFindHost"
        case .dnsLookupFailed: "dnsLookupFailed"
        case .secureConnectionFailed: "secureConnectionFailed"
        case .dataNotAllowed: "dataNotAllowed"
        case .badServerResponse: "badServerResponse"
        case .resourceUnavailable: "resourceUnavailable"
        default: "urlCode\(code.rawValue)"
        }
    }

    private func apply(streamEvent event: ChatStreamEvent, assistantMessageID: String) {
        if event.type == .userInput {
            appendUserInput(event)
            return
        }
        guard let index = messages.firstIndex(where: { $0.id == assistantMessageID }) else {
            return
        }

        switch event.type {
        case .text:
            guard let delta = event.text, !delta.isEmpty else {
                messages[index].isLoading = false
                break
            }
            // Buffer the delta and flush on a timer rather than mutating `messages`
            // per token (see `pendingTextByMessageID`). `isLoading` flips off when
            // the buffered text is flushed.
            pendingTextByMessageID[assistantMessageID, default: ""] += delta
            scheduleTextFlush()
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
            if event.status == "cancelled" && messages[index].text.isEmpty {
                messages[index].text = "Response stopped."
            }
        case .turnStarted, .connected, .message, .heartbeat, .streamDropped, .userInput:
            break
        }
    }

    private func appendUserInput(_ event: ChatStreamEvent) {
        guard let text = event.text?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty
        else {
            return
        }
        let id = "local_user_input_\(event.turnID ?? "turn")_\(event.seq.map(String.init) ?? UUID().uuidString)"
        guard !messages.contains(where: { $0.id == id }) else {
            return
        }
        if shouldRepresentWithPersistedUserInputEcho(turnID: event.turnID, text: text) {
            removeInFlightSteer(text)
            removeAwaitingEchoSteer(text)
            clearComposerIfMatching(text)
            steerErrorMessage = nil
            return
        }
        let message = ChatMessage(
            id: id,
            role: .user,
            text: text,
            createdAt: Date(),
            toolCalls: [],
            attachments: [],
            isLoading: false,
            status: .complete,
            processingProfileID: selectedProfileID,
            errorTraceback: nil,
            turnID: event.turnID
        )
        if let conversationID {
            localUserInputConversationIDByMessageID[id] = conversationID
        }
        if let index = messages.firstIndex(
            where: { $0.role == .assistant && $0.status == .running && $0.turnID == event.turnID }
        ) {
            messages.insert(message, at: index)
        } else {
            messages.append(message)
        }
        removeInFlightSteer(text)
        removeAwaitingEchoSteer(text)
        clearComposerIfMatching(text)
        steerErrorMessage = nil
    }

    private func shouldRepresentWithPersistedUserInputEcho(turnID: String?, text: String) -> Bool {
        guard let key = userInputEchoKey(turnID: turnID, text: text) else {
            return false
        }
        let persistedCount = messages.filter { message in
            message.id.hasPrefix("msg_")
                && message.role == .user
                && userInputEchoKey(for: message) == key
        }.count
        let localEchoCount = messages.filter { message in
            isLocalUserInputEcho(message) && userInputEchoKey(for: message) == key
        }.count
        let representedCount = localEchoCount + (representedPersistedUserInputEchoCounts[key] ?? 0)
        guard persistedCount > representedCount else {
            return false
        }
        representedPersistedUserInputEchoCounts[key, default: 0] += 1
        return true
    }

    /// Schedule a deferred flush of buffered stream text. A single pending timer
    /// coalesces all deltas that arrive within `streamTextFlushInterval`.
    private func scheduleTextFlush() {
        guard textFlushTask == nil else {
            return
        }
        let interval = streamTextFlushInterval
        textFlushTask = Task { [weak self] in
            try? await Task.sleep(for: interval)
            guard let self, !Task.isCancelled else {
                return
            }
            self.textFlushTask = nil
            self.flushPendingText()
        }
    }

    /// Apply all buffered stream text to its target messages. A buffer whose
    /// message no longer exists (placeholder dropped on recovery/supersession) is
    /// discarded.
    private func flushPendingText() {
        guard !pendingTextByMessageID.isEmpty else {
            return
        }
        for (id, suffix) in pendingTextByMessageID {
            if let index = messages.firstIndex(where: { $0.id == id }) {
                messages[index].text += suffix
                messages[index].isLoading = false
            }
        }
        pendingTextByMessageID.removeAll()
    }

    /// Flush buffered text immediately, cancelling any pending timer. Called
    /// before a turn finalizes so finalizers see the fully assembled text.
    private func flushPendingTextNow() {
        textFlushTask?.cancel()
        textFlushTask = nil
        flushPendingText()
    }

    private func completeStream(assistantMessageID: String) {
        flushPendingTextNow()
        if let index = messages.firstIndex(where: { $0.id == assistantMessageID }) {
            messages[index].isLoading = false
            messages[index].status = messages[index].status == .failed ? .failed : .complete
            if messages[index].text.isEmpty && messages[index].toolCalls.isEmpty {
                messages[index].text = "Done."
            }
        }
    }

    private func markStreamStopped(assistantMessageID: String) {
        flushPendingTextNow()
        if let index = messages.firstIndex(where: { $0.id == assistantMessageID }) {
            messages[index].isLoading = false
            messages[index].status = .complete
            if messages[index].text.isEmpty {
                messages[index].text = "Response stopped."
            }
        }
    }

    private func appendStreamError(_ message: String, assistantMessageID: String) {
        flushPendingTextNow()
        if let index = messages.firstIndex(where: { $0.id == assistantMessageID }) {
            messages[index].isLoading = false
            messages[index].status = .failed
            if messages[index].text.isEmpty {
                messages[index].text = "Sorry, I encountered an error processing your message."
            }
            messages[index].text += "\n\n\(message)"
        }
        errorMessage = message
        errorReporter.report(message: message, component: "Chat.stream")
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
        guard let conversationID else {
            return
        }
        let defaults = UserDefaults.standard
        defaults.set(conversationID, forKey: Keys.lastConversationID)
        defaults.set(Date().timeIntervalSinceReferenceDate, forKey: Keys.lastConversationActiveAt)
    }

    /// The stored last conversation, but only if it was active within
    /// `conversationRestoreWindow`. Returns nil when nothing is stored, no
    /// activity time was recorded (e.g. an upgrade from before this was
    /// tracked), or the window has elapsed.
    private static func recentlyActiveConversationID() -> String? {
        let defaults = UserDefaults.standard
        guard let id = defaults.string(forKey: Keys.lastConversationID),
              let activeAt = defaults.object(forKey: Keys.lastConversationActiveAt) as? Double
        else {
            return nil
        }
        let elapsed = Date().timeIntervalSinceReferenceDate - activeAt
        guard (0...conversationRestoreWindow).contains(elapsed) else {
            return nil
        }
        return id
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

        let rendered = backendMessages.compactMap { backend -> ChatMessage? in
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
                errorTraceback: backend.errorTraceback,
                turnID: backend.turnID
            )
        }
        return rendered
    }

    /// The held thread with each agentic turn's tool calls collapsed into a
    /// single bubble, for display. `messages` stays one-to-one with persisted
    /// backend messages so the incremental delta-merge cursor and message
    /// identity are unaffected; grouping is derived here over the *whole* thread,
    /// so a turn whose steps arrived across separate merges still collapses into
    /// one group.
    var groupedMessages: [ChatMessage] {
        Self.groupToolCallTurns(messages)
    }

    /// The recent window of `groupedMessages` actually rendered by the chat view.
    /// Always a suffix, so newly streamed messages stay visible; only older
    /// history is withheld until `showEarlierMessages()` widens the window.
    var visibleGroupedMessages: [ChatMessage] {
        let grouped = groupedMessages
        guard grouped.count > displayedMessageLimit else {
            return grouped
        }
        return Array(grouped.suffix(displayedMessageLimit))
    }

    /// Whether older bubbles are currently hidden behind the display window.
    var hasEarlierMessages: Bool {
        groupedMessages.count > displayedMessageLimit
    }

    /// Reveal another page of older bubbles.
    func showEarlierMessages() {
        displayedMessageLimit += Self.displayedMessagePageSize
    }

    /// Collapse the tool calls an agentic turn made across several backend
    /// assistant messages into a single bubble, so the thread shows one tool
    /// group per turn rather than one collapsible box per backend message. This
    /// mirrors the web client, where all of a turn's tool calls share one group.
    ///
    /// Consecutive assistant messages that each carry tool calls are merged.
    /// Assistant messages without tool calls (such as the final text answer),
    /// user/system messages, and errors are never merged and act as boundaries,
    /// matching the web behaviour where intervening text breaks a tool group.
    nonisolated static func groupToolCallTurns(_ messages: [ChatMessage]) -> [ChatMessage] {
        var grouped: [ChatMessage] = []
        for message in messages {
            if isToolGroupMember(message),
               let last = grouped.last,
               isToolGroupMember(last) {
                grouped[grouped.count - 1] = merging(last, with: message)
            } else {
                grouped.append(message)
            }
        }
        return grouped
    }

    private nonisolated static func isToolGroupMember(_ message: ChatMessage) -> Bool {
        message.role == .assistant && !message.toolCalls.isEmpty && message.errorTraceback == nil
    }

    private nonisolated static func merging(_ base: ChatMessage, with next: ChatMessage) -> ChatMessage {
        var result = base
        result.toolCalls.append(contentsOf: next.toolCalls)
        result.attachments.append(contentsOf: next.attachments)
        result.text = [base.text, next.text].filter { !$0.isEmpty }.joined(separator: "\n\n")
        // The grouped bubble represents the whole turn, so date it by its newest
        // folded-in step.
        result.createdAt = max(base.createdAt, next.createdAt)
        result.isLoading = base.isLoading || next.isLoading
        if next.status == .failed {
            result.status = .failed
        }
        return result
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

/// Per-subscription stream telemetry, accumulated while consuming a turn's SSE
/// events so a drop breadcrumb can report what had been seen before the stream
/// ended — notably whether a tool call had already arrived (the reported failure
/// mode) and how long the stream had been quiet before it died.
private struct ChatStreamDiagnostics {
    var connectedAt: Date?
    var lastEventAt: Date?
    var eventCount = 0
    var sawToolCall = false
    var lastEventType: ChatStreamEventType?
    var lastSeq: Int?
}
