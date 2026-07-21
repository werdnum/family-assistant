import Foundation

/// Outcome of a forced auth refresh on stream-connect 401/403.
enum StreamConnectAuthResult {
    /// A fresh token was minted; the connect is worth retrying at once.
    case reconnected
    /// The refresh was rejected or there were no credentials; authRequired is latched.
    case terminalAuth
    /// The refresh failed transiently (network/5xx); retry with backoff, no authRequired.
    case transientRetry
}

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

    /// A stream connect was rejected with a response-time 401/403. Force one
    /// coalesced auth refresh and report the outcome: `.reconnected` means a fresh
    /// token was minted and the connect is worth retrying at once; `.terminalAuth`
    /// means the refresh was itself rejected or there were no credentials — the
    /// delegate has already latched `authRequired`, so the loop must stop rather
    /// than spin the backoff replaying the rejected token; `.transientRetry` means
    /// the refresh failed transiently (network/5xx) and the loop should retry with
    /// backoff but keep the current session intact (no authRequired latch).
    func forceAuthRefreshForStreamConnect() async -> StreamConnectAuthResult
}

@MainActor
@Observable
final class SyncCoordinator {
    enum Lifecycle {
        case foreground
        case background
    }

    enum Reachability {
        case satisfied
        case unsatisfied
        case unknown
    }

    enum AuthState {
        case ok
        case refreshing
        case authRequired
    }

    enum ChannelHealth {
        case connected
        case reconnecting
        case down
    }

    enum ReconciliationPhase {
        case idle
        case syncing
    }

    enum Presentation {
        case live
        case syncing
        case degraded
        case offline
        case authRequired
        case suspended
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
    }

    enum SyncEffect: Equatable {
        case startFollowStream(generation: Int)
        case startActivityStream(generation: Int)
        case cancelStreams
        case runResync
    }

    private(set) var lifecycle: Lifecycle = .foreground
    private(set) var reachability: Reachability = .unknown
    private(set) var authState: AuthState = .ok
    private(set) var followHealth: ChannelHealth = .down
    private(set) var activityHealth: ChannelHealth = .down
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

    weak var delegate: SyncStreamDelegate?

    // `nonisolated(unsafe)` so the synchronous `deinit` (a nonisolated context)
    // can cancel the owned stream tasks to tear down their open SSE connections.
    // Task is Sendable and all mutation otherwise happens on the main actor.
    private nonisolated(unsafe) var followTask: Task<Void, Never>?
    private nonisolated(unsafe) var activityTask: Task<Void, Never>?

    init(
        pathMonitor: PathMonitoring,
        followReconnectInitialDelaySeconds: Double = 2,
        followReconnectMaxDelaySeconds: Double = 30
    ) {
        self.pathMonitor = pathMonitor
        self.followReconnectInitialDelaySeconds = followReconnectInitialDelaySeconds
        self.followReconnectMaxDelaySeconds = followReconnectMaxDelaySeconds
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
        if followHealth == .connected, activityHealth == .connected {
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
    func bumpFollowGeneration() -> Int {
        followGeneration += 1
        return followGeneration
    }

    @discardableResult
    func bumpActivityGeneration() -> Int {
        activityGeneration += 1
        return activityGeneration
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
    func startFollowStream(conversationID: String) {
        // Replacing a live task: cancelling it does not stop its in-flight delegate
        // callbacks synchronously, so they would still pass the generation check and
        // merge/ack stale events into the new stream's state. Bump the follow
        // generation so the replacement runs under a fresh one and the cancelled
        // task's late callbacks are rejected.
        if followTask != nil {
            bumpFollowGeneration()
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
        let initialDelay = followReconnectInitialDelaySeconds
        let maxDelay = followReconnectMaxDelaySeconds
        followTask = Task { [weak self] in
            var delay = initialDelay
            while !Task.isCancelled {
                var deliberateStop = false
                var connected = false
                var streamError: Error?
                do {
                    guard let stream = try await self?.delegate?.openFollowStream(
                        conversationID: conversationID,
                        generation: generation
                    ) else {
                        return
                    }
                    connected = true
                    self?.apply(.followConnected(generation: generation))
                    await self?.delegate?.followStreamDidConnect(
                        conversationID: conversationID,
                        generation: generation
                    )
                    delay = initialDelay
                    for try await event in stream {
                        if Task.isCancelled {
                            break
                        }
                        let shouldContinue = await self?.delegate?.handleFollowEvent(
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
                        self?.delegate?.followBufferRotated(generation: generation)
                    }
                    // A response-time 401/403 on connect is a terminal auth failure,
                    // not an ordinary transient drop: replaying the rejected token
                    // would spin the backoff forever. Force one coalesced refresh.
                    if case ChatAPIError.server(let statusCode, _) = error,
                       statusCode == 401 || statusCode == 403 {
                        let result = await self?.delegate?.forceAuthRefreshForStreamConnect() ?? .terminalAuth
                        if Task.isCancelled {
                            break
                        }
                        switch result {
                        case .reconnected:
                            continue
                        case .terminalAuth:
                            deliberateStop = true
                        case .transientRetry:
                            streamError = error
                        }
                        if deliberateStop {
                            break
                        }
                    }
                }

                if Task.isCancelled || deliberateStop {
                    break
                }
                self?.delegate?.reportFollowStreamDrop(
                    conversationID: conversationID,
                    error: streamError,
                    generation: generation
                )
                // Suppression parity with the former
                // `markLiveUpdatesDisconnectedIfActive`: a drop while a send is
                // actively streaming must not degrade presentation. Suppress at
                // the event-emission level so the reducer never sees the drop.
                if self?.delegate?.shouldSurfaceFollowDrop() ?? false {
                    self?.apply(.followDropped(generation: generation, cleanEOF: streamError == nil))
                }
                if !connected {
                    await self?.delegate?.catchUpFollowHistory(
                        conversationID: conversationID,
                        generation: generation
                    )
                }
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, maxDelay)
            }
        }
    }

    /// (Re)start the account-global activity stream. Owns the same capped-backoff
    /// reconnect loop; list refresh on connect and on every ping is delegated.
    func startActivityStream() {
        // Same generation hole as `startFollowStream`: a replaced activity task's
        // in-flight callbacks would still pass the check and let a stale signal ack
        // into the new stream. Bump the activity generation so the replacement owns
        // a fresh one.
        if activityTask != nil {
            bumpActivityGeneration()
            // Same as the follow channel: a replaced activity task is no longer a
            // live connection, so demote a `.connected` health to `.reconnecting`
            // until the replacement reports a real `activityConnected`.
            if activityHealth == .connected {
                activityHealth = .reconnecting
            }
        }
        activityTask?.cancel()
        let generation = activityGeneration
        let initialDelay = followReconnectInitialDelaySeconds
        let maxDelay = followReconnectMaxDelaySeconds
        activityTask = Task { [weak self] in
            var delay = initialDelay
            while !Task.isCancelled {
                do {
                    guard let stream = try await self?.delegate?.openActivityStream(
                        generation: generation
                    ) else {
                        return
                    }
                    delay = initialDelay
                    self?.apply(.activityConnected(generation: generation))
                    await self?.delegate?.activityStreamDidSignal(generation: generation)
                    for try await _ in stream {
                        if Task.isCancelled {
                            break
                        }
                        guard self != nil else {
                            return
                        }
                        await self?.delegate?.activityStreamDidSignal(generation: generation)
                    }
                } catch {
                    var shouldBreak = false
                    // A response-time 401/403 on connect is a terminal auth failure
                    // (see the follow loop): force one coalesced refresh.
                    if case ChatAPIError.server(let statusCode, _) = error,
                       statusCode == 401 || statusCode == 403 {
                        let result = await self?.delegate?.forceAuthRefreshForStreamConnect() ?? .terminalAuth
                        if Task.isCancelled {
                            break
                        }
                        switch result {
                        case .reconnected:
                            continue
                        case .terminalAuth:
                            self?.apply(.activityDropped(generation: generation, cleanEOF: false))
                            shouldBreak = true
                        case .transientRetry:
                            // Fall through to backoff+retry below
                            break
                        }
                        if shouldBreak {
                            break
                        }
                    }
                }
                if Task.isCancelled {
                    break
                }
                self?.apply(.activityDropped(generation: generation, cleanEOF: false))
                try? await Task.sleep(for: .seconds(delay))
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
    func cancelFollowStream() {
        if followTask != nil {
            bumpFollowGeneration()
            if followHealth == .connected {
                followHealth = .reconnecting
            }
        }
        followTask?.cancel()
    }

    /// Cancel both owned streams (deinit / logout).
    func cancelStreams() {
        if followTask != nil {
            bumpFollowGeneration()
            if followHealth == .connected {
                followHealth = .reconnecting
            }
        }
        if activityTask != nil {
            bumpActivityGeneration()
            if activityHealth == .connected {
                activityHealth = .reconnecting
            }
        }
        followTask?.cancel()
        activityTask?.cancel()
    }

    /// Run the foreground resync: restart the follow stream for the active
    /// conversation and the activity stream, matching the former
    /// `reconnectLiveUpdates()`. The follow stream target is derived from the
    /// current conversation to avoid stale cached IDs from launch or switch.
    func runResync() {
        if let conversationID = delegate?.currentConversationID() {
            startFollowStream(conversationID: conversationID)
        }
        startActivityStream()
    }

    /// Reset backoff and retry the reconnect loops immediately on a reachability
    /// recovery, instead of leaving a loop asleep at capped backoff. Only channels
    /// that are not currently connected are restarted (recreating their task resets
    /// the local backoff to the initial delay and re-enters the connect attempt at
    /// once); a healthy channel is left alone to avoid needless churn. Restarting a
    /// loop is a retry, not a health assertion — health is only set by an actual
    /// connect. The follow target is derived from the current conversation so a
    /// stale cached ID doesn't restart the old stream.
    private func wakeReconnectLoops() {
        if followHealth != .connected, let conversationID = delegate?.currentConversationID() {
            startFollowStream(conversationID: conversationID)
        }
        if activityHealth != .connected {
            startActivityStream()
        }
    }

    @discardableResult
    func apply(_ event: SyncEvent) -> [SyncEffect] {
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
            return []

        case let .reachabilityChanged(reachability):
            let wasUnsatisfied = self.reachability == .unsatisfied
            self.reachability = reachability
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
            // unsatisfied→satisfied transition, reset backoff and wake the reconnect
            // loops now so a live path is retried immediately. Health still comes
            // only from actual connects.
            if reachability == .satisfied, wasUnsatisfied, lifecycle == .foreground {
                wakeReconnectLoops()
            }
            return []

        case .authRefreshing:
            authState = .refreshing
            return []

        case .authOK:
            let wasAuthRequired = authState == .authRequired
            authState = .ok
            // Re-auth just succeeded. While `authRequired` was latched the streams
            // could not connect (their requests 401'd), so any loop is now asleep
            // at capped backoff and would otherwise stay down for up to one
            // max-delay interval, keeping the stuck indicator up after sign-in.
            // Mirror the reachability-recovery wake: reset backoff and retry the
            // non-connected loops now. Health still comes only from real connects.
            if wasAuthRequired, lifecycle == .foreground {
                wakeReconnectLoops()
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
            cancelStreams()
            followHealth = .down
            activityHealth = .down
            return []

        case let .followConnected(generation):
            guard isCurrentFollow(generation) else {
                return []
            }
            followHealth = .connected
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
        }
    }
}
