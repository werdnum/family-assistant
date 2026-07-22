import Foundation

typealias ChatSyncBreadcrumb = @MainActor (_ component: String, _ extraData: [String: String]) -> Void

/// The application-side operations the coordinator's owned stream loops invoke.
///
/// The coordinator owns the follow/activity `Task`s and their reconnect loops
/// (backoff, catch-up gating, 410 handling); all per-event *application* — history
/// merges, ack cursor management, live-token rendering — stays behind this
/// delegate in the view model. Every loop callback carries the coordinator
/// `generation` that owned the attempt so the delegate can drop events from a
/// superseded generation (`SyncCoordinator.isCurrent`).
@MainActor
protocol SyncStreamDelegate: AnyObject {
    /// Open the per-conversation follow stream, resuming from the delegate's
    /// current cursor. Returns the event stream to iterate.
    func openFollowStream(
        conversationID: String,
        generation: Int
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error>

    /// A follow connect succeeded: reconcile persisted history (catch-up).
    func followStreamDidConnect(conversationID: String, generation: Int) async

    /// Apply one follow-stream event. Returns false when the loop should stop
    /// (a deliberate stop). Stale-generation events are dropped by the delegate.
    func handleFollowEvent(
        _ event: ChatStreamEvent,
        conversationID: String,
        generation: Int
    ) async -> Bool

    /// A 410 rotated the resume cursor out of the hub buffer; clear it so the
    /// next attempt tails the head instead of re-requesting the gone seq.
    func followBufferRotated(generation: Int)

    /// Breadcrumb an involuntary follow-stream drop (throw or clean EOF).
    func reportFollowStreamDrop(conversationID: String, error: Error?, generation: Int)

    /// Whether a drop should surface as a user-visible disconnect. Mirrors the
    /// old `markLiveUpdatesDisconnectedIfActive` suppression: a drop while a send
    /// is actively streaming must not degrade presentation.
    func shouldSurfaceFollowDrop() -> Bool

    /// Return the ID of the conversation currently displayed in the UI, or nil if
    /// none is selected. Used by resync to derive the authoritative follow target
    /// so a stale cached ID doesn't restart the old stream on conversation switch
    /// or foreground resync after launch.
    func currentConversationID() -> String?

    /// Catch up persisted history over plain HTTP after a *failed* connect (the
    /// `!connected` branch), so a turn that finished while SSE is unusable still
    /// surfaces within one backoff interval.
    func catchUpFollowHistory(conversationID: String, generation: Int) async

    /// Open the account-global activity stream.
    func openActivityStream(
        generation: Int
    ) async throws -> AsyncThrowingStream<ChatConversationActivity, Error>

    /// Refresh the recent-conversation list (on activity connect and each ping).
    func activityStreamDidSignal(generation: Int) async

    /// Tear down the in-flight send's transport task WITHOUT running the
    /// user-facing `cancelStream()` semantics ("Response stopped", control
    /// discard, queued stop-cancel POST). The `ActiveTurnSession` (state, cursors,
    /// retry payload) survives so foreground resync can reattach to the turn. The
    /// dedicated background-suspend path in §4.3.
    func suspendActiveSend()

    /// Complete the foreground resync's auth gate: a single-flight token refresh
    /// when the stored token is near expiry (§4.4 step 2). Throws on a rejected
    /// refresh so the resync aborts cleanly (the auth layer latches
    /// `authRequired`; no error modal is raised).
    func gateAuthIfNeeded(generation: Int) async throws

    /// Run the SAME coalesced foreground resync the orchestrator drives, requested
    /// from inside the coordinator on an `NWPathMonitor` unsatisfied→satisfied
    /// recovery (§4.4 "the same resync runs on NWPathMonitor recovery"). Coalesces
    /// with any in-flight resync, so a burst of recovery hints does the snapshot
    /// work once. Distinct from `runResync()` (the bare loop restart) — recovery
    /// gets the full snapshot pass, not just a reconnect.
    func runCoalescedResync(reason: SyncCoordinator.RestartReason)
}

@MainActor
@Observable
final class SyncCoordinator {
    enum Lifecycle: String {
        case foreground
        case background
    }

    enum Reachability: String {
        case satisfied
        case unsatisfied
        case unknown
    }

    enum AuthState: String {
        case ok
        case refreshing
        case authRequired
    }

    enum ChannelHealth: String {
        case connected
        case reconnecting
        case down
    }

    enum ReconciliationPhase: String {
        case idle
        case syncing
    }

    enum Presentation: String {
        case live
        case syncing
        case degraded
        case offline
        case authRequired
        case suspended
    }

    enum RestartReason: String {
        case conversationSwitch
        case foreground
        case background
        case reachabilityRecovery
        case authOK
        case authRequired
        case manualReconnect
        case resyncHandoff
        case initial
        case newConversation
    }

    enum SyncEvent {
        case foregrounded
        case backgrounded
        case reachabilityChanged(Reachability)
        case authRefreshing
        case authOK
        case authRequired
        case followConnected(generation: Int)
        case followDropped(generation: Int, cleanEOF: Bool)
        case activityConnected(generation: Int)
        case activityDropped(generation: Int, cleanEOF: Bool)
        case syncStarted
        case syncFinished
        case pushHintReceived(conversationID: String?)
    }

    enum SyncEffect: Equatable {
        case startFollowStream(generation: Int)
        case startActivityStream(generation: Int)
        case cancelStreams
        case suspendSend
        case runResync
        case targetedRefresh(conversationID: String?)
    }

    private(set) var lifecycle: Lifecycle = .foreground
    private(set) var reachability: Reachability = .unknown
    private(set) var authState: AuthState = .ok
    private(set) var followHealth: ChannelHealth = .down
    private(set) var activityHealth: ChannelHealth = .down
    /// Set while `authRequired` is latched (streams suppressed) and consumed by
    /// the next `.authOK` to restart the suppressed streams. A separate latch is
    /// required because a `.refreshing` signal always fires between `.authRequired`
    /// and `.ok`, so `authState` is no longer `.authRequired` at `.authOK` time.
    private var pendingReauthRestart = false
    private(set) var phase: ReconciliationPhase = .idle
    private(set) var cameFromBackground = false

    // Per-channel generations. Each stream task captures its channel's generation
    // at start and stamps every delegate callback and health event with it; the
    // reducer/delegate reject any callback whose generation no longer matches. The
    // two channels are counted SEPARATELY so replacing one stream (a conversation
    // switch restarts only follow) cannot invalidate the sibling's still-valid
    // in-flight callbacks — a single shared counter would reject a concurrent
    // activity connect the moment a follow replacement bumped it.
    private(set) var followGeneration = 0
    private(set) var activityGeneration = 0

    private let pathMonitor: PathMonitoring
    private let followReconnectInitialDelaySeconds: Double
    private let followReconnectMaxDelaySeconds: Double
    private let authManager: AuthManager
    private let reconnectDelay: @MainActor (Double) async -> Void
    private let streamTerminationTimeoutSeconds: Double
    private let breadcrumb: ChatSyncBreadcrumb?
    private var lastReportedPresentation: Presentation = .degraded
    private var pendingResyncRestartReason: RestartReason?

    weak var delegate: SyncStreamDelegate?

    // `nonisolated(unsafe)` so the synchronous `deinit` (a nonisolated context)
    // can cancel the owned stream tasks to tear down their open SSE connections.
    // Task is Sendable and all mutation otherwise happens on the main actor.
    private nonisolated(unsafe) var followTask: Task<Void, Never>?
    private nonisolated(unsafe) var activityTask: Task<Void, Never>?

    init(
        authManager: AuthManager,
        pathMonitor: PathMonitoring,
        followReconnectInitialDelaySeconds: Double = 2,
        followReconnectMaxDelaySeconds: Double = 30,
        reconnectDelay: @escaping @MainActor (Double) async -> Void = { seconds in
            try? await Task.sleep(for: .seconds(seconds))
        },
        streamTerminationTimeoutSeconds: Double = 5,
        breadcrumb: ChatSyncBreadcrumb? = nil
    ) {
        self.authManager = authManager
        self.pathMonitor = pathMonitor
        self.followReconnectInitialDelaySeconds = followReconnectInitialDelaySeconds
        self.followReconnectMaxDelaySeconds = followReconnectMaxDelaySeconds
        self.reconnectDelay = reconnectDelay
        self.streamTerminationTimeoutSeconds = streamTerminationTimeoutSeconds
        self.breadcrumb = breadcrumb
    }

    /// Deferred so constructing a coordinator during a SwiftUI rebuild has no
    /// side effects and does not start a path monitor for a discarded model.
    func start() {
        pathMonitor.onChange = { [weak self] satisfied in
            self?.apply(.reachabilityChanged(satisfied ? .satisfied : .unsatisfied))
        }
        pathMonitor.start()
        if pathMonitor.isSatisfied {
            reachability = .satisfied
        }
    }

    deinit {
        followTask?.cancel()
        activityTask?.cancel()
    }

    /// Cancel the owned stream tasks from a nonisolated context. A running stream
    /// task retains `self` for the life of its open SSE connection (the loop holds
    /// a strong `self` across the indefinite `for await`), so the coordinator's own
    /// `deinit` cannot run until the tasks end. The owning view model calls this
    /// from its `deinit` to break that cycle and tear the sockets down when the UI
    /// goes away, rather than orphaning them until the process exits.
    nonisolated func cancelOwnedStreams() {
        followTask?.cancel()
        activityTask?.cancel()
    }

    var presentation: Presentation {
        if lifecycle == .background {
            return .suspended
        }
        if authState == .authRequired {
            return .authRequired
        }
        if reachability == .unsatisfied {
            return .offline
        }
        if phase == .syncing {
            return .syncing
        }
        // When no conversation is selected, only activity health matters.
        // Otherwise both follow and activity must be connected for live.
        // If delegate is not set, conservatively require both (treat as conversation selected).
        let isNoConversation = delegate != nil && delegate?.currentConversationID() == nil
        let isLive = isNoConversation
            ? activityHealth == .connected
            : followHealth == .connected && activityHealth == .connected
        if isLive {
            return .live
        }
        return .degraded
    }

    func isCurrentFollow(_ generation: Int) -> Bool {
        generation == followGeneration
    }

    func isCurrentActivity(_ generation: Int) -> Bool {
        generation == activityGeneration
    }

    @discardableResult
    func bumpFollowGeneration(reason: RestartReason? = nil) -> Int {
        followGeneration += 1
        if let reason {
            reportStreamRestart(channel: "follow", reason: reason, generation: followGeneration)
        }
        return followGeneration
    }

    @discardableResult
    func bumpActivityGeneration(reason: RestartReason? = nil) -> Int {
        activityGeneration += 1
        if let reason {
            reportStreamRestart(channel: "activity", reason: reason, generation: activityGeneration)
        }
        return activityGeneration
    }

    func prepareResync(reason: RestartReason) {
        pendingResyncRestartReason = reason
    }

    /// Maps a raw scene-phase observation onto the coordinator's lifecycle events. The
    /// `didBackground`/`isActive` split lets callers distinguish a real background (which must latch
    /// `cameFromBackground`) from an `.inactive` blip that never backgrounded. Returns the effects
    /// the transition produced (a latched foreground emits `.runResync`).
    @discardableResult
    func scenePhaseChanged(didBackground: Bool, isActive: Bool) -> [SyncEffect] {
        if didBackground {
            return apply(.backgrounded)
        } else if isActive {
            return apply(.foregrounded)
        }
        return []
    }

    // MARK: - Stream ownership

    /// (Re)start the per-conversation follow stream for `conversationID`. Owns the
    /// reconnect loop (capped exponential backoff, catch-up on connect and on
    /// failed connect, 410 buffer-rotation handling). All event application is
    /// delegated to the view model. Cancelling the previous task before starting a
    /// new one is the single-owner cancellation the design requires.
    func startFollowStream(
        conversationID: String,
        reason: RestartReason = .initial
    ) {
        // Replacing a live task: cancelling it does not stop its in-flight delegate
        // callbacks synchronously, so they would still pass the generation check and
        // merge/ack stale events into the new stream's state. Bump the follow
        // generation so the replacement runs under a fresh one and the cancelled
        // task's late callbacks are rejected.
        let replacesExistingTask = followTask != nil
        if replacesExistingTask {
            bumpFollowGeneration(reason: reason)
            // The old task's socket is being torn down; until the replacement task
            // reports a real `followConnected`, the channel is not live. Leaving it
            // `.connected` here would let presentation claim `.live` with no
            // connected follow stream. Demote to `.reconnecting`; only a real
            // connect promotes it back.
            if followHealth == .connected {
                followHealth = .reconnecting
            }
        }
        followTask?.cancel()
        let generation = followGeneration
        if !replacesExistingTask {
            reportStreamRestart(
                channel: "follow",
                reason: reason,
                generation: generation,
                conversationID: conversationID
            )
        }
        reportPresentationIfChanged()
        let initialDelay = followReconnectInitialDelaySeconds
        let maxDelay = followReconnectMaxDelaySeconds
        followTask = Task { [weak self] in
            var delay = initialDelay
            var authRefreshAlreadyAttempted = false
            while !Task.isCancelled {
                guard let self else { return }
                if self.authManager.authRequired {
                    break
                }
                // Capture the loop's auth epoch at the start; terminal 401 latch uses this
                // to prevent a stale rejection from a superseded epoch deleting new creds.
                let loopEpoch = self.authManager.authEpoch
                var deliberateStop = false
                var connected = false
                var connectedAt: Date?
                var streamError: Error?
                do {
                    guard let stream = try await self.delegate?.openFollowStream(
                        conversationID: conversationID,
                        generation: generation
                    ) else {
                        return
                    }
                    connected = true
                    connectedAt = Date()
                    self.apply(.followConnected(generation: generation))
                    authRefreshAlreadyAttempted = false
                    await self.delegate?.followStreamDidConnect(
                        conversationID: conversationID,
                        generation: generation
                    )
                    delay = initialDelay
                    for try await event in stream {
                        if Task.isCancelled {
                            break
                        }
                        let shouldContinue = await self.delegate?.handleFollowEvent(
                            event,
                            conversationID: conversationID,
                            generation: generation
                        )
                        guard let shouldContinue else { return }
                        if !shouldContinue {
                            deliberateStop = true
                            break
                        }
                    }
                } catch {
                    streamError = error
                    if case ChatAPIError.server(let statusCode, _) = error, statusCode == 410 {
                        self.delegate?.followBufferRotated(generation: generation)
                    }
                    if case ChatAPIError.server(let statusCode, _) = error,
                       statusCode == 401 || statusCode == 403 {
                        if !authRefreshAlreadyAttempted {
                            authRefreshAlreadyAttempted = true
                            do {
                                try await self.authManager.refreshIfNeeded(force: true, ownerEpoch: loopEpoch)
                                self.reportStreamDisconnect(
                                    channel: "follow",
                                    generation: generation,
                                    conversationID: conversationID,
                                    connectedAt: connectedAt,
                                    error: error,
                                    deliberateStop: false
                                )
                                continue
                            } catch AuthError.authRejected, AuthError.noCredentials {
                                self.authManager.markAuthRequiredIfCurrent(capturedEpoch: loopEpoch)
                                deliberateStop = true
                            } catch {
                                streamError = error
                            }
                        } else {
                            self.authManager.markAuthRequiredIfCurrent(capturedEpoch: loopEpoch)
                            deliberateStop = true
                        }
                        if deliberateStop {
                            self.reportStreamDisconnect(
                                channel: "follow",
                                generation: generation,
                                conversationID: conversationID,
                                connectedAt: connectedAt,
                                error: streamError,
                                deliberateStop: true
                            )
                            break
                        }
                    }
                }

                self.reportStreamDisconnect(
                    channel: "follow",
                    generation: generation,
                    conversationID: conversationID,
                    connectedAt: connectedAt,
                    error: streamError,
                    deliberateStop: deliberateStop
                )
                if Task.isCancelled || deliberateStop {
                    break
                }
                self.delegate?.reportFollowStreamDrop(
                    conversationID: conversationID,
                    error: streamError,
                    generation: generation
                )
                if self.delegate?.shouldSurfaceFollowDrop() ?? false {
                    self.apply(.followDropped(generation: generation, cleanEOF: streamError == nil))
                }
                if !connected {
                    await self.delegate?.catchUpFollowHistory(
                        conversationID: conversationID,
                        generation: generation
                    )
                }
                await reconnectDelay(delay)
                delay = min(delay * 2, maxDelay)
            }
        }
    }

    /// (Re)start the account-global activity stream. Owns the same capped-backoff
    /// reconnect loop; list refresh on connect and on every ping is delegated.
    func startActivityStream(reason: RestartReason = .initial) {
        // Same generation hole as `startFollowStream`: a replaced activity task's
        // in-flight callbacks would still pass the check and let a stale signal ack
        // into the new stream. Bump the activity generation so the replacement owns
        // a fresh one.
        let replacesExistingTask = activityTask != nil
        if replacesExistingTask {
            bumpActivityGeneration(reason: reason)
            // Same as the follow channel: a replaced activity task is no longer a
            // live connection, so demote a `.connected` health to `.reconnecting`
            // until the replacement reports a real `activityConnected`.
            if activityHealth == .connected {
                activityHealth = .reconnecting
            }
        }
        activityTask?.cancel()
        let generation = activityGeneration
        if !replacesExistingTask {
            reportStreamRestart(channel: "activity", reason: reason, generation: generation)
        }
        reportPresentationIfChanged()
        let initialDelay = followReconnectInitialDelaySeconds
        let maxDelay = followReconnectMaxDelaySeconds
        activityTask = Task { [weak self] in
            var delay = initialDelay
            var authRefreshAlreadyAttempted = false
            while !Task.isCancelled {
                guard let self else { return }
                if self.authManager.authRequired {
                    break
                }
                // Capture the loop's auth epoch at the start; terminal 401 latch uses this
                // to prevent a stale rejection from a superseded epoch deleting new creds.
                let loopEpoch = self.authManager.authEpoch
                var connectedAt: Date?
                var streamError: Error?
                do {
                    guard let stream = try await self.delegate?.openActivityStream(
                        generation: generation
                    ) else {
                        return
                    }
                    connectedAt = Date()
                    delay = initialDelay
                    self.apply(.activityConnected(generation: generation))
                    authRefreshAlreadyAttempted = false
                    await self.delegate?.activityStreamDidSignal(generation: generation)
                    for try await _ in stream {
                        if Task.isCancelled {
                            break
                        }
                        await self.delegate?.activityStreamDidSignal(generation: generation)
                    }
                } catch {
                    streamError = error
                    if case ChatAPIError.server(let statusCode, _) = error,
                       statusCode == 401 || statusCode == 403 {
                        if !authRefreshAlreadyAttempted {
                            authRefreshAlreadyAttempted = true
                            do {
                                try await self.authManager.refreshIfNeeded(force: true, ownerEpoch: loopEpoch)
                                self.reportStreamDisconnect(
                                    channel: "activity",
                                    generation: generation,
                                    connectedAt: connectedAt,
                                    error: streamError,
                                    deliberateStop: false
                                )
                                continue
                            } catch AuthError.authRejected, AuthError.noCredentials {
                                self.authManager.markAuthRequiredIfCurrent(capturedEpoch: loopEpoch)
                                self.apply(.activityDropped(generation: generation, cleanEOF: false))
                                self.reportStreamDisconnect(
                                    channel: "activity",
                                    generation: generation,
                                    connectedAt: connectedAt,
                                    error: streamError,
                                    deliberateStop: false
                                )
                                break
                            } catch {
                                streamError = error
                                self.apply(.activityDropped(generation: generation, cleanEOF: false))
                            }
                        } else {
                            self.authManager.markAuthRequiredIfCurrent(capturedEpoch: loopEpoch)
                            self.apply(.activityDropped(generation: generation, cleanEOF: false))
                            self.reportStreamDisconnect(
                                channel: "activity",
                                generation: generation,
                                connectedAt: connectedAt,
                                error: streamError,
                                deliberateStop: false
                            )
                            break
                        }
                        if self.authManager.authRequired {
                            break
                        }
                    }
                }
                self.reportStreamDisconnect(
                    channel: "activity",
                    generation: generation,
                    connectedAt: connectedAt,
                    error: streamError,
                    deliberateStop: false
                )
                if Task.isCancelled {
                    break
                }
                self.apply(.activityDropped(generation: generation, cleanEOF: false))
                await reconnectDelay(delay)
                delay = min(delay * 2, maxDelay)
            }
        }
    }

    /// Cancel the follow stream only (e.g. a conversation switch cancels it before
    /// the caller restarts it for the new conversation).
    ///
    /// Bumps the follow generation at cancellation time, not just at the later
    /// restart. A conversation switch cancels here, then `await`s `loadMessages`
    /// before `startLiveEvents` restarts the stream; during that window the
    /// cancelled task's in-flight callbacks would otherwise still pass
    /// `isCurrentFollow` and merge/ack into the NEW conversation's state. Bumping
    /// now fences those late callbacks immediately. Also demote a `.connected`
    /// health so presentation cannot claim `.live` over the cancelled stream.
    func cancelFollowStream(reason: RestartReason = .conversationSwitch) {
        if followTask != nil {
            bumpFollowGeneration(reason: reason)
            if followHealth == .connected {
                followHealth = .reconnecting
            }
        }
        followTask?.cancel()
        reportPresentationIfChanged()
    }

    /// Cancel both owned streams (deinit / logout).
    func cancelStreams(reason: RestartReason = .authRequired) {
        if followTask != nil {
            bumpFollowGeneration(reason: reason)
            if followHealth == .connected {
                followHealth = .reconnecting
            }
        }
        if activityTask != nil {
            bumpActivityGeneration(reason: reason)
            if activityHealth == .connected {
                activityHealth = .reconnecting
            }
        }
        followTask?.cancel()
        activityTask?.cancel()
        reportPresentationIfChanged()
    }

    /// Cancel the follow/activity tasks torn down on background AND await their
    /// completion before the caller (the foreground resync) establishes fresh
    /// streams. §4.3 requires the await, beyond the generation fence: a late event
    /// from the old consumer is already dropped by the fence, but an old task still
    /// iterating its socket could otherwise race the new follow connect and the
    /// two would briefly both consume the same conversation.
    ///
    /// Cancellation propagates through each `AsyncThrowingStream`'s `onTermination`,
    /// so a live iteration throws promptly and the task returns. A task whose socket
    /// read is wedged (a proxy that neither delivers nor closes) can't be waited on
    /// forever, so each await is bounded by `streamTerminationTimeoutSeconds`; the
    /// generation fence still protects against any event it emits after the timeout.
    func awaitStreamTermination() async {
        let follow = followTask
        let activity = activityTask
        followTask = nil
        activityTask = nil
        follow?.cancel()
        activity?.cancel()
        await awaitTaskWithTimeout(follow)
        await awaitTaskWithTimeout(activity)
    }

    private func awaitTaskWithTimeout(_ task: Task<Void, Never>?) async {
        guard let task else {
            return
        }
        let timeout = streamTerminationTimeoutSeconds
        // Race the task's completion against a timeout. `Task.value` is not
        // cancellable, so a wedged task (parked in a non-cancellable await after
        // ignoring cancellation) never resolves; this method's return must NOT
        // depend on it. A waiter resumes the continuation on task completion and a
        // sleep resumes it on timeout — whichever fires first wins and the method
        // returns. The loser is orphaned: on the timeout path the waiter is left
        // awaiting `task.value` (harmless — the generation fence already rejects
        // anything the wedged task might emit); on the completion path the sleep is
        // cancelled.
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let resumed = ResumeOnce()
            let waiter = Task {
                await task.value
                if resumed.tryResume() {
                    continuation.resume()
                }
            }
            Task {
                try? await Task.sleep(for: .seconds(timeout))
                if resumed.tryResume() {
                    waiter.cancel()
                    continuation.resume()
                }
            }
        }
    }

    /// Run the foreground resync: restart the follow stream for the active
    /// conversation and the activity stream, matching the former
    /// `reconnectLiveUpdates()`. The follow stream target is derived from the
    /// current conversation to avoid stale cached IDs from launch or switch.
    func runResync() {
        let reason = pendingResyncRestartReason ?? .resyncHandoff
        pendingResyncRestartReason = nil
        if let conversationID = delegate?.currentConversationID() {
            startFollowStream(conversationID: conversationID, reason: reason)
        }
        startActivityStream(reason: reason)
    }
    @discardableResult
    func apply(_ event: SyncEvent) -> [SyncEffect] {
        defer { reportPresentationIfChanged() }
        switch event {
        case .foregrounded:
            lifecycle = .foreground
            guard cameFromBackground else {
                return []
            }
            cameFromBackground = false
            // `runResync` restarts both streams, and each `startX` bumps its own
            // channel generation as it replaces the (backgrounded) task, so the
            // reattached loops run under fresh generations without a shared bump here.
            return [.runResync]

        case .backgrounded:
            lifecycle = .background
            cameFromBackground = true
            // On a real background: stop scheduling advisory work (bump BOTH channel
            // generations so the per-channel fences reject any late events from the
            // streams we tear down), suspend the in-flight send through the
            // dedicated path (preserving its ActiveTurnSession), and cancel the
            // advisory follow/activity streams (push notifications take over
            // delivery). See design 4.3.
            // Bump silently here (fencing only); the `.cancelStreams` effect below
            // emits the single, correctly-labeled `background` restart breadcrumb.
            bumpFollowGeneration()
            bumpActivityGeneration()
            return [.suspendSend, .cancelStreams]

        case let .reachabilityChanged(reachability):
            let wasUnsatisfied = self.reachability == .unsatisfied
            self.reachability = reachability
            breadcrumb?(
                "Chat.reachability",
                [
                    "status": reachability.rawValue,
                    "interface_type": pathMonitor.interfaceType,
                ]
            )
            if reachability == .unsatisfied {
                // The path just went down. The SSE tasks have not yet observed their
                // sockets die, so both healths still read `.connected`; leaving them
                // there would make the recovery wake (which only restarts
                // non-connected channels) skip both loops and flip presentation back
                // to `.live` over dead sockets. Offline presentation already wins
                // while unsatisfied, so marking both healths down here is invisible
                // now and makes the recovery wake actually restart both loops. Health
                // becomes connected again only via a real follow/activity connect.
                followHealth = .down
                activityHealth = .down
            }
            // Satisfied is only a retry hint (never marks channels healthy), but a
            // loop sleeping at capped backoff would otherwise stay down for up to
            // one max-delay interval, re-showing the stuck indicator. On the
            // unsatisfied→satisfied transition (foregrounded), run the SAME coalesced
            // resync foreground uses (§4.4): bump BOTH channel generations so late
            // events from the down streams are fenced, then request the
            // orchestrator's full snapshot pass. The resync re-establishes and
            // reconnects both loops (subsuming the bare `wakeReconnectLoops` retry)
            // AND refetches the authoritative list, so a list mutated while offline
            // converges. Health still comes only from actual connects.
            if reachability == .satisfied, wasUnsatisfied, lifecycle == .foreground {
                bumpFollowGeneration(reason: .reachabilityRecovery)
                bumpActivityGeneration(reason: .reachabilityRecovery)
                delegate?.runCoalescedResync(reason: .reachabilityRecovery)
            }
            return []

        case .authRefreshing:
            authState = .refreshing
            return []

        case .authOK:
            authState = .ok
            // Re-auth just succeeded. While `authRequired` was latched the streams
            // could not connect (their requests 401'd), so any loop is now asleep
            // at capped backoff and would otherwise stay down for up to one
            // max-delay interval, keeping the stuck indicator up after sign-in.
            // Route like the reachability recovery (§4.4): bump both channel
            // generations to fence the down streams' late events, then request the
            // orchestrator's coalesced resync (re-establishes + reconnects both
            // loops and refetches the list). Health still comes only from real
            // connects. Gate on the `pendingReauthRestart` latch (set when
            // `.authRequired` fired) rather than `authState`, because the
            // intervening `.refreshing` signal has already moved `authState` off
            // `.authRequired`. A routine near-expiry refresh (which never latched
            // `authRequired`) leaves the flag false and does not churn healthy
            // streams. Also gate on a satisfied path so a re-auth over a down
            // network doesn't kick a futile resync (the reachability recovery runs
            // it once the path returns). When backgrounded, leave the flag set so
            // the foreground resync owns the restart.
            if pendingReauthRestart, lifecycle == .foreground, reachability != .unsatisfied {
                pendingReauthRestart = false
                bumpFollowGeneration(reason: .authOK)
                bumpActivityGeneration(reason: .authOK)
                delegate?.runCoalescedResync(reason: .authOK)
            }
            return []

        case .authRequired:
            authState = .authRequired
            // The backend authorizes an SSE stream once, at connect. A follow or
            // activity socket opened under the now-rejected session keeps applying
            // events even after `authRequired` latches — and because its health
            // still reads `.connected`, the later `.authOK` wake (which only
            // restarts non-connected channels) would skip the restart, so events
            // would keep flowing under stale credentials. Cancel both streams and
            // mark their healths down so the `.authOK` wake actually reconnects
            // under fresh credentials. Bumping the generations (via cancelStreams)
            // also fences any in-flight callbacks from the old-session tasks.
            cancelStreams(reason: .authRequired)
            followHealth = .down
            activityHealth = .down
            pendingReauthRestart = true
            return []

        case let .followConnected(generation):
            guard isCurrentFollow(generation) else {
                return []
            }
            followHealth = .connected
            breadcrumb?(
                "Chat.streamConnect",
                ["channel": "follow", "generation": String(generation)]
            )
            return []

        case let .followDropped(generation, _):
            guard isCurrentFollow(generation) else {
                return []
            }
            followHealth = .down
            return []

        case let .activityConnected(generation):
            guard isCurrentActivity(generation) else {
                return []
            }
            activityHealth = .connected
            breadcrumb?(
                "Chat.streamConnect",
                ["channel": "activity", "generation": String(generation)]
            )
            return []

        case let .activityDropped(generation, _):
            guard isCurrentActivity(generation) else {
                return []
            }
            activityHealth = .down
            return []

        case .syncStarted:
            phase = .syncing
            return []

        case .syncFinished:
            phase = .idle
            return []

        case let .pushHintReceived(conversationID):
            // A push arriving while foregrounded is a low-latency hint to refresh
            // the referenced conversation + list (§4.6). Backgrounded, it is a
            // no-op: silent-push/background refresh is explicitly out of scope
            // (§4.8) — the OS delivers the notification and the next foreground
            // resync reconciles.
            guard lifecycle == .foreground else {
                return []
            }
            return [.targetedRefresh(conversationID: conversationID)]
        }
    }

    private func reportStreamRestart(
        channel: String,
        reason: RestartReason,
        generation: Int,
        conversationID: String? = nil
    ) {
        var extraData = [
            "channel": channel,
            "reason": reason.rawValue,
            "generation": String(generation),
        ]
        if channel == "follow" {
            extraData["conversation_id"] = conversationID ?? delegate?.currentConversationID() ?? "none"
        }
        breadcrumb?("Chat.streamRestart", extraData)
    }

    private func reportStreamDisconnect(
        channel: String,
        generation: Int,
        conversationID: String? = nil,
        connectedAt: Date?,
        error: Error?,
        deliberateStop: Bool
    ) {
        let connectedSeconds = connectedAt.map { max(0, Date().timeIntervalSince($0)) } ?? 0
        var extraData = [
            "channel": channel,
            "generation": String(generation),
            "reason": disconnectReason(
                channel: channel,
                generation: generation,
                error: error,
                deliberateStop: deliberateStop
            ),
            "connected_seconds": String(format: "%.3f", connectedSeconds),
        ]
        if channel == "follow", let conversationID {
            extraData["conversation_id"] = conversationID
        }
        breadcrumb?("Chat.streamDisconnect", extraData)
    }

    private func disconnectReason(
        channel: String,
        generation: Int,
        error: Error?,
        deliberateStop: Bool
    ) -> String {
        let isCurrentGeneration = channel == "follow"
            ? isCurrentFollow(generation)
            : isCurrentActivity(generation)
        if !isCurrentGeneration {
            return "superseded"
        }
        if Task.isCancelled || error is CancellationError {
            return "cancelled"
        }
        if let apiError = error as? ChatAPIError,
           case .server(let statusCode, _) = apiError {
            switch statusCode {
            case 410: return "http410"
            case 401: return "http401"
            case 403: return "http403"
            default: return "other"
            }
        }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cancelled: return "cancelled"
            case .timedOut: return "timedOut"
            case .networkConnectionLost: return "networkConnectionLost"
            default: return "other"
            }
        }
        if deliberateStop {
            return "deliberateStop"
        }
        return error == nil ? "cleanEOF" : "other"
    }

    private func reportPresentationIfChanged() {
        let currentPresentation = presentation
        guard currentPresentation != lastReportedPresentation else {
            return
        }
        let previousPresentation = lastReportedPresentation
        lastReportedPresentation = currentPresentation
        breadcrumb?(
            "Chat.presentation",
            [
                "from": previousPresentation.rawValue,
                "to": currentPresentation.rawValue,
                "follow_health": followHealth.rawValue,
                "activity_health": activityHealth.rawValue,
                "phase": phase.rawValue,
                "reachability": reachability.rawValue,
                "auth_state": authState.rawValue,
            ]
        )
    }
}

/// One-shot latch guarding a `CheckedContinuation` against a double resume when
/// two racing tasks (a task-completion waiter and a timeout) may each try to
/// resume it. Only the first `tryResume` wins.
@MainActor
private final class ResumeOnce {
    private var resumed = false

    func tryResume() -> Bool {
        guard !resumed else {
            return false
        }
        resumed = true
        return true
    }
}
