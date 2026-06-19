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
    @ObservationIgnored private var pendingConfirmationsTask: Task<Void, Never>?
    @ObservationIgnored private var lastProcessedInitialPrompt: String?
    // Highest stream seq applied for the active conversation, threaded into the
    // follow subscribe's `ack_seq` and the `/ack` POST after a turn_ended so the
    // server suppresses the disconnect push for events this client has seen.
    // Reset whenever the active conversation changes.
    @ObservationIgnored private var highestAppliedSeq: Int?

    private enum Keys {
        static let lastConversationID = "lastConversationId"
        static let lastConversationActiveAt = "lastConversationActiveAt"
        static let selectedProfileID = "selectedProfileId"
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
        selectedProfileID = UserDefaults.standard.string(forKey: Keys.selectedProfileID) ?? "default_assistant"
        if let initialPrompt, !initialPrompt.isEmpty {
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
            // No recent conversation: open a fresh thread but land on the list
            // (selection nil). Leave the stored last conversation untouched so a
            // genuinely-recent reopen after this one still resolves correctly.
            self.conversationID = Self.generateConversationID()
            conversationSelection = nil
        }
    }

    deinit {
        streamTask?.cancel()
        liveEventsTask?.cancel()
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
    private func refreshRecentConversations() async {
        do {
            let recent = try await apiClient.listRecentConversations()
            let recentIDs = Set(recent.map(\.conversationID))
            let untouched = conversations.filter { !recentIDs.contains($0.conversationID) }
            conversations = recent + untouched
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
            errorReporter.report(error, component: "Chat.recentConversations")
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
        displayedMessageLimit = Self.initialDisplayedMessageCount
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
        displayedMessageLimit = Self.initialDisplayedMessageCount
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
            errorReporter.report(error, component: "Chat.messages")
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
            if !profiles.contains(where: { $0.id == selectedProfileID }) {
                selectedProfileID = response.defaultProfileID
                UserDefaults.standard.set(selectedProfileID, forKey: Keys.selectedProfileID)
            }
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
            await runSendTurn(
                turnID: turnID,
                prompt: prompt,
                conversationID: id,
                attachments: uploadedAttachments,
                assistantMessageID: assistantMessageID,
                streamToken: streamToken
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
        case completed
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
    }

    private func runSendTurn(
        turnID: String,
        prompt: String,
        conversationID id: String,
        attachments: [ChatAttachment],
        assistantMessageID: String,
        streamToken: UUID
    ) async {
        var lastSeq: Int?
        do {
            let start = try await apiClient.startTurn(
                turnID: turnID,
                prompt: prompt,
                conversationID: id,
                profileID: selectedProfileID,
                attachments: attachments
            )
            guard !Task.isCancelled, currentStreamToken == streamToken else {
                return
            }
            if start.alreadyComplete {
                // The retried turn finished durably but is not replayable from the
                // hub. Don't subscribe; reload persisted history to surface it.
                await recoverByReloadingHistory(
                    conversationID: id,
                    assistantMessageID: assistantMessageID
                )
                finishStreaming(streamToken)
                return
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
            case .completed:
                // Acknowledge the highest received seq so the server suppresses the
                // disconnect push for a reply we actually saw. Fire-and-forget so
                // UI completion isn't blocked on the ack.
                if let lastSeq {
                    let ackClient = apiClient
                    Task { try? await ackClient.acknowledge(conversationID: id, ackSeq: lastSeq) }
                }
                completeStream(assistantMessageID: assistantMessageID)
                await refreshRecentConversations()
                await mergeNewMessages(conversationID: id)
            case .reloadHistory, .dropped, .interrupted:
                // The connection dropped before turn_ended, but the turn keeps
                // running durably. Reload persisted history silently — the reply
                // surfaces here (or via the live follow loop / push once it
                // finishes). Don't write to `errorMessage`: that drives a modal
                // "Chat Error" alert, which would be spurious for a reply that
                // actually succeeded. The disconnected indicator is the
                // appropriate non-modal signal.
                await recoverByReloadingHistory(
                    conversationID: id,
                    assistantMessageID: assistantMessageID
                )
            case .failed(let message):
                appendStreamError(message, assistantMessageID: assistantMessageID)
            }
        } catch is CancellationError {
            markStreamStopped(assistantMessageID: assistantMessageID)
        } catch {
            // Reaching here means starting the turn itself failed — the prompt was
            // never accepted, so there is no durable turn to recover; surface it.
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
            if outcome != .completed {
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
                // A failed turn carries its error on the terminal event; surface
                // it so the bubble shows the failure rather than an empty reply.
                if let message = event.errorMessage, !message.isEmpty {
                    appendStreamError(message, assistantMessageID: assistantMessageID)
                }
                return .completed
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
        }
    }

    func cancelStream() {
        guard streamTask != nil || isStreaming else {
            return
        }
        streamTask?.cancel()
        streamTask = nil
        isStreaming = false
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
        liveUpdatesConnected = true
        startLiveEvents()
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
                // Re-read the ack cursor each attempt so a reconnect resumes from
                // the highest seq this client has actually applied.
                let ackSeq = self?.highestAppliedSeq ?? -1
                var deliberateStop = false
                var streamError: Error?
                do {
                    let stream = try await client.connectEvents(
                        conversationID: conversationID,
                        ackSeq: ackSeq,
                        // Follow streams only reload persisted history on these;
                        // the token firehose is ignored, so don't ask for it.
                        // Lifecycle frames (turn_ended, heartbeat, stream_dropped)
                        // are always delivered regardless of this filter.
                        eventTypes: ["message", "turn_ended"]
                    )
                    // Call through the weak `self?` at each suspension point
                    // instead of binding a strong `self`: a binding would keep
                    // the view model (and its open SSE connection) alive across
                    // the indefinite follow loop, so logout/deinit could never
                    // tear it down.
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
                // Catch up persisted history over plain HTTP on every involuntary
                // disconnect — not only after a successful (re)connect. When the
                // front door makes SSE unusable (buffered/severed framing), the
                // connect above keeps throwing and `handleLiveReconnect` never
                // runs, so without this a turn that finished while the stream was
                // down would strand until a manual refresh. A short GET succeeds
                // precisely when the long-lived SSE does not.
                await self?.catchUpPersistedHistory(conversationID: conversationID)
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
            // A turn finished (possibly started on another device). Only surface
            // and acknowledge it while this device is NOT actively streaming its
            // own turn: during a send the send path owns the ack cursor, and
            // advancing it / acking here for a turn we don't surface (the merge
            // is skipped while streaming) would let the hub treat that turn as
            // delivered (ended_seq <= ack_seq) and suppress its disconnect push.
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
        case .turnStarted, .connected, .message, .heartbeat, .streamDropped:
            break
        }
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
                errorTraceback: backend.errorTraceback
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
