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
        authManager: AuthManager? = nil,
        channelWarningGraceSeconds: Double = 0.05
    ) -> (SyncCoordinator, StubPathMonitor, AuthManager) {
        let monitor = StubPathMonitor(isSatisfied: satisfied)
        let auth = authManager ?? AuthManager()
        let coordinator = SyncCoordinator(
            authManager: auth,
            pathMonitor: monitor,
            channelWarningGraceSeconds: channelWarningGraceSeconds
        )
        return (coordinator, monitor, auth)
    }

    func testConstructionDoesNotStartPathMonitorUntilStart() {
        let (coordinator, monitor, _) = makeCoordinator(satisfied: true)

        XCTAssertEqual(monitor.startCount, 0)
        XCTAssertEqual(coordinator.reachability, .unknown)

        coordinator.start()

        XCTAssertEqual(monitor.startCount, 1)
        XCTAssertEqual(coordinator.reachability, .satisfied)
        XCTAssertEqual(coordinator.presentation, .syncing)
    }

    func testUnstartedChannelsShowSpinnerNotWifiBad() {
        // Bootstrap runs awaited fetches around the stream starts, so there is a real
        // window where no channel has been opened yet. Nothing has failed in that
        // window, so it must read as the `.syncing` spinner — not the wifi-bad
        // `.degraded` warning, which claims a connection problem that does not exist.
        let (coordinator, _, _) = makeCoordinator()
        coordinator.start()

        XCTAssertEqual(coordinator.followHealth, .idle)
        XCTAssertEqual(coordinator.activityHealth, .idle)
        XCTAssertEqual(coordinator.presentation, .syncing)
    }

    func testDroppedChannelStillDegradesAfterHavingBeenTried() async throws {
        // The `.idle` case must not soften a genuine failure: once a channel has
        // actually connected and dropped, it is `.down` (waiting to retry) and the
        // wifi-bad warning is correct.
        let (coordinator, _, _) = makeCoordinator()
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.apply(.activityDropped(generation: coordinator.activityGeneration, cleanEOF: false))
        XCTAssertEqual(coordinator.activityHealth, .down)
        try await waitUntil { coordinator.presentation == .degraded }
    }

    func testStartLeavesReachabilityUnknownWhenPathUnsatisfied() {
        let (coordinator, _, _) = makeCoordinator(satisfied: false)

        coordinator.start()

        XCTAssertEqual(coordinator.reachability, .unknown)
    }

    func testChannelConnectionTraceReachesLive() {
        let (coordinator, _, _) = makeCoordinator()

        XCTAssertTrue(
            coordinator.apply(.followConnected(generation: coordinator.followGeneration)).isEmpty
        )
        // Activity has not been opened yet (`.idle`, not `.down`), so the half-connected
        // state is the spinner rather than the wifi-bad warning.
        XCTAssertEqual(coordinator.presentation, .syncing)

        XCTAssertTrue(
            coordinator.apply(.activityConnected(generation: coordinator.activityGeneration)).isEmpty
        )
        XCTAssertEqual(coordinator.followHealth, .connected)
        XCTAssertEqual(coordinator.activityHealth, .connected)
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testCleanEOFFollowDropDegradesAfterGrace() async throws {
        let (coordinator, _, _) = makeCoordinator()
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.apply(.followDropped(generation: coordinator.followGeneration, cleanEOF: true))
        XCTAssertEqual(coordinator.followHealth, .down)
        // The warning grace briefly holds `.live` so a momentary drop doesn't flash a
        // warning; a `.down` channel that outlasts the grace degrades to the wifi-bad
        // state ("waiting to retry").
        XCTAssertEqual(
            coordinator.presentation, .live,
            "the warning grace holds live immediately after a drop"
        )
        try await waitUntil { coordinator.presentation == .degraded }
    }

    func testReconnectingBeyondGraceShowsSpinnerNotWifiBad() async throws {
        // A follow channel that is `.reconnecting` (actively re-establishing after a
        // deliberate replace) is held `.live` during the grace so it does not flash on
        // every thread open; if it outlasts the grace it shows the `.syncing` spinner
        // ("actively trying"), never the scary wifi-bad `.degraded` — which is reserved
        // for a `.down` channel waiting to retry.
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        delegate.hangActivityOpen = true
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil {
            delegate.followOpenCount >= 1 && delegate.activityOpenCount >= 1
        }
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        XCTAssertEqual(coordinator.presentation, .live)

        // Demote the follow channel to `.reconnecting` without restarting it, so the
        // reconnecting state persists past the grace window (a live reconnect would
        // auto-connect and return to `.live`; this isolates the reconnecting state).
        coordinator.cancelFollowStream()
        XCTAssertEqual(coordinator.followHealth, .reconnecting)
        XCTAssertEqual(
            coordinator.presentation, .live,
            "the warning grace holds live immediately after a deliberate replace"
        )

        try await waitUntil { coordinator.presentation == .syncing }
        XCTAssertEqual(coordinator.followHealth, .reconnecting)
    }

    func testReturningToLiveWithinGraceNeverFlashesWarning() async throws {
        // The whole point of the grace: a switch that reconnects quickly must never
        // surface a warning. With a long grace the reconnect lands first and cancels
        // the pending grace, so presentation stays `.live` throughout.
        let (coordinator, _, _) = makeCoordinator(channelWarningGraceSeconds: 10)
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        delegate.hangActivityOpen = true
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil {
            delegate.followOpenCount >= 1 && delegate.activityOpenCount >= 1
        }
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.startFollowStream(conversationID: "conv-2")
        try await waitUntil { delegate.followOpenCount >= 2 }
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        XCTAssertEqual(coordinator.followHealth, .connected)
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testRelevanceFlipToConversationArmsGraceInsteadOfFlashing() async throws {
        // Opening a saved conversation from an activity-only-live launch draft flips
        // what `channelsAreLive` requires: a follow stream is now needed but has not
        // started. This live→non-live transition happens purely via the relevance
        // change (not an `apply`), so the owner notifies the coordinator; the grace
        // must then hold `.live` rather than flashing a warning during message load.
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangActivityOpen = true
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        try await waitUntil { delegate.activityOpenCount >= 1 }
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertNil(delegate.currentConversationID())
        XCTAssertEqual(coordinator.presentation, .live)

        // Open a saved conversation: relevance now requires a follow stream too.
        delegate.activeConversationID = "conv-1"
        coordinator.noteConversationRelevanceChanged()
        XCTAssertEqual(
            coordinator.presentation, .live,
            "opening a conversation must not flash a warning before follow connects"
        )

        // Past the grace, a follow stream that has not been opened yet (message load
        // still running, so `startLiveEvents` has not run) is `.idle` — nothing has
        // failed — so this reads as the `.syncing` spinner, never the wifi-bad
        // warning. That warning is reserved for a channel that was tried and dropped.
        try await waitUntil { coordinator.presentation == .syncing }
        XCTAssertEqual(coordinator.followHealth, .idle)
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
        XCTAssertEqual(
            coordinator.activityHealth, .idle,
            "a rejected connect leaves the channel at its never-attempted initial state"
        )
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

    func testFreshFollowStartMarksReconnectingNotDown() {
        // A fresh follow open (no prior connected task) must mark the channel
        // `.reconnecting` (actively opening → spinner) rather than leaving it `.idle`,
        // so the channel reports that an attempt is genuinely under way.
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangFollowOpen = true
        coordinator.delegate = delegate

        XCTAssertEqual(coordinator.followHealth, .idle)
        coordinator.startFollowStream(conversationID: "conv-1")
        XCTAssertEqual(
            coordinator.followHealth, .reconnecting,
            "an actively opening follow stream is reconnecting, not down"
        )
    }

    func testFreshActivityStartMarksReconnectingNotDown() {
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.hangActivityOpen = true
        coordinator.delegate = delegate

        XCTAssertEqual(coordinator.activityHealth, .idle)
        coordinator.startActivityStream()
        XCTAssertEqual(
            coordinator.activityHealth, .reconnecting,
            "an actively opening activity stream is reconnecting, not down"
        )
    }

    func testFollowRetryOpenAfterDropShowsReconnecting() async throws {
        // The backoff wait between attempts is `.down` (wifi-bad), but the retry open
        // itself is an active attempt, so it must show `.reconnecting` (the spinner).
        let reconnected = expectation(description: "follow reopened")
        reconnected.expectedFulfillmentCount = 2
        let coordinator = SyncCoordinator(
            authManager: AuthManager(),
            pathMonitor: StubPathMonitor(isSatisfied: true),
            followReconnectInitialDelaySeconds: 0.01,
            followReconnectMaxDelaySeconds: 0.01
        )
        let delegate = RecordingSyncStreamDelegate()
        delegate.followOpenError = ChatAPIError.server(statusCode: 500, detail: nil, retryAfter: nil)
        delegate.followOpenErrorLimit = 1
        delegate.hangFollowOpen = true
        var healthAtRetryOpen: SyncCoordinator.ChannelHealth?
        delegate.onFollowOpen = {
            if delegate.followOpenCount == 2 {
                healthAtRetryOpen = coordinator.followHealth
            }
            reconnected.fulfill()
        }
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        await fulfillment(of: [reconnected], timeout: 2)

        XCTAssertEqual(
            healthAtRetryOpen, .reconnecting,
            "the retry open after a drop shows the spinner, not the wifi-bad warning"
        )
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

    func testOneChannelConnectedDegradesOnlyOnceTheOtherHasFailed() {
        let (coordinator, _, _) = makeCoordinator()

        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        // Activity has never been opened: not live, but nothing has failed either.
        XCTAssertEqual(coordinator.presentation, .syncing)

        coordinator.apply(.activityDropped(generation: coordinator.activityGeneration, cleanEOF: false))
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

    func testRunResyncStartsAdvisoryStreamsOnlyWhileForegrounded() async throws {
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.activeConversationID = "conv-1"
        coordinator.delegate = delegate

        coordinator.apply(.backgrounded)
        coordinator.runResync()

        XCTAssertEqual(delegate.followOpenCount, 0)
        XCTAssertEqual(delegate.activityOpenCount, 0)

        coordinator.apply(.foregrounded)
        coordinator.runResync()
        try await waitUntil {
            delegate.followOpenCount >= 1 && delegate.activityOpenCount >= 1
        }

        XCTAssertGreaterThanOrEqual(delegate.followOpenCount, 1)
        XCTAssertGreaterThanOrEqual(delegate.activityOpenCount, 1)
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
        coordinator.start()

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
        coordinator.apply(.activityDropped(generation: coordinator.activityGeneration, cleanEOF: false))
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
        coordinator.start()
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

    func testReachabilityRecoveryRunsCoalescedResync() async throws {
        // §4.4: an unsatisfied→satisfied recovery (foregrounded) runs the SAME
        // coalesced resync foreground uses — the full snapshot pass — not the bare
        // loop restart, and bumps both channel generations so late events from the
        // down streams are fenced.
        let (coordinator, monitor, _) = makeCoordinator(satisfied: true)
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate
        coordinator.start()
        let followGenerationBefore = coordinator.followGeneration
        let activityGenerationBefore = coordinator.activityGeneration

        monitor.setSatisfied(false)
        monitor.setSatisfied(true)

        XCTAssertEqual(delegate.runCoalescedResyncCount, 1, "Recovery must request the coalesced resync exactly once.")
        XCTAssertGreaterThan(
            coordinator.followGeneration,
            followGenerationBefore,
            "Recovery must bump the follow generation to fence late events from the down stream."
        )
        XCTAssertGreaterThan(
            coordinator.activityGeneration,
            activityGenerationBefore,
            "Recovery must bump the activity generation to fence late events from the down stream."
        )
    }

    func testReachabilityRecoveryDoesNotResyncWhileBackgrounded() async throws {
        // A recovery hint that lands while backgrounded is a no-op: the resync runs
        // on the next real foreground, not over a suspended app.
        let monitor = StubPathMonitor(isSatisfied: true)
        let coordinator = SyncCoordinator(authManager: AuthManager(), pathMonitor: monitor)
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate
        coordinator.start()
        coordinator.apply(.backgrounded)

        monitor.setSatisfied(false)
        monitor.setSatisfied(true)

        XCTAssertEqual(delegate.runCoalescedResyncCount, 0, "A backgrounded recovery hint must not resync.")
    }

    func testUnsatisfiedMarksConnectedChannelsDownSoRecoveryReconciles() async throws {
        // Both channels are live when the path drops. The SSE tasks have not yet
        // seen their sockets die, so without marking health down here both healths
        // would stay `.connected` and presentation would flip back to `.live` over
        // dead sockets before the recovery resync republishes real health. Marking
        // both down on `.unsatisfied` keeps the indicator honest until an actual
        // connect (driven by the recovery resync) republishes health.
        let (coordinator, monitor, _) = makeCoordinator(satisfied: true)
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.apply(.reachabilityChanged(.unsatisfied))
        XCTAssertEqual(coordinator.followHealth, .down)
        XCTAssertEqual(coordinator.activityHealth, .down)
        XCTAssertEqual(coordinator.presentation, .offline)

        coordinator.apply(.reachabilityChanged(.satisfied))

        XCTAssertEqual(delegate.runCoalescedResyncCount, 1, "Recovery reconciles through the coalesced resync.")
        XCTAssertEqual(coordinator.followHealth, .down, "Health stays down until an actual connect republishes it.")
        XCTAssertEqual(coordinator.activityHealth, .down)
    }

    func testAwaitStreamTerminationCompletesWhenTasksEndPromptly() async throws {
        // Cancellation propagates through the stream's onTermination, so a live
        // consumer throws promptly and the task returns: the await resolves without
        // hitting the bounded timeout.
        let monitor = StubPathMonitor(isSatisfied: true)
        let coordinator = SyncCoordinator(
            authManager: AuthManager(),
            pathMonitor: monitor,
            streamTerminationTimeoutSeconds: 5
        )
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate
        coordinator.startFollowStream(conversationID: "conv-1")
        coordinator.startActivityStream()
        try await waitUntil { delegate.followOpenCount >= 1 && delegate.activityOpenCount >= 1 }

        let started = Date()
        await coordinator.awaitStreamTermination()

        XCTAssertLessThan(
            Date().timeIntervalSince(started),
            2,
            "A promptly-terminating consumer must not wait out the bounded timeout."
        )
    }

    func testAwaitStreamTerminationIsBoundedForAWedgedTask() async throws {
        // An old task whose socket read is wedged (never observes cancellation)
        // must not wedge the resync: the await is bounded by the configured timeout.
        let monitor = StubPathMonitor(isSatisfied: true)
        let coordinator = SyncCoordinator(
            authManager: AuthManager(),
            pathMonitor: monitor,
            streamTerminationTimeoutSeconds: 0.2
        )
        let delegate = WedgingSyncStreamDelegate()
        coordinator.delegate = delegate
        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.followOpenCount >= 1 }

        let started = Date()
        await coordinator.awaitStreamTermination()
        let elapsed = Date().timeIntervalSince(started)

        XCTAssertLessThan(elapsed, 2, "The wedged await must resolve within the bounded timeout, not hang.")
    }

    func testPushHintWhileForegroundedEmitsTargetedRefresh() {
        let (coordinator, _, _) = makeCoordinator()

        let effects = coordinator.apply(.pushHintReceived(conversationID: "web_conv_push"))

        XCTAssertEqual(effects, [.targetedRefresh(conversationID: "web_conv_push")])
    }

    func testPushHintWithoutConversationIDEmitsListOnlyTargetedRefresh() {
        let (coordinator, _, _) = makeCoordinator()

        let effects = coordinator.apply(.pushHintReceived(conversationID: nil))

        XCTAssertEqual(effects, [.targetedRefresh(conversationID: nil)])
    }

    func testPushHintWhileBackgroundedIsANoOp() {
        let (coordinator, _, _) = makeCoordinator()
        coordinator.apply(.backgrounded)

        let effects = coordinator.apply(.pushHintReceived(conversationID: "web_conv_push"))

        XCTAssertTrue(effects.isEmpty, "A backgrounded push hint must not refresh (silent-push out of scope).")
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

    func testReauthSuccessRunsCoalescedResync() async throws {
        // While `authRequired` is latched the streams cannot connect (their
        // requests 401), so their loops sit at capped backoff. On the
        // authRequired→ok transition (re-auth success) recovery must route like the
        // reachability recovery on M2: bump both channel generations (fencing the
        // down streams' late events) and request the coalesced resync, rather than
        // leaving the loops asleep for up to one max-delay interval.
        let (coordinator, _, _) = makeCoordinator(satisfied: true)
        let delegate = RecordingSyncStreamDelegate()
        coordinator.delegate = delegate

        coordinator.apply(.authRequired)
        XCTAssertEqual(coordinator.authState, .authRequired)
        let followGenerationBefore = coordinator.followGeneration
        let activityGenerationBefore = coordinator.activityGeneration

        coordinator.apply(.authOK)

        XCTAssertEqual(
            delegate.runCoalescedResyncCount,
            1,
            "Re-auth success must request the coalesced resync exactly once."
        )
        XCTAssertGreaterThan(
            coordinator.followGeneration,
            followGenerationBefore,
            "Re-auth must bump the follow generation to fence late events from the down stream."
        )
        XCTAssertGreaterThan(
            coordinator.activityGeneration,
            activityGenerationBefore,
            "Re-auth must bump the activity generation to fence late events from the down stream."
        )
    }

    func testAuthRequiredCancelsConnectedStreamsAndMarksHealthsDown() async throws {
        // Finding 1: when authRequired latches while both channels read connected,
        // the old-session sockets keep applying events (the backend authorizes once
        // at connect). The transition must cancel the streams and mark both channels
        // down; combined with the generation bump (via cancelStreams), the later
        // re-auth resync then reconnects under fresh credentials without any old
        // socket lingering as `.connected`.
        let (coordinator, _, _) = makeCoordinator()
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
        let followGenerationBefore = coordinator.followGeneration
        let activityGenerationBefore = coordinator.activityGeneration

        coordinator.apply(.authRequired)

        XCTAssertEqual(coordinator.followHealth, .down)
        XCTAssertEqual(coordinator.activityHealth, .down)
        XCTAssertEqual(coordinator.presentation, .authRequired)
        // Cancelling both streams bumps both generations, so any in-flight callback
        // from the old-session tasks is now fenced.
        XCTAssertGreaterThan(coordinator.followGeneration, followGenerationBefore)
        XCTAssertGreaterThan(coordinator.activityGeneration, activityGenerationBefore)
        let followConnectAfterCancel = coordinator.apply(
            .followConnected(generation: followGenerationBefore)
        )
        XCTAssertTrue(followConnectAfterCancel.isEmpty)
        XCTAssertEqual(coordinator.followHealth, .down)
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
        // Replacing a live follow task must not leave followHealth `.connected` until
        // the new task connects — a late event from the cancelled task must not be
        // mistaken for the new stream. The health drops to `.reconnecting` on replace
        // and is promoted back only by a real connect. Presentation treats that brief
        // `.reconnecting` as `.live` during the warning grace (then the `.syncing`
        // spinner if it lingers), never `.degraded`; see
        // `testReconnectingBeyondGraceShowsSpinnerNotWifiBad`.
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
        delegate.followOpenError = ChatAPIError.server(statusCode: 401, detail: nil, retryAfter: nil)
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

    func testActivityConnect403WithRejectedRefreshStopsAndLatchesAuthRequired() async throws {
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
        delegate.activityOpenError = ChatAPIError.server(statusCode: 403, detail: nil, retryAfter: nil)
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        await fulfillment(of: [authRequired], timeout: 2)
        try await waitUntil { coordinator.activityHealth == .down }

        XCTAssertEqual(delegate.activityOpenCount, 1)
        XCTAssertEqual(refreshRequests.value, 1)
        XCTAssertEqual(delayCount, 0, "terminal auth must stop instead of entering backoff")
        XCTAssertTrue(authManager.authRequired)
        XCTAssertEqual(coordinator.activityHealth, .down)
    }

    func testFollowConnectAuthWallIsSurfacedBeforeBackoff() async throws {
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.followOpenError = ChatAPIError.authWall
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.presentedAuthWallCount == 1 }
        coordinator.cancelStreams()

        XCTAssertEqual(delegate.followOpenCount, 1)
        XCTAssertEqual(delegate.presentedAuthWallCount, 1)
    }

    func testFollowRefreshAuthWallIsSurfacedBeforeBackoff() async throws {
        seedStoredAuth()
        let refreshRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/auth/refresh")
            _ = refreshRequests.increment()
            return .json("<html><body>Sign in</body></html>")
        }
        let coordinator = SyncCoordinator(
            authManager: makeAuthManager(),
            pathMonitor: StubPathMonitor(isSatisfied: true)
        )
        let delegate = RecordingSyncStreamDelegate()
        delegate.followOpenError = ChatAPIError.server(statusCode: 401, detail: nil, retryAfter: nil)
        coordinator.delegate = delegate

        coordinator.startFollowStream(conversationID: "conv-1")
        try await waitUntil { delegate.presentedAuthWallCount == 1 }
        coordinator.cancelStreams()

        XCTAssertEqual(refreshRequests.value, 1)
        XCTAssertEqual(delegate.presentedAuthWallCount, 1)
    }

    func testActivityConnectAuthWallIsSurfacedBeforeBackoff() async throws {
        let (coordinator, _, _) = makeCoordinator()
        let delegate = RecordingSyncStreamDelegate()
        delegate.activityOpenError = ChatAPIError.authWall
        coordinator.delegate = delegate

        coordinator.startActivityStream()
        try await waitUntil { delegate.presentedAuthWallCount == 1 }
        coordinator.cancelStreams()

        XCTAssertEqual(delegate.activityOpenCount, 1)
        XCTAssertEqual(delegate.presentedAuthWallCount, 1)
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
        delegate.followOpenError = ChatAPIError.server(statusCode: 401, detail: nil, retryAfter: nil)
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
        delegate.activityOpenError = ChatAPIError.server(statusCode: 401, detail: nil, retryAfter: nil)
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

    private func waitUntil(
        timeout: Duration = .seconds(5),
        _ predicate: @escaping () -> Bool
    ) async throws {
        // Poll against a real wall-clock deadline with short real sleeps, so a
        // condition gated on an actual timer (e.g. the coordinator's warning grace)
        // is awaited deterministically rather than racing a fixed yield count that
        // can drain before the timer fires.
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if predicate() {
                return
            }
            try await Task.sleep(for: .milliseconds(1))
        }
        if predicate() {
            return
        }
        XCTFail("Timed out waiting for predicate")
    }

    // MARK: - Advisory-read health (M3)

    func testAdvisoryReadsFailingDegradesEvenWithHealthyChannels() {
        // §4.5: persistent advisory-read failure must be visible even when both SSE
        // channels read connected, so a silent-forever background failure is
        // impossible.
        let (coordinator, _, _) = makeCoordinator(satisfied: true)
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertEqual(coordinator.presentation, .live)

        coordinator.apply(.advisoryReadsFailing(true))

        XCTAssertTrue(coordinator.advisoryHealthDegraded)
        XCTAssertEqual(coordinator.presentation, .degraded)
    }

    func testAdvisoryReadsRecoveryClearsDegraded() {
        let (coordinator, _, _) = makeCoordinator(satisfied: true)
        coordinator.apply(.followConnected(generation: coordinator.followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        coordinator.apply(.advisoryReadsFailing(true))
        XCTAssertEqual(coordinator.presentation, .degraded)

        coordinator.apply(.advisoryReadsFailing(false))

        XCTAssertFalse(coordinator.advisoryHealthDegraded)
        XCTAssertEqual(coordinator.presentation, .live)
    }

    func testAdvisoryDegradedDoesNotOverrideAuthOrOffline() {
        // Auth and offline are more specific than advisory health and must still
        // win the presentation derivation.
        let (coordinator, _, _) = makeCoordinator(satisfied: true)
        coordinator.apply(.advisoryReadsFailing(true))

        coordinator.apply(.authRequired)
        XCTAssertEqual(coordinator.presentation, .authRequired)

        coordinator.apply(.authOK)
        coordinator.apply(.reachabilityChanged(.unsatisfied))
        XCTAssertEqual(coordinator.presentation, .offline)
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
    private(set) var runCoalescedResyncCount = 0
    private(set) var presentedAuthWallCount = 0
    private(set) var lastFollowConversationID: String?
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
    var activeConversationID: String?

    struct StubError: Error {}

    func openFollowStream(
        conversationID: String,
        generation: Int
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        followOpenCount += 1
        onFollowOpen?()
        activeConversationID = conversationID
        lastFollowConversationID = conversationID
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

    func presentFollowStreamAuthWall(_ error: Error, generation: Int) {
        presentedAuthWallCount += 1
    }

    func presentActivityStreamAuthWall(_ error: Error, generation: Int) {
        presentedAuthWallCount += 1
    }

    func currentConversationID() -> String? {
        activeConversationID
    }

    func suspendActiveSend() {
        suspendActiveSendCount += 1
    }

    func gateAuthIfNeeded(generation: Int) async throws {}

    func runCoalescedResync(reason _: SyncCoordinator.RestartReason) {
        runCoalescedResyncCount += 1
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
}

/// A `SyncStreamDelegate` whose follow open wedges: it awaits a continuation that
/// is never resumed and does not observe cancellation, modeling a socket read that
/// neither delivers nor closes. Used to prove `awaitStreamTermination` is bounded.
@MainActor
private final class WedgingSyncStreamDelegate: SyncStreamDelegate {
    private(set) var followOpenCount = 0

    func openFollowStream(
        conversationID: String,
        generation: Int
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        followOpenCount += 1
        await withCheckedContinuation { (_: CheckedContinuation<Void, Never>) in
            // Never resumed and no cancellation handler: the owning task stays
            // parked here until the bounded termination await gives up on it.
        }
        return AsyncThrowingStream { $0.finish() }
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
        AsyncThrowingStream { $0.finish() }
    }

    func activityStreamDidSignal(generation: Int) async {}

    func presentFollowStreamAuthWall(_ error: Error, generation: Int) {}

    func presentActivityStreamAuthWall(_ error: Error, generation: Int) {}

    func currentConversationID() -> String? { nil }

    func suspendActiveSend() {}

    func gateAuthIfNeeded(generation: Int) async throws {}

    func runCoalescedResync(reason _: SyncCoordinator.RestartReason) {}
}
