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
        XCTAssertEqual(
            host.listSnapshotCount, 1,
            "Resync performs one authoritative full replacement."
        )
        XCTAssertEqual(
            host.recentListSnapshotCount, 1,
            "The post-handoff fallback must use the bounded recent-page refresh."
        )
        XCTAssertEqual(host.phaseStartCount, 1)
        XCTAssertEqual(host.phaseFinishCount, 1)
    }

    func testFailedAuthoritativeListSnapshotRetriesFullReplacementAfterHandoff() async {
        let host = FakeResyncHost(generation: 3, selectedConversationID: "conv-1")
        host.listSnapshotResults = [false, true]
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.listSnapshotCount, 2)
        XCTAssertEqual(
            host.recentListSnapshotCount, 0,
            "A bounded merge must not conceal a failed authoritative replacement."
        )
        XCTAssertEqual(host.restartStreamsCount, 1)
    }

    func testHappyPathEmitsEnterAndExitBreadcrumbsForEveryAwaitedStep() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        var steps: [String] = []
        let orchestrator = ResyncOrchestrator(
            host: host,
            breadcrumb: { component, extraData in
                guard component == "Chat.resyncStep",
                      let step = extraData["step"],
                      let edge = extraData["edge"],
                      let attempt = extraData["attempt"]
                else {
                    return
                }
                steps.append("\(step):\(edge):\(attempt)")
            }
        )

        await orchestrator.request().value

        XCTAssertEqual(
            steps,
            [
                "awaitTermination:enter:1", "awaitTermination:exit:1",
                "gateAuth:enter:1", "gateAuth:exit:1",
                "establishFollow:enter:1", "establishFollow:exit:1",
                "establishActivity:enter:1", "establishActivity:exit:1",
                "listSnapshot:enter:1", "listSnapshot:exit:1",
                "messagesSnapshot:enter:1", "messagesSnapshot:exit:1",
                "drain:enter:1", "drain:exit:1",
                "handoff:enter:1", "handoff:exit:1",
                "finalListSnapshot:enter:1", "finalListSnapshot:exit:1",
            ]
        )
    }

    func testConversationSwitchMidResyncSupersedesAndTargetsNewSelection() async {
        // F4: a switch to another conversation during the list snapshot supersedes
        // the attempt — the stale message snapshot is discarded AND a fresh resync
        // targets the new selection (rather than restarting streams on the old one).
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-a")
        host.onListSnapshot = { host in
            if host.listSnapshotCount == 1 {
                host.selectedConversationID = "conv-b"
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(
            host.messagesSnapshotConversationIDs,
            ["conv-b"],
            "The re-run must snapshot the NEW conversation, never the stale one."
        )
        XCTAssertEqual(
            host.restartStreamsCount, 1,
            "Streams restart once, for the new selection, after the supersede re-run."
        )
        XCTAssertEqual(host.phaseFinishCount, 1, "The syncing phase spans both attempts, closed once.")
    }

    func testConversationSwitchMidResyncDoesNotDrainStaleFollowBuffer() async {
        // F4: buffered follow events belong to the OLD conversation. On a selection
        // switch mid-attempt they must NOT be drained (the follow handler fences by
        // generation, not conversation, so draining could route stale tokens at the
        // new thread).
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-a")
        let follow = ControllableFollowStream()
        host.followStreamSource = follow
        let snapshotGate = AsyncGate()
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                follow.emit(Self.tokenEvent(turnID: "turn-a", text: "stale"))
                host.selectedConversationID = "conv-b"
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
            "A selection switch mid-attempt must not drain the stale conversation's follow buffer."
        )
        XCTAssertEqual(
            host.messagesSnapshotConversationIDs,
            ["conv-b"],
            "The re-run targets the new selection."
        )
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

    func testAuthRejectedMidResyncAbortsCleanlyWithoutRestartingStreams() async {
        // A TERMINAL rejection (401/403): the auth layer latches `authRequired` and
        // a re-auth recovery trigger fires, so the resync aborts without touching
        // the streams and without a modal.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.authGateError = AuthError.authRejected
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.authGateCount, 1)
        XCTAssertEqual(host.listSnapshotCount, 0, "A rejected auth gate aborts before any snapshot.")
        XCTAssertTrue(host.messagesSnapshotConversationIDs.isEmpty)
        XCTAssertEqual(host.restartStreamsCount, 0)
        XCTAssertEqual(host.phaseFinishCount, 1, "The syncing phase is still closed out on abort.")
    }

    func testTransientAuthFailureMidResyncRestartsStreams() async {
        // F3: a TRANSIENT refresh failure (network error / 5xx) after the old loops
        // were torn down must NOT strand the app with no loops. `authRequired` is
        // not latched, so the resync restarts the reconnect loops (their own
        // backoff + near-expiry force-refresh resumes) rather than aborting cold.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.authGateError = AuthError.transient(underlying: URLError(.networkConnectionLost))
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.authGateCount, 1)
        XCTAssertEqual(host.listSnapshotCount, 0, "The transient gate failure aborts before any snapshot.")
        XCTAssertEqual(
            host.restartStreamsCount, 1,
            "The reconnect loops must be restarted so backoff/retry resumes."
        )
        XCTAssertEqual(host.phaseFinishCount, 1)
    }

    func testAuthWallMidResyncRestartsStreams() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.authGateError = AuthError.authWall
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.authGateCount, 1)
        XCTAssertEqual(host.listSnapshotCount, 0)
        XCTAssertEqual(host.restartStreamsCount, 1)
        XCTAssertEqual(host.phaseFinishCount, 1)
    }

    func testNonAuthGateFailureRestartsStreams() async {
        // A non-`AuthError` failure from the gate is treated as transient for the
        // same reason: never leave the torn-down loops stranded.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.authGateError = FakeAuthError.rejected
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.restartStreamsCount, 1)
        XCTAssertEqual(host.listSnapshotCount, 0)
    }

    func testTransientAuthFailureAfterBackgroundBumpDoesNotRestartStreams() async {
        // Finding 11: a transient auth-gate failure normally restarts the loops so
        // they don't strand torn-down. But if the app backgrounded mid-gate (which
        // bumps both generations and cancels the streams by policy), restarting here
        // would reopen the very advisory streams the background policy just
        // cancelled. The restart must be guarded on the captured generations still
        // being current — otherwise skip it and let the next foreground resync own
        // reconnection.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.authGateError = AuthError.transient(underlying: URLError(.networkConnectionLost))
        // Model the background bump landing during the gate.
        host.onAuthGate = { $0.generation += 1 }
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.authGateCount, 1)
        XCTAssertEqual(
            host.restartStreamsCount, 0,
            "A background bump mid-gate must leave reconnection to the next foreground resync."
        )
    }

    func testNonAuthGateFailureAfterBackgroundBumpDoesNotRestartStreams() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        host.authGateError = FakeAuthError.rejected
        host.onAuthGate = { $0.generation += 1 }
        let orchestrator = ResyncOrchestrator(host: host)

        await orchestrator.request().value

        XCTAssertEqual(host.restartStreamsCount, 0)
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
        XCTAssertEqual(host.listSnapshotCount, 1)
        XCTAssertEqual(host.recentListSnapshotCount, 1)
        XCTAssertEqual(host.restartStreamsCount, 1)
    }

    func testSupersedingRequestSchedulesFollowUpRunForNewGenerations() async {
        // F2: a request that arrives while a resync runs and whose generations
        // differ (bumped by a background/foreground/reachability transition) must
        // NOT merely join the stale task — that task aborts on its generation guard
        // and reconciles nothing. A fresh run must cover the new generations.
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        let firstSnapshotGate = AsyncGate()
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                // Bump generations mid-attempt, as a foreground/reachability event
                // would, then hold the snapshot so the superseding request lands
                // while the stale attempt is still in flight.
                host.generation = 2
                await firstSnapshotGate.wait()
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        let first = orchestrator.request()
        try? await waitUntil { host.listSnapshotCount == 1 }
        // The superseding request captures the NEW generation (2); it differs from
        // the running attempt's (1), so it is remembered as superseding.
        let second = orchestrator.request()
        firstSnapshotGate.open()
        await first.value
        await second.value

        // The stale attempt (generation 1) aborted; a fresh attempt ran to
        // completion under generation 2 and restarted the streams.
        XCTAssertEqual(
            host.restartStreamsCount, 1,
            "A fresh resync must run to completion for the new generations."
        )
        XCTAssertEqual(
            host.messagesSnapshotConversationIDs, ["conv-1"],
            "The follow-up run applies the message snapshot the aborted one skipped."
        )
        XCTAssertGreaterThanOrEqual(
            host.authGateCount, 2,
            "The follow-up run is a full second pass, not a joined no-op."
        )
    }

    func testSameGenerationBurstStillCoalescesWithoutFollowUp() async {
        // F2 must not over-fire: a same-generation burst (no transition) still
        // coalesces to exactly one resync — no superseding follow-up run.
        let host = FakeResyncHost(generation: 4, selectedConversationID: "conv-1")
        let gate = AsyncGate()
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                await gate.wait()
            }
        }
        let orchestrator = ResyncOrchestrator(host: host)

        let first = orchestrator.request()
        try? await waitUntil { host.listSnapshotCount == 1 }
        let second = orchestrator.request()
        gate.open()
        await first.value
        await second.value

        XCTAssertEqual(host.authGateCount, 1, "A same-generation burst does the work exactly once.")
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
        XCTAssertEqual(host.listSnapshotCount, 1)
        XCTAssertEqual(host.recentListSnapshotCount, 1)
    }

    func testOverallDeadlineFinishesWedgedFollowEstablishmentAndRestartsStreams() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        let neverOpen = AsyncGate()
        host.onEstablishFollow = {
            await neverOpen.wait()
        }
        var breadcrumbs: [(component: String, extraData: [String: String])] = []
        let orchestrator = ResyncOrchestrator(
            host: host,
            overallDeadlineSeconds: 0.05,
            breadcrumb: { component, extraData in
                breadcrumbs.append((component, extraData))
            }
        )

        await orchestrator.request().value

        XCTAssertEqual(host.phaseStartCount, 1)
        XCTAssertEqual(host.phaseFinishCount, 1)
        XCTAssertFalse(host.isResyncPhaseActive, "The deadline must clear the syncing phase.")
        XCTAssertEqual(host.restartStreamsCount, 1)
        XCTAssertTrue(
            breadcrumbs.contains {
                $0.component == "Chat.resyncStuck"
                    && $0.extraData["last_step"] == "establishFollow"
            }
        )
    }

    func testOverallDeadlineRestartsStreamsAfterGenerationChange() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        let neverOpen = AsyncGate()
        host.onEstablishFollow = { [weak host] in
            host?.generation = 2
            await neverOpen.wait()
        }
        var breadcrumbs: [(component: String, extraData: [String: String])] = []
        let orchestrator = ResyncOrchestrator(
            host: host,
            overallDeadlineSeconds: 0.05,
            breadcrumb: { component, extraData in
                breadcrumbs.append((component, extraData))
            }
        )

        await orchestrator.request().value
        neverOpen.open()

        XCTAssertEqual(host.phaseFinishCount, 1)
        XCTAssertFalse(host.isResyncPhaseActive, "The deadline must still clear the syncing phase.")
        XCTAssertEqual(
            host.restartStreamsCount, 1,
            "Deadline recovery must serve the current target after a superseding generation change."
        )
        XCTAssertTrue(
            breadcrumbs.contains {
                $0.component == "Chat.resyncStuck"
                    && $0.extraData["last_step"] == "establishFollow"
            }
        )
    }

    func testOverallDeadlineReportsOverflowRestartsBeforeWedgedEstablishment() async {
        let host = FakeResyncHost(generation: 1, selectedConversationID: "conv-1")
        let follow = ControllableFollowStream()
        let firstSnapshotGate = AsyncGate()
        let neverOpen = AsyncGate()
        host.followStreamSource = follow
        host.onEstablishFollow = { [weak host] in
            if host?.followEstablishCount == 2 {
                await neverOpen.wait()
            }
        }
        host.onListSnapshotAsync = { host in
            if host.listSnapshotCount == 1 {
                for index in 0 ..< 5 {
                    follow.emit(Self.tokenEvent(turnID: "turn-1", text: "\(index)"))
                }
                await firstSnapshotGate.wait()
            }
        }
        var breadcrumbs: [(component: String, extraData: [String: String])] = []
        let orchestrator = ResyncOrchestrator(
            host: host,
            bufferCapacity: 2,
            overallDeadlineSeconds: 0.2,
            breadcrumb: { component, extraData in
                breadcrumbs.append((component, extraData))
            }
        )

        let task = orchestrator.request()
        try? await waitUntil { host.listSnapshotCount == 1 && follow.emittedCount == 5 }
        follow.finish()
        firstSnapshotGate.open()
        await task.value
        neverOpen.open()

        let finish = breadcrumbs.last {
            $0.component == "Chat.resync" && $0.extraData["phase"] == "finish"
        }
        XCTAssertEqual(finish?.extraData["overflow_restarts"], "1")
        XCTAssertEqual(host.restartStreamsCount, 1)
    }

    func testEstablishmentTimeoutDoesNotAwaitWedgedConnect() async {
        let neverOpen = AsyncGate()
        let startedAt = ContinuousClock.now

        let result = await ChatViewModel.raceResyncStreamEstablishment(timeoutSeconds: 0.02) {
            await neverOpen.wait()
            return true
        }
        let elapsed = ContinuousClock.now - startedAt
        neverOpen.open()

        guard case .timeout = result else {
            return XCTFail("Expected the timeout to win the establishment race.")
        }
        XCTAssertLessThan(
            elapsed,
            .seconds(1),
            "The timeout must return without awaiting a connect that ignores cancellation."
        )
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
    /// Async hook run inside `gateAuthIfNeeded` before it throws `authGateError`
    /// (e.g. to model the app backgrounding mid-gate by bumping `generation`).
    var onAuthGate: ((FakeResyncHost) async -> Void)?
    var onEstablishFollow: (() async -> Void)?
    private(set) var awaitTerminationCount = 0
    private(set) var authGateCount = 0
    private(set) var listSnapshotCount = 0
    private(set) var recentListSnapshotCount = 0

    /// Ordered log of the resync steps, so a test can assert the old-consumer
    /// termination completes before the new follow stream is established.
    private(set) var stepLog: [String] = []
    private(set) var messagesSnapshotConversationIDs: [String] = []
    private(set) var restartStreamsCount = 0
    private(set) var phaseStartCount = 0
    private(set) var phaseFinishCount = 0
    private(set) var isResyncPhaseActive = false
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
    var listSnapshotResults: [Bool] = []

    var followStreamSource: ControllableFollowStream?
    var activityStreamSource: ControllableActivityStream?

    init(generation: Int, selectedConversationID: String?) {
        self.generation = generation
        self.selectedConversationID = selectedConversationID
    }

    // The fake keeps ONE `generation` that stands in for both per-channel
    // generations: a coalesced foreground/recovery resync bumps both together, so
    // mutating `generation` supersedes both channels, matching production.
    var resyncFollowGeneration: Int { generation }
    var resyncActivityGeneration: Int { generation }
    var resyncSelectedConversationID: String? { selectedConversationID }

    func awaitStreamTermination() async {
        awaitTerminationCount += 1
        stepLog.append("awaitTermination")
        await onAwaitTermination?()
    }

    func gateAuthIfNeeded(generation _: Int) async throws {
        authGateCount += 1
        await onAuthGate?(self)
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
        await onEstablishFollow?()
        return followStreamSource?.makeStream()
    }

    func establishActivityStream(
        generation _: Int
    ) async -> AsyncThrowingStream<ChatConversationActivity, Error>? {
        activityEstablishCount += 1
        return activityStreamSource?.makeStream()
    }

    func applyListSnapshot() async -> Bool {
        listSnapshotCount += 1
        onListSnapshot?(self)
        await onListSnapshotAsync?(self)
        let succeeded = listSnapshotResults.isEmpty ? true : listSnapshotResults.removeFirst()
        guard succeeded else {
            return false
        }
        localConversationIDs = serverConversationIDs
        return true
    }

    func applyRecentListSnapshot() async {
        recentListSnapshotCount += 1
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
        isResyncPhaseActive = true
    }

    func resyncPhaseDidFinish() {
        phaseFinishCount += 1
        isResyncPhaseActive = false
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
