import Foundation

/// A stream event buffered during a resync, held without dispatch until the
/// authoritative snapshots have been applied, then drained through the same
/// steady-state handlers the coordinator's loops use.
enum BufferedResyncEvent {
    case follow(ChatStreamEvent)
    case activitySignal
}

/// The application-side steps a foreground resync drives. Implemented by
/// ``ChatViewModel``; factored behind a protocol so the orchestration ordering
/// (subscribe-and-buffer → snapshots → drain → hand over) is unit-testable
/// against a fake host without standing up the full view model and its
/// `URLProtocol` backend.
///
/// The host is the source of truth for the values every apply guards on — the
/// coordinator's per-channel `followGeneration`/`activityGeneration` and the
/// selected conversation — so a resync started before a background bump or a
/// conversation switch discards its snapshot instead of clobbering current state.
@MainActor
protocol ResyncHost: AnyObject {
    /// The coordinator's follow/activity generations current when read. A resync
    /// captures both at the start and re-reads them before applying snapshots; a
    /// mismatch on EITHER means a newer lifecycle transition (background,
    /// foreground, recovery — each bumps both) or a conversation switch (follow
    /// only) superseded this resync. Drained follow events are fenced by the
    /// follow generation and drained activity signals by the activity generation,
    /// matching the steady-state per-channel fences.
    var resyncFollowGeneration: Int { get }
    var resyncActivityGeneration: Int { get }

    /// The conversation whose messages a resync snapshots. A switch mid-resync
    /// changes this, so the message snapshot is discarded on a mismatch.
    var resyncSelectedConversationID: String? { get }

    /// Await termination of the old follow/activity consumer tasks torn down on
    /// background before this resync establishes fresh streams (§4.3). Bounded so a
    /// socket-wedged old task can't wedge the resync; the generation fence still
    /// rejects any late event it emits.
    func awaitStreamTermination() async

    /// Complete one auth gate before touching the network: a single-flight token
    /// refresh when the stored token is near expiry. Throws when the refresh is
    /// rejected (the credentials are gone); the resync then aborts cleanly and
    /// the coordinator's `authRequired` presentation — set by the auth layer —
    /// stands, with no error modal.
    func gateAuthIfNeeded(generation: Int) async throws

    /// Establish the selected conversation's follow stream: open the connection
    /// and return the event stream once response headers are received ("the
    /// existing connect signal"). Returns nil when no conversation is selected or
    /// the connect fails — the resync then proceeds without a buffered follow
    /// channel and finishes degraded.
    func establishFollowStream(
        conversationID: String,
        generation: Int
    ) async -> AsyncThrowingStream<ChatStreamEvent, Error>?

    /// Establish the account-global activity stream, returning the event stream
    /// once headers are received. Returns nil when the connect fails.
    func establishActivityStream(
        generation: Int
    ) async -> AsyncThrowingStream<ChatConversationActivity, Error>?

    /// Snapshot the full conversation list with full-replacement semantics, so a
    /// conversation deleted server-side while backgrounded converges (disappears)
    /// on resume rather than lingering from the held list.
    func applyListSnapshot() async

    /// Snapshot the selected conversation's messages and `active_turns`, merging
    /// around the live send session and tail-attaching to any running turn the
    /// server reports.
    func applyMessagesSnapshot(conversationID: String) async

    /// Drain one buffered follow event through the SAME steady-state handler the
    /// coordinator's follow loop uses, so generation fencing and turn routing are
    /// identical.
    func drainFollowEvent(
        _ event: ChatStreamEvent,
        conversationID: String,
        generation: Int
    ) async

    /// Drain one buffered activity signal through the SAME steady-state handler
    /// the coordinator's activity loop uses (a recent-list refresh).
    func drainActivitySignal(generation: Int) async

    /// Hand the live connections back to the coordinator's reconnect loops. The
    /// resync's own streams are closed as this runs; the loops reconnect
    /// immediately (health is published only by their real connect events).
    func restartStreams()

    /// Publish the reconciliation phase so the indicator shows `.syncing` for the
    /// duration of the resync.
    func resyncPhaseDidStart()
    func resyncPhaseDidFinish()
}

/// Drives the foreground reconciliation (`design §4.4`) as one coalesced,
/// cancellable unit of work owned by ``ChatViewModel`` (not the coordinator: the
/// coordinator owns stream tasks + the reducer, while these app-side steps stay
/// behind the ``SyncStreamDelegate`` boundary).
///
/// Ordering (subscribe-then-buffer, closing the lost-wakeup race the review
/// identified — the activity stream has no replay, so a snapshot-then-subscribe
/// order could drop anything committed between the fetch and the subscription):
///
/// 1. Generation is already bumped by the trigger; the resync captures it.
/// 2. AWAIT termination of the old follow/activity consumer tasks torn down on
///    background (§4.3), before any new stream is established, so the old and new
///    consumers never briefly overlap on the same conversation.
/// 3. Auth gate: single-flight refresh if near expiry. A rejection aborts.
/// 4. ESTABLISH the activity + selected-conversation follow streams first
///    ("established" = headers received) and BUFFER their events into a bounded
///    queue without dispatching.
/// 5. Fetch authoritative snapshots (full conversation list + selected
///    conversation messages + `active_turns`), applied only while generation and
///    selection still match.
/// 6. DRAIN the buffer through the same steady-state handlers the loops use.
/// 7. Hand the live connections to the coordinator's reconnect loops.
///
/// Buffer overflow during a resync (which should not happen during a snapshot
/// fetch) aborts and RESTARTS the resync rather than silently dropping events;
/// after a bounded number of restarts it finishes degraded (the loop reconnect
/// + snapshot then reconcile).
///
/// Handover fallback (§4.4 acceptable fallback): rather than transferring the
/// already-open sockets into the coordinator's loops — which own backoff / 410 /
/// catch-up and cannot share a once-iterable `AsyncThrowingStream` — the drained
/// resync streams are closed and the loops reconnect immediately. The activity
/// stream has no replay, so the only residual gap is activity events between the
/// drain and the loop's reconnect; the immediate loop start minimizes it and a
/// final list refetch after handover closes it. Follow-stream content always
/// comes from persisted history via the loop's connect-time catch-up, so the
/// extra connect risks no lost follow content.
///
/// Coalescing: a resync request that arrives while one is running joins the
/// in-flight task instead of starting a second, so a burst of foreground /
/// path-recovery triggers does the snapshot work exactly once.
@MainActor
final class ResyncOrchestrator {
    private weak var host: ResyncHost?
    // Cancelled from the owning view model's nonisolated `deinit` (see
    // `cancelInFlight`), so — like the coordinator's stream tasks — these are
    // `nonisolated(unsafe)`; `Task.cancel()` is itself thread-safe.
    nonisolated(unsafe) private var currentTask: Task<Void, Never>?

    private let bufferCapacity: Int
    private let maxRestarts: Int

    /// Buffered stream events held (undispatched) while the snapshots are
    /// fetched, then drained in order. Actor-isolated: appended by the buffering
    /// tasks and read by the drain, all on the main actor.
    private var buffer: [BufferedResyncEvent] = []
    private var bufferOverflowed = false
    nonisolated(unsafe) private var followBufferingTask: Task<Void, Never>?
    nonisolated(unsafe) private var activityBufferingTask: Task<Void, Never>?

    /// The generations + selection the currently-running attempt is reconciling. A
    /// `request()` that arrives while a resync runs joins the in-flight task, but
    /// if its captured target differs (generations bumped by a background /
    /// foreground / reachability transition, or the user switched conversations)
    /// the running attempt will abort on its guards and reconcile the WRONG target
    /// — potentially after streams were torn down, leaving live updates dead. So a
    /// differing request is remembered as superseding and a fresh run starts when
    /// the stale one finishes (F2/F4).
    private var runningTarget: ResyncTarget?
    private var pendingSupersede = false

    init(host: ResyncHost, bufferCapacity: Int = 256, maxRestarts: Int = 3) {
        self.host = host
        self.bufferCapacity = bufferCapacity
        self.maxRestarts = maxRestarts
    }

    /// The reconciliation target captured from the host: the per-channel
    /// generations and the selected conversation. Two requests with the same target
    /// coalesce; a differing target supersedes.
    private struct ResyncTarget: Equatable {
        let follow: Int
        let activity: Int
        let selectedConversationID: String?

        @MainActor
        init(host: ResyncHost) {
            follow = host.resyncFollowGeneration
            activity = host.resyncActivityGeneration
            selectedConversationID = host.resyncSelectedConversationID
        }
    }

    /// Start a resync, or join the one already running. Returns the driving task
    /// so callers (and tests) can await completion. When a request joins an
    /// in-flight resync whose target no longer matches (generations bumped or the
    /// selection changed), it is remembered as superseding so a fresh run covers
    /// the new target once the stale attempt unwinds.
    @discardableResult
    func request() -> Task<Void, Never> {
        if let currentTask {
            if let host, runningTarget != nil, ResyncTarget(host: host) != runningTarget {
                pendingSupersede = true
            }
            return currentTask
        }
        let task = Task { [weak self] in
            await self?.run()
            self?.currentTask = nil
        }
        currentTask = task
        return task
    }

    /// Cancel the in-flight resync and its buffering tasks from a nonisolated
    /// context. The owning view model calls this from its `deinit` so a
    /// fire-and-forget resync (e.g. the auth-observer re-auth path, which does not
    /// await `request()`) cannot outlive the model — holding open SSE sockets and
    /// running its handover after the owner is gone (which also leaked the resync's
    /// async work across test boundaries). Mirrors `SyncCoordinator.cancelOwnedStreams`.
    nonisolated func cancelInFlight() {
        currentTask?.cancel()
        followBufferingTask?.cancel()
        activityBufferingTask?.cancel()
    }

    private func run() async {
        guard let host else {
            return
        }
        host.resyncPhaseDidStart()
        defer {
            runningTarget = nil
            host.resyncPhaseDidFinish()
        }

        while true {
            pendingSupersede = false
            let outcome = await runOneResync(host: host)
            switch outcome {
            case .completed, .aborted:
                // A supersede request that arrived during this run (a differing
                // target) starts a fresh run rather than leaving the new target
                // unreconciled with streams possibly torn down.
                if pendingSupersede {
                    continue
                }
                return
            case .superseded:
                // A selection switch mid-attempt aborted the stale attempt without
                // draining its follow buffer; re-run targeting the new selection.
                continue
            }
        }
    }

    /// One full resync pass INCLUDING its bounded overflow restarts. Returns the
    /// terminal outcome for the `run()` supersede loop.
    private func runOneResync(host: ResyncHost) async -> RunOutcome {
        var attempt = 0
        while true {
            runningTarget = ResyncTarget(host: host)
            let outcome = await attemptResync(host: host)
            switch outcome {
            case .completed:
                return .completed
            case .aborted:
                return .aborted
            case .superseded:
                return .superseded
            case .overflowRestart:
                attempt += 1
                if attempt > maxRestarts {
                    // Bounded restarts exhausted: finish degraded. Hand the live
                    // connections to the coordinator's reconnect loops so a healthy
                    // channel is re-established; their reconnect + connect-time
                    // catch-up reconcile content (the loops fence by generation),
                    // and the indicator reflects real per-channel health.
                    host.restartStreams()
                    return .completed
                }
            }
        }
    }

    private enum RunOutcome {
        case completed
        case aborted
        case superseded
    }

    private enum ResyncOutcome {
        case completed
        case aborted
        case overflowRestart
        /// The selected conversation changed mid-attempt: abort WITHOUT draining
        /// the stale conversation's follow buffer and re-run for the new selection.
        case superseded
    }

    /// The follow/activity generations captured at the start of a resync attempt,
    /// used to detect a superseding transition on either channel.
    private struct ResyncGenerations {
        let follow: Int
        let activity: Int
    }

    /// Whether `host` still owns both channel generations captured at the start of
    /// the attempt. A mismatch on either means a newer lifecycle transition
    /// (background/foreground/recovery bump both; a conversation switch bumps
    /// follow) superseded this resync.
    private func generationsStillCurrent(
        _ generations: ResyncGenerations,
        host: ResyncHost
    ) -> Bool {
        host.resyncFollowGeneration == generations.follow
            && host.resyncActivityGeneration == generations.activity
    }

    private func attemptResync(host: ResyncHost) async -> ResyncOutcome {
        resetBuffer()
        let generations = ResyncGenerations(
            follow: host.resyncFollowGeneration,
            activity: host.resyncActivityGeneration
        )

        // Step 2: await termination of the old consumer tasks (§4.3) before any new
        // stream is established below. Idempotent across overflow restarts: the
        // coordinator's tasks are cleared on the first await, so a restart's call
        // returns at once.
        await host.awaitStreamTermination()

        do {
            try await host.gateAuthIfNeeded(generation: generations.follow)
        } catch let error as AuthError {
            switch error {
            case .transient:
                // A TRANSIENT refresh failure (network error, 5xx) is NOT a
                // rejection: `authRequired` is not latched and a re-auth trigger
                // fires elsewhere only for real rejections. Because
                // `awaitStreamTermination()` above already tore both loops down,
                // returning here would strand the app with NO loops running until
                // some later trigger. Restart the loops instead so their own
                // backoff/retry (and near-expiry force-refresh on connect) resumes —
                // but ONLY while the captured generations are still current. If the
                // app backgrounded mid-gate (bumping both generations and cancelling
                // the streams by policy), restarting here would reopen the advisory
                // streams the background policy just cancelled; leave reconnection to
                // the next foreground resync instead.
                if generationsStillCurrent(generations, host: host) {
                    host.restartStreams()
                }
                return .aborted
            case .authRejected, .noCredentials, .invalidServerURL, .exchangeFailed:
                // A terminal rejection: the auth layer latched `authRequired`
                // (driving the coordinator's dedicated presentation) and a re-auth
                // recovery trigger fires. Abort cleanly with no error modal.
                return .aborted
            }
        } catch {
            // A non-`AuthError` failure from the gate is treated as transient for
            // the same reason: don't strand the torn-down loops. Same generation
            // guard as the transient case — don't reopen streams a background policy
            // cancelled mid-gate.
            if generationsStillCurrent(generations, host: host) {
                host.restartStreams()
            }
            return .aborted
        }

        guard !Task.isCancelled, generationsStillCurrent(generations, host: host) else {
            return .aborted
        }

        let selectedConversationID = host.resyncSelectedConversationID

        // Step 4: establish + buffer BEFORE fetching snapshots. The buffering
        // tasks interleave with the snapshot awaits below, so an event committed
        // after subscribe but before the fetch completes lands in the buffer and
        // is drained after the snapshot (closing the lost-wakeup race).
        await startBuffering(
            host: host,
            selectedConversationID: selectedConversationID,
            generations: generations
        )

        // Step 5: authoritative snapshots (full-replacement list + selected
        // conversation messages/active_turns).
        await host.applyListSnapshot()

        if bufferOverflowed {
            return abortForOverflow()
        }
        guard !Task.isCancelled, generationsStillCurrent(generations, host: host) else {
            stopBuffering()
            return .aborted
        }

        // A selection switch mid-attempt (the list snapshot's await let the user
        // move to another thread) SUPERSEDES this attempt: the buffered follow
        // events belong to the OLD conversation, and the follow handler fences by
        // generation — not conversation — so draining them here could route stale
        // tokens at the new thread, and `restartStreams()` could reconnect the old
        // followConversationID. Abort WITHOUT draining the stale follow buffer and
        // re-run targeting the new selection. The buffered activity signals are
        // account-global (they only trigger a full-replacement list refetch), so
        // dropping them is safe — the fresh run's snapshot + final list refetch
        // reconcile the list regardless.
        if host.resyncSelectedConversationID != selectedConversationID {
            stopBuffering()
            return .superseded
        }

        if let selectedConversationID {
            await host.applyMessagesSnapshot(conversationID: selectedConversationID)
        }

        if bufferOverflowed {
            return abortForOverflow()
        }
        if host.resyncSelectedConversationID != selectedConversationID {
            stopBuffering()
            return .superseded
        }
        guard !Task.isCancelled, generationsStillCurrent(generations, host: host) else {
            stopBuffering()
            return .aborted
        }

        // Step 6: stop buffering (the tasks that consumed events during the
        // snapshot-fetch window into `buffer`), then drain the buffer through the
        // steady-state handlers.
        stopBuffering()
        if bufferOverflowed {
            return abortForOverflow()
        }
        await drainBuffer(
            host: host,
            selectedConversationID: selectedConversationID,
            generations: generations
        )

        guard !Task.isCancelled, generationsStillCurrent(generations, host: host) else {
            return .aborted
        }

        // Step 7: hand the live connections to the coordinator's reconnect loops.
        // The resync streams are already closed (stopBuffering cancelled them);
        // the loops reconnect immediately.
        host.restartStreams()

        // Fallback mitigation: the activity stream has no replay, so close the
        // residual window between the drain and the loop's activity reconnect with
        // one final full-replacement list refetch.
        await host.applyListSnapshot()

        return .completed
    }

    private func abortForOverflow() -> ResyncOutcome {
        // Never silently drop events: on overflow, tear this attempt's buffering
        // down and restart the whole resync (bounded). The restart re-establishes
        // its own streams, so we do NOT hand off to the coordinator's loops here —
        // that would open a second follow consumer for the same conversation
        // alongside the restart's buffering subscription. The loops are handed the
        // connection only on a successful completion or a degraded give-up.
        stopBuffering()
        return .overflowRestart
    }

    private func startBuffering(
        host: ResyncHost,
        selectedConversationID: String?,
        generations: ResyncGenerations
    ) async {
        if let selectedConversationID,
           let stream = await host.establishFollowStream(
               conversationID: selectedConversationID,
               generation: generations.follow
           ) {
            followBufferingTask = Task { [weak self] in
                do {
                    // Enqueue every delivered event: a finished stream drains its
                    // buffered tail before ending, and a cancelled iterator over an
                    // unfinished stream simply returns nil (loop ends) — so no
                    // yielded-but-unbuffered event is dropped on handover.
                    //
                    // Reject a post-cancel delivery: cancellation is cooperative, so
                    // an element already produced before `stopBuffering` cancelled
                    // this task can still be delivered afterward. On the MainActor the
                    // isCancelled check is atomic with element receipt (no await
                    // between), so an overflow-restart that reset the shared buffer
                    // cannot see this stale element appended to the next attempt's
                    // buffer (it would otherwise be drained and re-rendered when the
                    // replacement follow connection replays the same seq). A natural
                    // stream finish is not cancellation, so tail draining is intact.
                    for try await event in stream {
                        if Task.isCancelled { break }
                        self?.enqueue(.follow(event))
                    }
                } catch {
                    // A drop during buffering is fine: the loop that takes over on
                    // handover reconnects, and the snapshot already covers content.
                }
            }
        }

        if let stream = await host.establishActivityStream(generation: generations.activity) {
            activityBufferingTask = Task { [weak self] in
                do {
                    for try await _ in stream {
                        if Task.isCancelled { break }
                        self?.enqueue(.activitySignal)
                    }
                } catch {}
            }
        }
    }

    private func stopBuffering() {
        // Cancel without awaiting: the resync must never block on a live SSE
        // connection (blocking on the very sockets it is reconciling would defeat
        // the point). Events the buffering tasks already consumed during the
        // snapshot-fetch window are in `buffer` and get drained; any event still
        // in flight is covered by the loop's connect-time catch-up (follow) and
        // the final list refetch (activity, which has no replay).
        followBufferingTask?.cancel()
        activityBufferingTask?.cancel()
        followBufferingTask = nil
        activityBufferingTask = nil
    }

    private func drainBuffer(
        host: ResyncHost,
        selectedConversationID: String?,
        generations: ResyncGenerations
    ) async {
        while !buffer.isEmpty {
            // A selection switch mid-drain (the per-event await let the user move to
            // another thread) supersedes this attempt: the remaining buffered follow
            // events belong to the OLD conversation, and during a resync
            // `selectConversation` cannot bump the follow generation to fence them
            // (the coordinator's follow task was already nilled by
            // awaitStreamTermination), so draining them would route stale tokens at
            // the new thread. Stop draining — the buffered follow events are the old
            // thread's, and the remaining activity signals are account-global and
            // safely reconciled by the fresh run's snapshot (same rationale as the
            // pre-drain supersede checks in `runAttempt`).
            if host.resyncSelectedConversationID != selectedConversationID {
                return
            }
            let event = buffer.removeFirst()
            switch event {
            case let .follow(followEvent):
                if let selectedConversationID {
                    await host.drainFollowEvent(
                        followEvent,
                        conversationID: selectedConversationID,
                        generation: generations.follow
                    )
                }
            case .activitySignal:
                await host.drainActivitySignal(generation: generations.activity)
            }
        }
    }

    private func enqueue(_ event: BufferedResyncEvent) {
        guard !bufferOverflowed else {
            return
        }
        if buffer.count >= bufferCapacity {
            bufferOverflowed = true
            return
        }
        buffer.append(event)
    }

    private func resetBuffer() {
        buffer.removeAll(keepingCapacity: true)
        bufferOverflowed = false
    }
}
