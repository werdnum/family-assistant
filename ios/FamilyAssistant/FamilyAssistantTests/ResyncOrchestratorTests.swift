import XCTest

@testable import FamilyAssistant

@MainActor
final class ResyncOrchestratorTests: XCTestCase {
    func testSnapshotsAppliedWhenGenerationAndSelectionMatch() async {
        let host = FakeResyncHost(generation: 3, selectedConversationID: "conv-1")
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.authGateCount, 1)
        XCTAssertEqual(host.listSnapshotCount, 1)
        XCTAssertEqual(host.messagesSnapshotConversationIDs, ["conv-1"])
        XCTAssertEqual(host.restartStreamsCount, 1)
        XCTAssertEqual(host.phaseStartCount, 1)
        XCTAssertEqual(host.phaseFinishCount, 1)
    }

    func testConversationSwitchMidResyncAbortsMessageApply() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-a")
        // A switch to another conversation lands during the list snapshot, before
        // the message snapshot is applied.
        host.onListSnapshot = { $0.selectedConversationID = "conv-b" }
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.listSnapshotCount, 1)
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
        host.onListSnapshot = { $0.generation = 6 }
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
        // Hold the list snapshot open so the resync cannot complete before the
        // second request is issued; the second must join the in-flight task.
        let gate = AsyncGate()
        host.onListSnapshotAsync = { _ in await gate.wait() }
        let orchestrator = ResyncOrchestrator(host: host)

        // `request()` records the in-flight task synchronously, so a second call
        // issued before the first task's body finishes joins it rather than
        // starting a second run.
        let first = orchestrator.request()
        let second = orchestrator.request()

        gate.open()
        await first.value
        await second.value

        XCTAssertEqual(host.listSnapshotCount, 1, "Coalesced requests fetch the list exactly once.")
        XCTAssertEqual(host.authGateCount, 1, "The joined request does no duplicate work.")
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
}

private enum FakeAuthError: Error {
    case rejected
}

/// In-memory `ResyncHost` that records the resync steps and lets a test mutate
/// generation/selection mid-resync (via `onListSnapshot`) to drive the fence
/// guards, and model full-replacement list convergence.
@MainActor
private final class FakeResyncHost: ResyncHost {
    var generation: Int
    var selectedConversationID: String?

    var authGateError: Error?
    private(set) var authGateCount = 0
    private(set) var listSnapshotCount = 0
    private(set) var messagesSnapshotConversationIDs: [String] = []
    private(set) var restartStreamsCount = 0
    private(set) var phaseStartCount = 0
    private(set) var phaseFinishCount = 0

    /// Synchronous mutation applied at the top of the list snapshot, before the
    /// post-snapshot fence re-reads generation/selection.
    var onListSnapshot: ((FakeResyncHost) -> Void)?
    /// Async hook run inside the list snapshot (e.g. to hold it open for a
    /// coalescing test).
    var onListSnapshotAsync: ((FakeResyncHost) async -> Void)?

    /// Full-replacement list model: the server snapshot replaces the local set.
    var localConversationIDs: [String] = []
    var serverConversationIDs: [String] = []

    init(generation: Int, selectedConversationID: String?) {
        self.generation = generation
        self.selectedConversationID = selectedConversationID
    }

    var resyncGeneration: Int { generation }
    var resyncSelectedConversationID: String? { selectedConversationID }

    func gateAuthIfNeeded(generation _: Int) async throws {
        authGateCount += 1
        if let authGateError {
            throw authGateError
        }
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
