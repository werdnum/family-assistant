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
}
