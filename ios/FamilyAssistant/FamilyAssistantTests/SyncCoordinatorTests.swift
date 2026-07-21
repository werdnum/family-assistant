import XCTest

@testable import FamilyAssistant

@MainActor
final class SyncCoordinatorTests: XCTestCase {
    private func makeCoordinator(satisfied: Bool = true) -> (SyncCoordinator, StubPathMonitor) {
        let monitor = StubPathMonitor(isSatisfied: satisfied)
        let coordinator = SyncCoordinator(pathMonitor: monitor)
        return (coordinator, monitor)
    }

    func testInitStartsPathMonitorAndSeedsReachability() {
        let (coordinator, monitor) = makeCoordinator(satisfied: true)

        XCTAssertEqual(monitor.startCount, 1)
        XCTAssertEqual(coordinator.reachability, .satisfied)
        XCTAssertEqual(coordinator.presentation, .degraded)
    }

    func testInitLeavesReachabilityUnknownWhenPathUnsatisfied() {
        let (coordinator, _) = makeCoordinator(satisfied: false)

        XCTAssertEqual(coordinator.reachability, .unknown)
    }

    func testChannelConnectionTraceReachesLive() {
        let (coordinator, _) = makeCoordinator()
        let generation = coordinator.generation

        XCTAssertTrue(coordinator.apply(.followConnected(generation: generation)).isEmpty)
        XCTAssertEqual(coordinator.presentation, .degraded)

        XCTAssertTrue(coordinator.apply(.activityConnected(generation: generation)).isEmpty)
        XCTAssertEqual(coordinator.followHealth, .connected)
        XCTAssertEqual(coordinator.activityHealth, .connected)
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testCleanEOFFollowDropLeavesDegraded() {
        let (coordinator, _) = makeCoordinator()
        let generation = coordinator.generation
        coordinator.apply(.followConnected(generation: generation))
        coordinator.apply(.activityConnected(generation: generation))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.apply(.followDropped(generation: generation, cleanEOF: true))

        XCTAssertEqual(coordinator.followHealth, .down)
        XCTAssertEqual(coordinator.presentation, .degraded)
    }

    func testSyncPhaseTakesPrecedenceOverChannelHealth() {
        let (coordinator, _) = makeCoordinator()
        let generation = coordinator.generation
        coordinator.apply(.followConnected(generation: generation))
        coordinator.apply(.activityConnected(generation: generation))

        coordinator.apply(.syncStarted)
        XCTAssertEqual(coordinator.presentation, .syncing)

        coordinator.apply(.syncFinished)
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testStaleGenerationEventsAreRejected() {
        let (coordinator, _) = makeCoordinator()
        let staleGeneration = coordinator.generation
        coordinator.apply(.followConnected(generation: staleGeneration))
        XCTAssertEqual(coordinator.followHealth, .connected)

        let newGeneration = coordinator.bumpGeneration()
        XCTAssertNotEqual(staleGeneration, newGeneration)

        let effects = coordinator.apply(.followDropped(generation: staleGeneration, cleanEOF: false))

        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.followHealth, .connected)
    }

    func testStaleGenerationConnectIsRejected() {
        let (coordinator, _) = makeCoordinator()
        let staleGeneration = coordinator.generation
        coordinator.bumpGeneration()

        let effects = coordinator.apply(.activityConnected(generation: staleGeneration))

        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.activityHealth, .down)
    }

    func testPresentationDerivationPriorityTable() {
        let (coordinator, _) = makeCoordinator()
        let generation = coordinator.generation
        coordinator.apply(.followConnected(generation: generation))
        coordinator.apply(.activityConnected(generation: generation))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.apply(.syncStarted)
        XCTAssertEqual(coordinator.presentation, .syncing)

        coordinator.apply(.reachabilityChanged(.unsatisfied))
        XCTAssertEqual(coordinator.presentation, .offline)

        coordinator.apply(.authRequired)
        XCTAssertEqual(coordinator.presentation, .authRequired)

        coordinator.apply(.backgrounded)
        XCTAssertEqual(coordinator.presentation, .suspended)
    }

    func testDegradedWhenOnlyOneChannelConnected() {
        let (coordinator, _) = makeCoordinator()
        let generation = coordinator.generation

        coordinator.apply(.followConnected(generation: generation))

        XCTAssertEqual(coordinator.presentation, .degraded)
    }

    func testLatchedForegroundEmitsExactlyOneResync() {
        let (coordinator, _) = makeCoordinator()
        let startingGeneration = coordinator.generation

        coordinator.scenePhaseChanged(didBackground: true, isActive: false)
        XCTAssertEqual(coordinator.lifecycle, .background)
        XCTAssertTrue(coordinator.cameFromBackground)
        XCTAssertEqual(coordinator.presentation, .suspended)

        let effects = coordinator.apply(.foregrounded)

        XCTAssertEqual(effects, [.runResync(generation: startingGeneration + 1)])
        XCTAssertEqual(coordinator.generation, startingGeneration + 1)
        XCTAssertFalse(coordinator.cameFromBackground)
        XCTAssertEqual(coordinator.lifecycle, .foreground)
    }

    func testRealResumeSceneSequenceTriggersExactlyOneResync() {
        let (coordinator, _) = makeCoordinator()
        let startingGeneration = coordinator.generation

        coordinator.scenePhaseChanged(didBackground: true, isActive: false)
        coordinator.scenePhaseChanged(didBackground: false, isActive: false)
        coordinator.scenePhaseChanged(didBackground: false, isActive: true)

        XCTAssertEqual(coordinator.generation, startingGeneration + 1)
        XCTAssertFalse(coordinator.cameFromBackground)

        let repeated = coordinator.apply(.foregrounded)
        XCTAssertTrue(repeated.isEmpty)
    }

    func testInactiveBlipWithoutBackgroundEmitsNothing() {
        let (coordinator, _) = makeCoordinator()

        coordinator.scenePhaseChanged(didBackground: false, isActive: false)
        let effects = coordinator.apply(.foregrounded)

        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.generation, 0)
        XCTAssertFalse(coordinator.cameFromBackground)
    }

    func testRepeatForegroundAfterResyncEmitsNothing() {
        let (coordinator, _) = makeCoordinator()
        coordinator.apply(.backgrounded)
        let firstResync = coordinator.apply(.foregrounded)
        XCTAssertEqual(firstResync.count, 1)

        let secondForeground = coordinator.apply(.foregrounded)

        XCTAssertTrue(secondForeground.isEmpty)
    }

    func testReachabilityUnsatisfiedShowsOfflineImmediatelyWithoutEffects() {
        let (coordinator, monitor) = makeCoordinator(satisfied: true)

        monitor.setSatisfied(false)

        XCTAssertEqual(coordinator.reachability, .unsatisfied)
        XCTAssertEqual(coordinator.presentation, .offline)
    }

    func testReachabilitySatisfiedIsHintOnlyAndEmitsNoEffects() {
        let (coordinator, _) = makeCoordinator()

        let effects = coordinator.apply(.reachabilityChanged(.satisfied))

        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.reachability, .satisfied)
    }

    func testDegradedAndOfflineAreDistinctPresentationStates() {
        // The toolbar indicator renders distinct affordances per presentation, so
        // degraded (a live-but-unhealthy channel) must never collapse into offline
        // (no network). Guards the view-model-layer contract the indicator maps to
        // separate symbols/labels/identifiers.
        let (coordinator, _) = makeCoordinator(satisfied: true)

        coordinator.apply(.followConnected(generation: coordinator.generation))
        XCTAssertEqual(coordinator.presentation, .degraded)

        coordinator.apply(.reachabilityChanged(.unsatisfied))
        XCTAssertEqual(coordinator.presentation, .offline)
        XCTAssertNotEqual(SyncCoordinator.Presentation.degraded, coordinator.presentation)
    }

    func testFirstUnsatisfiedObservationLeavesUnknownForReachability() {
        // Launching offline: NWPathMonitor's first callback reports `.unsatisfied`,
        // whose value equals the coordinator's initial (unknown-seeded) state. The
        // path monitor must still deliver it so the coordinator leaves `.unknown`
        // and shows offline, rather than being pinned at `.unknown` forever.
        let (coordinator, monitor) = makeCoordinator(satisfied: false)
        XCTAssertEqual(coordinator.reachability, .unknown)

        monitor.setSatisfied(false)

        XCTAssertEqual(coordinator.reachability, .unsatisfied)
        XCTAssertEqual(coordinator.presentation, .offline)
    }

    func testStubPathMonitorDeliversFirstObservationEvenWhenSameValue() {
        // Contract parity with the production `NetworkPathMonitor`: the first
        // observation is always delivered; later same-value observations coalesce.
        let monitor = StubPathMonitor(isSatisfied: false)
        var delivered: [Bool] = []
        monitor.onChange = { delivered.append($0) }

        monitor.setSatisfied(false)
        monitor.setSatisfied(false)
        monitor.setSatisfied(true)

        XCTAssertEqual(delivered, [false, true])
    }

    func testReachabilityRecoveryWakesReconnectLoopImmediately() async throws {
        // A follow loop asleep at backoff after an unsatisfied path must not wait
        // out the capped delay when the path recovers: the unsatisfied→satisfied
        // transition resets backoff and retries now.
        let monitor = StubPathMonitor(isSatisfied: true)
        let coordinator = SyncCoordinator(
            pathMonitor: monitor,
            followReconnectInitialDelaySeconds: 60,
            followReconnectMaxDelaySeconds: 60
        )
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate

        // Bring a follow loop up so it owns a conversation, then let it drop into a
        // long backoff sleep by failing the next connect.
        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }
        delegate.shouldFailFollowOpen = true
        monitor.setSatisfied(false)
        monitor.setSatisfied(true)

        // The wake restarts the loop, re-entering openFollowStream well within the
        // 60s backoff that would otherwise gate the retry.
        try await waitUntil(timeout: 3) { delegate.followOpenCount >= 2 }
    }

    func testAuthRequiredOverridesReachabilityAndSync() {
        let (coordinator, _) = makeCoordinator()

        coordinator.apply(.authRefreshing)
        XCTAssertEqual(coordinator.authState, .refreshing)

        coordinator.apply(.authRequired)
        coordinator.apply(.reachabilityChanged(.unsatisfied))
        coordinator.apply(.syncStarted)

        XCTAssertEqual(coordinator.presentation, .authRequired)

        coordinator.apply(.authOK)
        XCTAssertEqual(coordinator.presentation, .offline)
    }

    private func waitUntil(
        timeout: TimeInterval = 2,
        _ predicate: @escaping () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate() {
                return
            }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail("Timed out waiting for predicate")
    }
}

/// Minimal `SyncStreamDelegate` that records how often the coordinator's owned
/// loops invoke it and can be told to fail the next follow connect, so a test can
/// drive a loop into backoff and assert the reachability-recovery wake retries.
@MainActor
private final class RecordingSyncStreamDelegate: SyncStreamDelegate {
    private(set) var followOpenCount = 0
    private(set) var activityOpenCount = 0
    var shouldFailFollowOpen = false

    struct StubError: Error {}

    func openFollowStream(
        conversationID: String,
        generation: Int
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        followOpenCount += 1
        if shouldFailFollowOpen {
            throw StubError()
        }
        return AsyncThrowingStream { continuation in
            continuation.finish()
        }
    }

    func followStreamDidConnect(conversationID: String, generation: Int) async {}

    func handleFollowEvent(
        _ event: ChatStreamEvent,
        conversationID: String,
        generation: Int
    ) async -> Bool { true }

    func followBufferRotated(generation: Int) {}

    func reportFollowStreamDrop(conversationID: String, error: Error?, generation: Int) {}

    func shouldSurfaceFollowDrop() -> Bool { true }

    func catchUpFollowHistory(conversationID: String, generation: Int) async {}

    func openActivityStream(
        generation: Int
    ) async throws -> AsyncThrowingStream<ChatConversationActivity, Error> {
        activityOpenCount += 1
        throw StubError()
    }

    func activityStreamDidSignal(generation: Int) async {}
}
