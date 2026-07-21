import XCTest

@testable import FamilyAssistant

@MainActor
final class ResyncOrchestratorTests: XCTestCase {
    // MARK: - Coalescing, auth gate, snapshot fencing (§4.4 steps 1-2, 4-5)

    func testSnapshotsAppliedWhenGenerationAndSelectionMatch() async {
        let host = FakeResyncHost(generation: 3, selectedConversationID: "conv-1")
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.authGateCount, 1)
        XCTAssertEqual(host.messagesSnapshotConversationIDs, ["conv-1"])
        XCTAssertEqual(host.restartStreamsCount, 1)
        // Two list snapshots: the authoritative one, plus the final refetch that
        // closes the no-replay activity window on handover.
        XCTAssertEqual(host.listSnapshotCount, 2)
        XCTAssertEqual(host.phaseStartCount, 1)
        XCTAssertEqual(host.phaseFinishCount, 1)
    }

    func testConversationSwitchMidResyncAbortsMessageApply() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-a")
        // A switch to another conversation lands during the list snapshot, before
        // the message snapshot is applied.
        host.onListSnapshot = { host in
            if host.listSnapshotCount == 1 {
                host.selectedConversationID = "conv-b"
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertTrue(
            host.messagesSnapshotConversationIDs.isEmpty,
            "A conversation switch mid-resync must discard the stale message snapshot."
        )
        XCTAssertEqual(host.restartStreamsCount, 1, "Stream restart still proceeds for the new selection.")
        XCTAssertEqual(host.phaseFinishCount, 1)
    }

    func testGenerationBumpMidResyncAbortsRemainingApply() async {
        let host = FakeResyncHost(generation: 5, selectedConversationID: "conv-x")
        // A background bump lands during the list snapshot: the newer generation
        // owns state now, so neither the message snapshot nor the stream restart
        // should run for this superseded resync.
        host.onListSnapshot = { host in
            if host.listSnapshotCount == 1 {
                host.generation = 6
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.listSnapshotCount, 1)
        XCTAssertTrue(host.messagesSnapshotConversationIDs.isEmpty)
        XCTAssertEqual(host.restartStreamsCount, 0)
        XCTAssertEqual(host.phaseFinishCount, 1, "The syncing phase is always closed out.")
    }

    func testAuthRequiredMidResyncAbortsCleanly() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.authGateError = FakeAuthError.rejected
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.authGateCount, 1)
        XCTAssertEqual(host.listSnapshotCount, 0, "A rejected auth gate aborts before any snapshot.")
        XCTAssertTrue(host.messagesSnapshotConversationIDs.isEmpty)
        XCTAssertEqual(host.restartStreamsCount, 0)
        XCTAssertEqual(host.phaseFinishCount, 1, "The syncing phase is still closed out on abort.")
    }

    func testSecondRequestWhileRunningCoalesces() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        // Hold the first list snapshot open so the resync cannot complete before
        // the second request is issued; the second must join the in-flight task.
        let gate = AsyncGate()
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                await gate.wait()
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        // `request()` records the in-flight task synchronously, so a second call
        // issued before the first task's body finishes joins it rather than
        // starting a second run.
        let first = orchestrator.request()
        let second = orchestrator.request()

        gate.open()
        await first.value
        await second.value

        XCTAssertEqual(host.authGateCount, 1, "The joined request does no duplicate work.")
        // One completed resync: authoritative list snapshot + final refetch.
        XCTAssertEqual(host.listSnapshotCount, 2)
        XCTAssertEqual(host.restartStreamsCount, 1)
    }

    func testDeletionConvergesViaFullReplacementListSnapshot() async {
        // Full-replacement semantics: a conversation present locally but absent
        // from the server snapshot disappears on resync.
        let host = FakeResyncHost(generation: 1, selectedConversationID: nil)
        host.localConversationIDs = ["conv-kept", "conv-deleted"]
        host.serverConversationIDs = ["conv-kept"]
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(
            host.localConversationIDs,
            ["conv-kept"],
            "A conversation absent from the server snapshot must disappear (full replacement)."
        )
    }

    // MARK: - Await old-consumer termination (§4.3)

    func testResyncAwaitsOldConsumerTerminationBeforeEstablishingNewFollowStream() async {
        // §4.3: the old follow/activity consumer must be fully torn down before the
        // resync opens the new follow stream, so the two never briefly overlap.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.followStreamSource = ControllableFollowStream()
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.awaitTerminationCount, 1)
        let terminationIndex = host.stepLog.firstIndex(of: "awaitTermination")
        let establishIndex = host.stepLog.firstIndex(of: "establishFollow")
        XCTAssertNotNil(terminationIndex)
        XCTAssertNotNil(establishIndex)
        XCTAssertLessThan(
            terminationIndex ?? .max,
            establishIndex ?? .min,
            "Old-consumer termination must complete before the new follow stream is established."
        )
    }

    func testWedgedOldConsumerDoesNotHangResync() async {
        // A socket-wedged old task can't be waited on forever: the host's bounded
        // termination await returns, and the resync proceeds to establish + snapshot
        // rather than hanging. Modeled with a short bounded sleep in the hook.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.onAwaitTermination = {
            try? await Task.sleep(for: .milliseconds(20))
        }
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.awaitTerminationCount, 1)
        XCTAssertEqual(host.restartStreamsCount, 1, "The resync still completes past a wedged old consumer.")
        XCTAssertEqual(host.listSnapshotCount, 2)
    }

    // MARK: - Subscribe-then-buffer ordering (§4.4 steps 4/6/7)

    func testLostWakeupActivityEventBeforeSnapshotIsAppliedAfterSnapshot() async {
        // The lost-wakeup race: an activity event committed AFTER subscribe but
        // BEFORE the snapshot fetch completes must be applied after the snapshot.
        // Hold the snapshot fetch open, emit the event, release the fetch.
        let host = FakeResyncHost(generation: 1, selectedConversationID: nil)
        let activity = ControllableActivityStream()
        host.activityStreamSource = activity
        let snapshotGate = AsyncGate()
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                // The stream is subscribed by now; emit while the fetch hangs.
                activity.emit()
                await snapshotGate.wait()
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        let task = orchestrator.request()
        // Let the resync reach the held snapshot and the emitted event land in the
        // buffer (undispatched: the drain has not run yet).
        try? await waitUntil { host.listSnapshotCount == 1 && activity.emittedCount == 1 }
        XCTAssertEqual(
            host.activitySignalDrainCount, 0,
            "The event must be buffered, not dispatched, until the snapshot is applied."
        )

        snapshotGate.open()
        activity.finish()
        await task.value

        XCTAssertGreaterThanOrEqual(
            host.activitySignalDrainCount, 1,
            "The buffered activity event must be drained after the snapshot."
        )
    }

    func testBufferedFollowEventsReattachRunningTurnDuringResync() async {
        // A running turn discovered during resync reattaches and continues
        // rendering from buffered follow events: the follow events queued during
        // the snapshot fetch drain through the same handler after the snapshot.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-live")
        let follow = ControllableFollowStream()
        host.followStreamSource = follow
        let snapshotGate = AsyncGate()
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                follow.emit(Self.tokenEvent(turnID: "turn-live", text: "Hel"))
                follow.emit(Self.tokenEvent(turnID: "turn-live", text: "lo"))
                await snapshotGate.wait()
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        let task = orchestrator.request()
        try? await waitUntil { host.listSnapshotCount == 1 && follow.emittedCount == 2 }
        XCTAssertTrue(
            host.drainedFollowEvents.isEmpty,
            "Follow events are buffered until the snapshot is applied."
        )

        snapshotGate.open()
        follow.finish()
        await task.value

        XCTAssertEqual(
            host.drainedFollowEvents.map(\.text),
            ["Hel", "lo"],
            "Buffered follow events drain in order through the steady-state handler."
        )
        XCTAssertEqual(host.messagesSnapshotConversationIDs, ["conv-live"])
    }

    func testStaleGenerationBufferedEventsAreDropped() async {
        // A generation bump during the snapshot supersedes this resync: buffered
        // events must not be drained (the resync aborts before drain).
        let host = FakeResyncHost(generation: 2, selectedConversationID: "conv-1")
        let follow = ControllableFollowStream()
        host.followStreamSource = follow
        let snapshotGate = AsyncGate()
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                follow.emit(Self.tokenEvent(turnID: "turn-1", text: "x"))
                host.generation = 3
                await snapshotGate.wait()
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        let task = orchestrator.request()
        try? await waitUntil { host.listSnapshotCount == 1 && follow.emittedCount == 1 }
        snapshotGate.open()
        follow.finish()
        await task.value

        XCTAssertTrue(
            host.drainedFollowEvents.isEmpty,
            "A superseded resync must not drain its buffered events."
        )
        XCTAssertEqual(host.restartStreamsCount, 0)
    }

    func testBufferOverflowAbortsAndRestarts() async {
        // A tiny buffer overflows during the snapshot fetch; the resync must abort
        // and restart rather than silently drop events. On the restart the source
        // is drained (stops emitting), so it completes.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        let follow = ControllableFollowStream()
        host.followStreamSource = follow
        let firstSnapshotGate = AsyncGate()
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                // Emit more than the capacity so the buffer overflows before the
                // snapshot completes.
                for index in 0 ..< 5 {
                    follow.emit(Self.tokenEvent(turnID: "turn-1", text: "\(index)"))
                }
                await firstSnapshotGate.wait()
            }
            // Later attempts: the stream is finished, nothing to buffer.
        }
        let orchestrator = ResyncOrchestrator(host: host, bufferCapacity: 2, maxRestarts: 3)

        let task = orchestrator.request()
        try? await waitUntil { host.listSnapshotCount == 1 && follow.emittedCount == 5 }
        // Release the first snapshot; the overflow is observed and the resync
        // restarts. Finish the source so the restart's buffering sees a clean EOF.
        follow.finish()
        firstSnapshotGate.open()
        await task.value

        XCTAssertGreaterThanOrEqual(
            host.listSnapshotCount, 2,
            "Overflow must restart the resync (a second attempt runs)."
        )
        XCTAssertGreaterThanOrEqual(
            host.followEstablishCount, 2,
            "Each attempt re-establishes the follow stream."
        )
        XCTAssertGreaterThanOrEqual(host.restartStreamsCount, 1)
    }

    // MARK: - Helpers

    private static func tokenEvent(turnID: String, text: String) -> ChatStreamEvent {
        ChatStreamEvent(
            type: .text,
            turnID: turnID,
            seq: nil,
            text: text,
            toolCall: nil,
            toolCallID: nil,
            toolResult: nil,
            attachments: [],
            attachmentSource: .response,
            confirmation: nil,
            confirmationResult: nil,
            errorMessage: nil,
            status: nil
        )
    }

    private func waitUntil(
        timeout: TimeInterval = 4,
        _ predicate: @escaping @MainActor () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate() {
                return
            }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTFail("Timed out waiting for predicate")
    }
}

private enum FakeAuthError: Error {
    case rejected
}

/// A follow stream a test drives synchronously: `emit` queues an event that the
/// orchestrator's buffering task will consume, `finish` closes the stream.
@MainActor
private final class ControllableFollowStream {
    private var continuation: AsyncThrowingStream<ChatStreamEvent, Error>.Continuation?
    private var finished = false
    private(set) var emittedCount = 0

    func makeStream() -> AsyncThrowingStream<ChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            // Once finished, any re-established stream (e.g. on an overflow
            // restart) closes immediately so its buffering task doesn't hang.
            if finished {
                continuation.finish()
                return
            }
            self.continuation = continuation
        }
    }

    func emit(_ event: ChatStreamEvent) {
        emittedCount += 1
        continuation?.yield(event)
    }

    func finish() {
        finished = true
        continuation?.finish()
    }
}

/// An activity stream a test drives: `emit` queues a ping, `finish` closes it.
@MainActor
private final class ControllableActivityStream {
    private var continuation: AsyncThrowingStream<ChatConversationActivity, Error>.Continuation?
    private var finished = false
    private(set) var emittedCount = 0

    func makeStream() -> AsyncThrowingStream<ChatConversationActivity, Error> {
        AsyncThrowingStream { continuation in
            if finished {
                continuation.finish()
                return
            }
            self.continuation = continuation
        }
    }

    func emit() {
        emittedCount += 1
        continuation?.yield(
            ChatConversationActivity(conversationID: "conv-any", reason: "turn_started")
        )
    }

    func finish() {
        finished = true
        continuation?.finish()
    }
}

/// In-memory `ResyncHost` that records the resync steps, lets a test mutate
/// generation/selection mid-resync (via `onListSnapshot`) to drive the fence
/// guards, models full-replacement list convergence, and supplies controllable
/// follow/activity streams for the subscribe-then-buffer ordering tests.
@MainActor
private final class FakeResyncHost: ResyncHost {
    var generation: Int
    var selectedConversationID: String?

    var authGateError: Error?
    private(set) var awaitTerminationCount = 0
    private(set) var authGateCount = 0
    private(set) var listSnapshotCount = 0

    /// Ordered log of the resync steps, so a test can assert the old-consumer
    /// termination completes before the new follow stream is established.
    private(set) var stepLog: [String] = []
    private(set) var messagesSnapshotConversationIDs: [String] = []
    private(set) var restartStreamsCount = 0
    private(set) var phaseStartCount = 0
    private(set) var phaseFinishCount = 0
    private(set) var followEstablishCount = 0
    private(set) var activityEstablishCount = 0
    private(set) var drainedFollowEvents: [ChatStreamEvent] = []
    private(set) var activitySignalDrainCount = 0

    /// Async hook run inside `awaitStreamTermination` (e.g. to model a
    /// bounded-timeout wedged old task via a bounded sleep).
    var onAwaitTermination: (() async -> Void)?

    /// Synchronous mutation applied inside the list snapshot.
    var onListSnapshot: ((FakeResyncHost) -> Void)?
    /// Async hook run inside the list snapshot (e.g. to hold it open or emit
    /// stream events mid-fetch).
    var onListSnapshotAsync: ((FakeResyncHost) async -> Void)?

    /// Full-replacement list model: the server snapshot replaces the local set.
    var localConversationIDs: [String] = []
    var serverConversationIDs: [String] = []

    var followStreamSource: ControllableFollowStream?
    var activityStreamSource: ControllableActivityStream?

    init(generation: Int, selectedConversationID: String?) {
        self.generation = generation
        self.selectedConversationID = selectedConversationID
    }

    var resyncGeneration: Int { generation }
    var resyncSelectedConversationID: String? { selectedConversationID }

    func awaitStreamTermination() async {
        awaitTerminationCount += 1
        stepLog.append("awaitTermination")
        await onAwaitTermination?()
    }

    func gateAuthIfNeeded(generation _: Int) async throws {
        authGateCount += 1
        if let authGateError {
            throw authGateError
        }
    }

    func establishFollowStream(
        conversationID _: String,
        generation _: Int
    ) async -> AsyncThrowingStream<ChatStreamEvent, Error>? {
        followEstablishCount += 1
        stepLog.append("establishFollow")
        return followStreamSource?.makeStream()
    }

    func establishActivityStream(
        generation _: Int
    ) async -> AsyncThrowingStream<ChatConversationActivity, Error>? {
        activityEstablishCount += 1
        return activityStreamSource?.makeStream()
    }

    func applyListSnapshot() async {
        listSnapshotCount += 1
        onListSnapshot?(self)
        await onListSnapshotAsync?(self)
        localConversationIDs = serverConversationIDs
    }

    func applyMessagesSnapshot(conversationID: String) async {
        messagesSnapshotConversationIDs.append(conversationID)
    }

    func drainFollowEvent(
        _ event: ChatStreamEvent,
        conversationID _: String,
        generation _: Int
    ) async {
        drainedFollowEvents.append(event)
    }

    func drainActivitySignal(generation _: Int) async {
        activitySignalDrainCount += 1
    }

    func restartStreams() {
        restartStreamsCount += 1
    }

    func resyncPhaseDidStart() {
        phaseStartCount += 1
    }

    func resyncPhaseDidFinish() {
        phaseFinishCount += 1
    }
}

/// A one-shot gate a test opens to release a held async step.
private final class AsyncGate: @unchecked Sendable {
    private let lock = NSLock()
    private var opened = false
    private var continuations: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if opened {
                lock.unlock()
                continuation.resume()
                return
            }
            continuations.append(continuation)
            lock.unlock()
        }
    }

    func open() {
        lock.lock()
        opened = true
        let pending = continuations
        continuations.removeAll()
        lock.unlock()
        for continuation in pending {
            continuation.resume()
        }
    }
}
