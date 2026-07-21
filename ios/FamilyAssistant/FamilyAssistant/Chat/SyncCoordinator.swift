import Foundation

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
        case runResync(generation: Int)
    }

    private(set) var lifecycle: Lifecycle = .foreground
    private(set) var reachability: Reachability = .unknown
    private(set) var authState: AuthState = .ok
    private(set) var followHealth: ChannelHealth = .down
    private(set) var activityHealth: ChannelHealth = .down
    private(set) var phase: ReconciliationPhase = .idle
    private(set) var cameFromBackground = false
    private(set) var generation = 0

    private let pathMonitor: PathMonitoring
    private let followReconnectInitialDelaySeconds: Double
    private let followReconnectMaxDelaySeconds: Double

    weak var delegate: SyncStreamDelegate?

    // `nonisolated(unsafe)` so the synchronous `deinit` (a nonisolated context)
    // can cancel the owned stream tasks to tear down their open SSE connections.
    // Task is Sendable and all mutation otherwise happens on the main actor.
    private nonisolated(unsafe) var followTask: Task<Void, Never>?
    private nonisolated(unsafe) var activityTask: Task<Void, Never>?
    private var followConversationID: String?

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

    func isCurrent(_ generation: Int) -> Bool {
        generation == self.generation
    }

    @discardableResult
    func bumpGeneration() -> Int {
        generation += 1
        return generation
    }

    /// Maps a raw scene-phase observation onto the coordinator's lifecycle events. The
    /// `didBackground`/`isActive` split lets callers distinguish a real background (which must latch
    /// `cameFromBackground`) from an `.inactive` blip that never backgrounded.
    func scenePhaseChanged(didBackground: Bool, isActive: Bool) {
        if didBackground {
            apply(.backgrounded)
        } else if isActive {
            apply(.foregrounded)
        }
    }

    // MARK: - Stream ownership

    /// (Re)start the per-conversation follow stream for `conversationID`. Owns the
    /// reconnect loop (capped exponential backoff, catch-up on connect and on
    /// failed connect, 410 buffer-rotation handling). All event application is
    /// delegated to the view model. Cancelling the previous task before starting a
    /// new one is the single-owner cancellation the design requires.
    func startFollowStream(conversationID: String) {
        followTask?.cancel()
        followConversationID = conversationID
        let generation = self.generation
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
        activityTask?.cancel()
        let generation = self.generation
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
                    // Connection failed or dropped; fall through to backoff+retry.
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
    func cancelFollowStream() {
        followTask?.cancel()
    }

    /// Cancel both owned streams (deinit / logout).
    func cancelStreams() {
        followTask?.cancel()
        activityTask?.cancel()
    }

    /// Run the foreground resync: restart the follow stream for the active
    /// conversation and the activity stream, matching the former
    /// `reconnectLiveUpdates()`. The follow stream is restarted only when a
    /// conversation is selected.
    func runResync() {
        if let followConversationID {
            startFollowStream(conversationID: followConversationID)
        }
        startActivityStream()
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
            let newGeneration = bumpGeneration()
            return [.runResync(generation: newGeneration)]

        case .backgrounded:
            lifecycle = .background
            cameFromBackground = true
            return []

        case let .reachabilityChanged(reachability):
            self.reachability = reachability
            return []

        case .authRefreshing:
            authState = .refreshing
            return []

        case .authOK:
            authState = .ok
            return []

        case .authRequired:
            authState = .authRequired
            return []

        case let .followConnected(generation):
            guard isCurrent(generation) else {
                return []
            }
            followHealth = .connected
            return []

        case let .followDropped(generation, _):
            guard isCurrent(generation) else {
                return []
            }
            followHealth = .down
            return []

        case let .activityConnected(generation):
            guard isCurrent(generation) else {
                return []
            }
            activityHealth = .connected
            return []

        case let .activityDropped(generation, _):
            guard isCurrent(generation) else {
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
