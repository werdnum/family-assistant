import XCTest

@testable import FamilyAssistant

@MainActor
final class SyncCoordinatorTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"

    override func setUp() {
        super.setUp()
        resetStoredAuth()
        URLProtocol.registerClass(ChatMockBackendURLProtocol.self)
        ChatMockBackendURLProtocol.reset()
    }

    override func tearDown() {
        ChatMockBackendURLProtocol.reset()
        URLProtocol.unregisterClass(ChatMockBackendURLProtocol.self)
        resetStoredAuth()
        super.tearDown()
    }

    private func makeCoordinator(
        satisfied: Bool = true,
        authManager: AuthManager? = nil
    ) -> (SyncCoordinator, StubPathMonitor, AuthManager) {
        let monitor = StubPathMonitor(isSatisfied: satisfied)
        let auth = authManager ?? AuthManager()
        let coordinator = SyncCoordinator(
            authManager: auth,
            pathMonitor: monitor
        )
        return (coordinator, monitor, auth)
    }

    func testInitStartsPathMonitorAndSeedsReachability() {
        let (coordinator, monitor, _) = makeCoordinator(satisfied: true)

        XCTAssertEqual(monitor.startCount, 1)
        XCTAssertEqual(coordinator.reachability, .satisfied)
        XCTAssertEqual(coordinator.presentation, .degraded)
    }

    func testInitLeavesReachabilityUnknownWhenPathUnsatisfied() {
        let (coordinator, _, _) = makeCoordinator(satisfied: false)

        XCTAssertEqual(coordinator.reachability, .unknown)
    }

    func testChannelConnectionTraceReachesLive() {
        let (coordinator, _, _) = makeCoordinator()

        XCTAssertTrue(
            coordinator.apply(.followConnected(generation: coordinator.followGeneration)).isEmpty
        )
        XCTAssertEqual(coordinator.presentation, .degraded)

        XCTAssertTrue(
            coordinator.apply(.activityConnected(generation: coordinator.activityGeneration)).isEmpty
        )
        XCTAssertEqual(coordinator.followHealth, .connected)
        XCTAssertEqual(coordinator.activityHealth, .connected)
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testCleanEOFFollowDropLeavesDegraded() {
        let (coordinator, _, _) = makeCoordinator()
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.apply(.followDropped(generation: coordinator.followGeneration, cleanEOF: true))

        XCTAssertEqual(coordinator.followHealth, .down)
        XCTAssertEqual(coordinator.presentation, .degraded)
    }

    func testSyncPhaseTakesPrecedenceOverChannelHealth() {
        let (coordinator, _, _) = makeCoordinator()
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))

        coordinator.apply(.syncStarted)
        XCTAssertEqual(coordinator.presentation, .syncing)

        coordinator.apply(.syncFinished)
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testStaleGenerationEventsAreRejected() {
        let (coordinator, _, _) = makeCoordinator()
        let staleGeneration = coordinator.followGeneration
        coordinator.apply(.followConnected(generation: staleGeneration))
        XCTAssertEqual(coordinator.followHealth, .connected)

        let newGeneration = coordinator.bumpFollowGeneration()
        XCTAssertNotEqual(staleGeneration, newGeneration)

        let effects = coordinator.apply(.followDropped(generation: staleGeneration, cleanEOF: false))

        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.followHealth, .connected)
    }

    func testStaleGenerationConnectIsRejected() {
        let (coordinator, _, _) = makeCoordinator()
        let staleGeneration = coordinator.activityGeneration
        coordinator.bumpActivityGeneration()

        let effects = coordinator.apply(.activityConnected(generation: staleGeneration))

        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.activityHealth, .down)
    }

    func testReplacingFollowStreamBumpsGenerationSoStaleEventsAreRejected() async throws {
        // Replacing a live follow task (manual reconnect / wake / conversation
        // switch) must run under a fresh follow generation: the cancelled task's
        // in-flight delegate callbacks would otherwise still pass the generation
        // check and merge/ack stale events into the new stream's state.
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }
        let replacedGeneration = coordinator.followGeneration
        coordinator.apply(.followConnected(generation: replacedGeneration))
        XCTAssertEqual(coordinator.followHealth, .connected)

        // Restart the stream (replacement). The follow generation must advance so
        // the replaced task's generation no longer owns the follow channel, and the
        // channel demotes to `.reconnecting` until the replacement really connects.
        coordinator.startFollowStream(conversationID: "conv-1")
        XCTAssertNotEqual(coordinator.followGeneration, replacedGeneration)
        XCTAssertEqual(coordinator.followHealth, .reconnecting)

        // A late drop from the replaced task's generation is rejected: it neither
        // changes health nor produces effects.
        let effects = coordinator.apply(
            .followDropped(generation: replacedGeneration, cleanEOF: false)
        )
        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.followHealth, .reconnecting)
    }

    func testReplacingFollowStreamLeavesActivityGenerationUntouched() async throws {
        // The two channels are counted separately: replacing the follow stream must
        // NOT invalidate a concurrent, still-valid activity connect. A shared
        // counter would reject the activity connect the moment follow bumped.
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        delegate.hangActivityOpen = true
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        try await waitUntil { delegate.activityOpenCount >= 1 }
        let activityGenerationBeforeSwitch = coordinator.activityGeneration

        // Switch conversations: only the follow stream is replaced.
        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }

        // The activity connect (captured before the follow switch) is still current.
        XCTAssertEqual(coordinator.activityGeneration, activityGenerationBeforeSwitch)
        coordinator.apply(.activityConnected(generation: activityGenerationBeforeSwitch))
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testReplacingActivityStreamBumpsGenerationSoStaleEventsAreRejected() async throws {
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangActivityOpen = true
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        try await waitUntil { delegate.activityOpenCount >= 1 }
        let replacedGeneration = coordinator.activityGeneration
        coordinator.apply(.activityConnected(generation: replacedGeneration))
        XCTAssertEqual(coordinator.activityHealth, .connected)

        coordinator.startActivityStream()
        XCTAssertNotEqual(coordinator.activityGeneration, replacedGeneration)
        XCTAssertEqual(coordinator.activityHealth, .reconnecting)

        let effects = coordinator.apply(
            .activityDropped(generation: replacedGeneration, cleanEOF: false)
        )
        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.activityHealth, .reconnecting)
    }

    func testPresentationDerivationPriorityTable() {
        let (coordinator, _, _) = makeCoordinator()
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
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
        let (coordinator, _, _) = makeCoordinator()

        coordinator.apply(.followConnected(generation: coordinator.followGeneration))

        XCTAssertEqual(coordinator.presentation, .degraded)
    }

    func testEmptyLaunchDraftIsLiveWhenActivityStreamIsConnected() {
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate

        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))

        XCTAssertNil(delegate.currentConversationID())
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testLatchedForegroundEmitsExactlyOneResync() {
        let (coordinator, _, _) = makeCoordinator()
        let followGenerationBeforeBackground = coordinator.followGeneration
        let activityGenerationBeforeBackground = coordinator.activityGeneration

        let backgroundEffects = coordinator.scenePhaseChanged(didBackground: true, isActive: false)
        XCTAssertEqual(coordinator.lifecycle, .background)
        XCTAssertTrue(coordinator.cameFromBackground)
        XCTAssertEqual(coordinator.presentation, .suspended)
        // Background bumps BOTH channel generations (fencing the torn-down streams'
        // late events) and asks for a send suspend + stream teardown (design 4.3).
        XCTAssertEqual(backgroundEffects, [.suspendSend, .cancelStreams])
        XCTAssertGreaterThan(coordinator.followGeneration, followGenerationBeforeBackground)
        XCTAssertGreaterThan(coordinator.activityGeneration, activityGenerationBeforeBackground)

        let effects = coordinator.apply(.foregrounded)

        XCTAssertEqual(effects, [.runResync])
        XCTAssertFalse(coordinator.cameFromBackground)
        XCTAssertEqual(coordinator.lifecycle, .foreground)
    }

    func testRealResumeSceneSequenceTriggersExactlyOneResync() {
        let (coordinator, _, _) = makeCoordinator()

        var allEffects: [SyncCoordinator.SyncEffect] = []
        allEffects += coordinator.scenePhaseChanged(didBackground: true, isActive: false)
        allEffects += coordinator.scenePhaseChanged(didBackground: false, isActive: false)
        allEffects += coordinator.scenePhaseChanged(didBackground: false, isActive: true)

        // A full resume emits the background suspend + teardown, then exactly one
        // latched-foreground resync.
        XCTAssertEqual(allEffects, [.suspendSend, .cancelStreams, .runResync])
        XCTAssertFalse(coordinator.cameFromBackground)

        let repeated = coordinator.apply(.foregrounded)
        XCTAssertTrue(repeated.isEmpty)
    }

    func testInactiveBlipWithoutBackgroundEmitsNothing() {
        let (coordinator, _, _) = makeCoordinator()

        coordinator.scenePhaseChanged(didBackground: false, isActive: false)
        let effects = coordinator.apply(.foregrounded)

        XCTAssertTrue(effects.isEmpty)
        XCTAssertFalse(coordinator.cameFromBackground)
    }

    func testRepeatForegroundAfterResyncEmitsNothing() {
        let (coordinator, _, _) = makeCoordinator()
        coordinator.apply(.backgrounded)
        let firstResync = coordinator.apply(.foregrounded)
        XCTAssertEqual(firstResync.count, 1)

        let secondForeground = coordinator.apply(.foregrounded)

        XCTAssertTrue(secondForeground.isEmpty)
    }

    func testReachabilityUnsatisfiedShowsOfflineImmediatelyWithoutEffects() {
        let (coordinator, monitor, _) = makeCoordinator(satisfied: true)

        monitor.setSatisfied(false)

        XCTAssertEqual(coordinator.reachability, .unsatisfied)
        XCTAssertEqual(coordinator.presentation, .offline)
    }

    func testReachabilitySatisfiedIsHintOnlyAndEmitsNoEffects() {
        let (coordinator, _, _) = makeCoordinator()

        let effects = coordinator.apply(.reachabilityChanged(.satisfied))

        XCTAssertTrue(effects.isEmpty)
        XCTAssertEqual(coordinator.reachability, .satisfied)
    }

    func testDegradedAndOfflineAreDistinctPresentationStates() {
        // The toolbar indicator renders distinct affordances per presentation, so
        // degraded (a live-but-unhealthy channel) must never collapse into offline
        // (no network). Guards the view-model-layer contract the indicator maps to
        // separate symbols/labels/identifiers.
        let (coordinator, _, _) = makeCoordinator(satisfied: true)

        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
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
        let (coordinator, monitor, _) = makeCoordinator(satisfied: false)
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
            authManager: AuthManager(),
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
        try await waitUntil { delegate.followOpenCount >= 2 }
    }

    func testUnsatisfiedMarksConnectedChannelsDownSoRecoveryRestartsBoth() async throws {
        // Both channels are live when the path drops. The SSE tasks have not yet
        // seen their sockets die, so without marking health down here both healths
        // would stay `.connected`; the recovery wake (which only restarts
        // non-connected channels) would then skip both loops and presentation would
        // flip back to `.live` over dead sockets. Marking both down on `.unsatisfied`
        // makes the following `.satisfied` wake restart both loops.
        let monitor = StubPathMonitor(isSatisfied: true)
        let coordinator = SyncCoordinator(
            authManager: AuthManager(),
            pathMonitor: monitor,
            followReconnectInitialDelaySeconds: 60,
            followReconnectMaxDelaySeconds: 60
        )
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate

        // Bring the follow loop up and mark both channels connected (the live state
        // just before the path drops). The activity loop's open always throws, so
        // its health is asserted via the coordinator event, not a real connect.
        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertEqual(coordinator.presentation, .live)
        let followOpensBeforeDrop = delegate.followOpenCount

        // Path drops: both healths must go down even though no drop event fired.
        coordinator.apply(.reachabilityChanged(.unsatisfied))
        XCTAssertEqual(coordinator.followHealth, .down)
        XCTAssertEqual(coordinator.activityHealth, .down)
        XCTAssertEqual(coordinator.presentation, .offline)

        // Fail the next connects so the restarted loops stay down deterministically;
        // the point under test is that recovery *restarts* both loops (re-invokes
        // openFollow/openActivity), not that they reconnect. Health only ever becomes
        // connected again via a real follow/activity connect.
        delegate.shouldFailFollowOpen = true

        coordinator.apply(.reachabilityChanged(.satisfied))
        try await waitUntil {
            delegate.followOpenCount > followOpensBeforeDrop && delegate.activityOpenCount >= 1
        }
        XCTAssertEqual(coordinator.followHealth, .down)
        XCTAssertEqual(coordinator.activityHealth, .down)
    }

    func testAuthRequiredOverridesReachabilityAndSync() {
        let (coordinator, _, _) = makeCoordinator()

        coordinator.apply(.authRefreshing)
        XCTAssertEqual(coordinator.authState, .refreshing)

        coordinator.apply(.authRequired)
        coordinator.apply(.reachabilityChanged(.unsatisfied))
        coordinator.apply(.syncStarted)

        XCTAssertEqual(coordinator.presentation, .authRequired)

        coordinator.apply(.authOK)
        XCTAssertEqual(coordinator.presentation, .offline)
    }

    func testReauthSuccessWakesReconnectLoopsImmediately() async throws {
        // While `authRequired` is latched the streams cannot connect, so their
        // loops sit at capped backoff. On the authRequired→ok transition (re-auth
        // success) the loops must be woken immediately instead of waiting out the
        // (here 60s) backoff, mirroring the reachability-recovery wake.
        let monitor = StubPathMonitor(isSatisfied: true)
        let coordinator = SyncCoordinator(
            authManager: AuthManager(),
            pathMonitor: monitor,
            followReconnectInitialDelaySeconds: 60,
            followReconnectMaxDelaySeconds: 60
        )
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate

        // Bring both loops up (activity's open always throws, so it drops into
        // backoff), then fail every connect so they stay down deterministically.
        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }
        delegate.shouldFailFollowOpen = true
        coordinator.startActivityStream()
        try await waitUntil { delegate.activityOpenCount >= 1 }
        let followOpensBeforeReauth = delegate.followOpenCount
        let activityOpensBeforeReauth = delegate.activityOpenCount

        coordinator.apply(.authRequired)
        XCTAssertEqual(coordinator.authState, .authRequired)

        coordinator.apply(.authOK)
        try await waitUntil {
            delegate.followOpenCount > followOpensBeforeReauth
                && delegate.activityOpenCount > activityOpensBeforeReauth
        }
    }

    func testAuthRequiredCancelsConnectedStreamsAndMarksHealthsDown() async throws {
        // Finding 1: when authRequired latches while both channels read connected,
        // the old-session sockets keep applying events (the backend authorizes once
        // at connect) and a later authOK would skip the restart because health is
        // still `.connected`. The transition must cancel the streams and mark both
        // channels down so the authOK wake reconnects under fresh credentials.
        let monitor = StubPathMonitor(isSatisfied: true)
        let coordinator = SyncCoordinator(
            authManager: AuthManager(),
            pathMonitor: monitor,
            followReconnectInitialDelaySeconds: 60,
            followReconnectMaxDelaySeconds: 60
        )
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        delegate.hangActivityOpen = true
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        coordinator.startActivityStream()
        try await waitUntil { delegate.followOpenCount >= 1 && delegate.activityOpenCount >= 1 }
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertEqual(coordinator.presentation, .live)
        let followOpensBeforeAuthRequired = delegate.followOpenCount
        let activityOpensBeforeAuthRequired = delegate.activityOpenCount

        coordinator.apply(.authRequired)
        XCTAssertEqual(coordinator.followHealth, .down)
        XCTAssertEqual(coordinator.activityHealth, .down)
        XCTAssertEqual(coordinator.presentation, .authRequired)

        // authOK's wake now restarts both loops because their healths are down.
        delegate.hangFollowOpen = false
        delegate.hangActivityOpen = false
        coordinator.apply(.authOK)
        try await waitUntil {
            delegate.followOpenCount > followOpensBeforeAuthRequired
                && delegate.activityOpenCount > activityOpensBeforeAuthRequired
        }
    }

    func testCancelFollowStreamBumpsGenerationSoInFlightCallbacksAreFenced() async throws {
        // Finding 2: a conversation switch cancels the follow stream, then awaits
        // loadMessages before restarting it. Cancelling must bump the follow
        // generation immediately so the cancelled task's in-flight callbacks (which
        // still race the switch window) no longer pass isCurrentFollow.
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }
        let cancelledGeneration = coordinator.followGeneration

        coordinator.cancelFollowStream()
        XCTAssertNotEqual(coordinator.followGeneration, cancelledGeneration)
        XCTAssertFalse(coordinator.isCurrentFollow(cancelledGeneration))
    }

    func testReplacingFollowStreamDemotesConnectedHealthToReconnecting() async throws {
        // Finding 3: replacing a live follow task must not leave followHealth
        // `.connected` until the new task connects — presentation would otherwise
        // claim `.live` with no connected follow stream. The health drops to
        // `.reconnecting` on replace and is promoted back only by a real connect.
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.startFollowStream(conversationID: "conv-1")
        XCTAssertEqual(coordinator.followHealth, .reconnecting)
        XCTAssertNotEqual(coordinator.presentation, .live)

        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        XCTAssertEqual(coordinator.followHealth, .connected)
    }

    func testCancelFollowStreamDemotesConnectedHealthToReconnecting() async throws {
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        XCTAssertEqual(coordinator.followHealth, .connected)

        coordinator.cancelFollowStream()
        XCTAssertEqual(coordinator.followHealth, .reconnecting)
    }

    func testFollowConnect401WithRejectedRefreshStopsAndLatchesAuthRequired() async {
        seedStoredAuth()
        let monitor = StubPathMonitor(isSatisfied: true)
        let authManager = makeAuthManager()
        let authRequired = expectation(description: "authRequired latched")
        authManager.addAuthStateObserver { signal in
            if case .authRequired = signal {
                authRequired.fulfill()
            }
        }
        let refreshRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/auth/refresh")
            _ = refreshRequests.increment()
            return .json(#"{"detail":"rejected"}"#, statusCode: 401)
        }
        var delayCount = 0
        let coordinator = SyncCoordinator(
            authManager: authManager,
            pathMonitor: monitor,
            followReconnectInitialDelaySeconds: 60,
            followReconnectMaxDelaySeconds: 60,
            reconnectDelay: { _ in delayCount += 1 }
        )
        let delegate = RecordingSyncStreamDelegate()
        delegate.followOpenError = ChatAPIError.server(statusCode: 401, detail: nil)
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        await fulfillment(of: [authRequired], timeout: 2)
        await Task.yield()

        XCTAssertEqual(delegate.followOpenCount, 1)
        XCTAssertEqual(refreshRequests.value, 1)
        XCTAssertEqual(delayCount, 0, "terminal auth must stop instead of entering backoff")
        XCTAssertTrue(authManager.authRequired)
        XCTAssertNil(KeychainHelper.readString(key: "fa_api_token"))
    }

    func testActivityConnect403WithRejectedRefreshStopsAndLatchesAuthRequired() async {
        seedStoredAuth()
        let monitor = StubPathMonitor(isSatisfied: true)
        let authManager = makeAuthManager()
        let authRequired = expectation(description: "authRequired latched")
        authManager.addAuthStateObserver { signal in
            if case .authRequired = signal {
                authRequired.fulfill()
            }
        }
        let refreshRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/auth/refresh")
            _ = refreshRequests.increment()
            return .json(#"{"detail":"rejected"}"#, statusCode: 403)
        }
        var delayCount = 0
        let coordinator = SyncCoordinator(
            authManager: authManager,
            pathMonitor: monitor,
            followReconnectInitialDelaySeconds: 60,
            followReconnectMaxDelaySeconds: 60,
            reconnectDelay: { _ in delayCount += 1 }
        )
        let delegate = RecordingSyncStreamDelegate()
        delegate.activityOpenError = ChatAPIError.server(statusCode: 403, detail: nil)
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        await fulfillment(of: [authRequired], timeout: 2)
        await Task.yield()

        XCTAssertEqual(delegate.activityOpenCount, 1)
        XCTAssertEqual(refreshRequests.value, 1)
        XCTAssertEqual(delayCount, 0, "terminal auth must stop instead of entering backoff")
        XCTAssertTrue(authManager.authRequired)
        XCTAssertEqual(coordinator.activityHealth, .down)
    }

    func testFollowConnect401WithSuccessfulRefreshReconnectsImmediately() async {
        seedStoredAuth()
        let authManager = makeAuthManager()
        let refreshRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/auth/refresh")
            _ = refreshRequests.increment()
            return .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
        }
        var delayCount = 0
        let coordinator = SyncCoordinator(
            authManager: authManager,
            pathMonitor: StubPathMonitor(isSatisfied: true),
            followReconnectInitialDelaySeconds: 60,
            followReconnectMaxDelaySeconds: 60,
            reconnectDelay: { _ in delayCount += 1 }
        )
        let delegate = RecordingSyncStreamDelegate()
        delegate.followOpenError = ChatAPIError.server(statusCode: 401, detail: nil)
        delegate.followOpenErrorLimit = 1
        delegate.hangFollowOpen = true
        let reconnected = expectation(description: "follow stream reopened")
        reconnected.expectedFulfillmentCount = 2
        delegate.onFollowOpen = { reconnected.fulfill() }
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        await fulfillment(of: [reconnected], timeout: 2)

        XCTAssertEqual(refreshRequests.value, 1)
        XCTAssertEqual(delayCount, 0, "successful refresh reconnects without transport backoff")
        XCTAssertFalse(authManager.authRequired)
        XCTAssertEqual(coordinator.followHealth, .connected)
    }

    func testActivityConnect401WithSuccessfulRefreshReconnectsImmediately() async {
        seedStoredAuth()
        let authManager = makeAuthManager()
        let refreshRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/auth/refresh")
            _ = refreshRequests.increment()
            return .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
        }
        var delayCount = 0
        let coordinator = SyncCoordinator(
            authManager: authManager,
            pathMonitor: StubPathMonitor(isSatisfied: true),
            followReconnectInitialDelaySeconds: 60,
            followReconnectMaxDelaySeconds: 60,
            reconnectDelay: { _ in delayCount += 1 }
        )
        let delegate = RecordingSyncStreamDelegate()
        delegate.activityOpenError = ChatAPIError.server(statusCode: 401, detail: nil)
        delegate.activityOpenErrorLimit = 1
        delegate.hangActivityOpen = true
        let reconnected = expectation(description: "activity stream reopened")
        reconnected.expectedFulfillmentCount = 2
        delegate.onActivityOpen = { reconnected.fulfill() }
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        await fulfillment(of: [reconnected], timeout: 2)

        XCTAssertEqual(refreshRequests.value, 1)
        XCTAssertEqual(delayCount, 0, "successful refresh reconnects without transport backoff")
        XCTAssertFalse(authManager.authRequired)
        XCTAssertEqual(coordinator.activityHealth, .connected)
    }

    private func makeAuthManager() -> AuthManager {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return authManager
    }

    private func seedStoredAuth() {
        KeychainHelper.save(key: "fa_api_token", string: "rejected-api-token")
        KeychainHelper.save(key: "fa_refresh_token", string: "refresh-token")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
    }

    private func resetStoredAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
    }

    private func waitUntil(_ predicate: @escaping () -> Bool) async throws {
        for _ in 0 ..< 10_000 {
            if predicate() {
                return
            }
            await Task.yield()
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
    private(set) var suspendActiveSendCount = 0
    var shouldFailFollowOpen = false
    /// When set, the follow/activity open throws this specific error instead of the
    /// generic `StubError` (e.g. a `ChatAPIError.server(401)` to exercise the
    /// terminal-auth connect path).
    var followOpenError: Error?
    var activityOpenError: Error?
    /// When > 0, `followOpenError` is thrown only for the first N opens; later opens
    /// fall through to the hang/finish path. Lets a test exercise "401 then a
    /// successful reconnect" without an infinite open→401→refresh spin.
    var followOpenErrorLimit = 0
    /// When > 0, `activityOpenError` is thrown only for the first N opens; later opens
    /// fall through to the hang/finish path.
    var activityOpenErrorLimit = 0
    var onFollowOpen: (() -> Void)?
    var onActivityOpen: (() -> Void)?
    /// When set, the opened stream never finishes on its own, so the loop stays
    /// connected without emitting a competing drop event — letting a test assert
    /// stale-generation rejection deterministically instead of racing the loop.
    var hangFollowOpen = false
    var hangActivityOpen = false
    /// The conversation ID from the most recent openFollowStream call, returned by
    /// currentConversationID so wakeReconnectLoops can restart the loop for the
    /// active conversation.
    private var activeConversationID: String?

    struct StubError: Error {}

    func openFollowStream(
        conversationID: String,
        generation: Int
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        followOpenCount += 1
        onFollowOpen?()
        activeConversationID = conversationID
        if let followOpenError, followOpenErrorLimit == 0 || followOpenCount <= followOpenErrorLimit {
            throw followOpenError
        }
        if shouldFailFollowOpen {
            throw StubError()
        }
        let hang = hangFollowOpen
        return AsyncThrowingStream { continuation in
            if !hang {
                continuation.finish()
            }
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
        onActivityOpen?()
        if let activityOpenError, activityOpenErrorLimit == 0 || activityOpenCount <= activityOpenErrorLimit {
            throw activityOpenError
        }
        if hangActivityOpen {
            return AsyncThrowingStream { _ in }
        }
        throw StubError()
    }

    func activityStreamDidSignal(generation: Int) async {}

    func currentConversationID() -> String? {
        activeConversationID
    }
}

private final class AtomicCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    @discardableResult
    func increment() -> Int {
        lock.withLock {
            count += 1
            return count
        }
    }

    var value: Int {
        lock.withLock { count }
    }

    func suspendActiveSend() {
        suspendActiveSendCount += 1
    }

    func gateAuthIfNeeded(generation: Int) async throws {}
}
