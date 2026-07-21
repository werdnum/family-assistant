import Foundation

/// The application-side steps a foreground resync drives. Implemented by
/// ``ChatViewModel``; factored behind a protocol so the orchestration ordering
/// (auth gate → snapshots → restart streams) is unit-testable against a fake
/// host without standing up the full view model and its `URLProtocol` backend.
///
/// The host is the source of truth for the two values every apply guards on —
/// the coordinator `generation` and the selected conversation — so a resync
/// started before a background bump or a conversation switch discards its
/// snapshot instead of clobbering current state.
@MainActor
protocol ResyncHost: AnyObject {
    /// The coordinator generation current at the moment this is read. A resync
    /// captures it at the start and re-reads it before applying snapshots; a
    /// mismatch means a newer lifecycle transition superseded this resync.
    var resyncGeneration: Int { get }

    /// The conversation whose messages a resync snapshots. A switch mid-resync
    /// changes this, so the message snapshot is discarded on a mismatch.
    var resyncSelectedConversationID: String? { get }

    /// Complete one auth gate before touching the network: a single-flight token
    /// refresh when the stored token is near expiry. Throws when the refresh is
    /// rejected (the credentials are gone); the resync then aborts cleanly and
    /// the coordinator's `authRequired` presentation — set by the auth layer —
    /// stands, with no error modal.
    func gateAuthIfNeeded(generation: Int) async throws

    /// Snapshot the full conversation list with full-replacement semantics, so a
    /// conversation deleted server-side while backgrounded converges (disappears)
    /// on resume rather than lingering from the held list.
    func applyListSnapshot() async

    /// Snapshot the selected conversation's messages and `active_turns`, merging
    /// around the live send session and tail-attaching to any running turn the
    /// server reports.
    func applyMessagesSnapshot(conversationID: String) async

    /// Hand the live connections back to the coordinator's reconnect loops.
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
/// Ordering shipped in this commit (snapshot-then-subscribe; the
/// subscribe-then-buffer reorder that closes the lost-wakeup race lands in the
/// next commit):
///
/// 1. Generation is already bumped by the trigger (background→foreground, path
///    recovery). The resync captures it.
/// 2. Auth gate: single-flight refresh if near expiry. A rejection aborts.
/// 3. Snapshots: full conversation list (full replacement) + selected
///    conversation messages + `active_turns`, applied only while generation and
///    selection still match.
/// 4. Restart the streams through the coordinator.
///
/// Coalescing: a resync request that arrives while one is running joins the
/// in-flight task instead of starting a second, so a burst of foreground /
/// path-recovery triggers does the snapshot work exactly once.
@MainActor
final class ResyncOrchestrator {
    private weak var host: ResyncHost?
    private var currentTask: Task<Void, Never>?

    init(host: ResyncHost) {
        self.host = host
    }

    /// Start a resync, or join the one already running. Returns the driving task
    /// so callers (and tests) can await completion.
    @discardableResult
    func request() -> Task<Void, Never> {
        if let currentTask {
            return currentTask
        }
        let task = Task { [weak self] in
            await self?.run()
            self?.currentTask = nil
        }
        currentTask = task
        return task
    }

    private func run() async {
        guard let host else {
            return
        }
        host.resyncPhaseDidStart()
        defer { host.resyncPhaseDidFinish() }

        let generation = host.resyncGeneration

        do {
            try await host.gateAuthIfNeeded(generation: generation)
        } catch {
            // The refresh was rejected (credentials gone) or otherwise failed.
            // Abort cleanly: the auth layer has already latched `authRequired`
            // where appropriate, and a resync must never raise an error modal.
            return
        }

        guard !Task.isCancelled, host.resyncGeneration == generation else {
            return
        }

        let selectedConversationID = host.resyncSelectedConversationID

        await host.applyListSnapshot()

        guard !Task.isCancelled, host.resyncGeneration == generation else {
            return
        }

        if let selectedConversationID,
           host.resyncSelectedConversationID == selectedConversationID {
            await host.applyMessagesSnapshot(conversationID: selectedConversationID)
        }

        guard !Task.isCancelled, host.resyncGeneration == generation else {
            return
        }

        host.restartStreams()
    }
}
