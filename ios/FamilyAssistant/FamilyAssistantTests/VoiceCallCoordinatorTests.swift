import CallKit
@testable import FamilyAssistant
import Foundation
import XCTest

// MARK: - Fakes

@MainActor
private final class FakeCallProvider: CallProviding {
    private(set) var startedConnecting: [UUID] = []
    private(set) var connected: [UUID] = []
    private(set) var ended: [(uuid: UUID, reason: CXCallEndedReason)] = []
    private(set) var updates: [(uuid: UUID, update: CXCallUpdate)] = []
    private(set) var invalidated = false

    func reportOutgoingCall(with uuid: UUID, startedConnectingAt _: Date?) {
        startedConnecting.append(uuid)
    }

    func reportOutgoingCall(with uuid: UUID, connectedAt _: Date?) {
        connected.append(uuid)
    }

    func reportCall(with uuid: UUID, updated update: CXCallUpdate) {
        updates.append((uuid, update))
    }

    func reportCall(with uuid: UUID, endedAt _: Date?, reason: CXCallEndedReason) {
        ended.append((uuid, reason))
    }

    func invalidate() {
        invalidated = true
    }
}

@MainActor
private final class FakeCallController: CallRequesting {
    private(set) var startRequests: [(uuid: UUID, handle: CXHandle)] = []
    private(set) var endRequests: [UUID] = []
    var startError: Error?

    func requestStartCall(uuid: UUID, handle: CXHandle) async throws {
        startRequests.append((uuid, handle))
        if let startError { throw startError }
    }

    func requestEndCall(uuid: UUID) async throws {
        endRequests.append(uuid)
    }
}

/// Records events that have to happen in a particular order, so a test can
/// assert the order rather than each event in isolation.
private final class EventLog {
    private(set) var events: [String] = []

    func record(_ event: String) {
        events.append(event)
    }
}

/// Collects what the coordinator records, so a test can assert the breadcrumb
/// a start left as well as what it did to the call.
private final class RecordingVoiceCallTelemetry: VoiceCallTelemetryRecording {
    private let lock = NSLock()
    private var records: [(event: String, component: String, extraData: [String: String])] = []

    func record(_ event: String, component: String, extraData: [String: String]) {
        lock.withLock { records.append((event, component, extraData)) }
    }

    func components() -> [String] {
        lock.withLock { records.map(\.component) }
    }

    func extraData(for component: String) -> [[String: String]] {
        lock.withLock { records.filter { $0.component == component }.map(\.extraData) }
    }
}

@MainActor
private final class FakeCallAction: CallAction {
    private(set) var fulfilled = false
    private(set) var failed = false
    private let log: EventLog?

    init(log: EventLog? = nil) {
        self.log = log
    }

    func fulfill() {
        fulfilled = true
        log?.record("fulfill")
    }

    func fail() {
        failed = true
        log?.record("fail")
    }
}

/// Stands in for the call's ``VoiceAudioEngine``. Only the audio-session
/// configuration the coordinator drives itself is interesting here; the session
/// owns everything else.
private final class FakeVoiceAudioIO: VoiceAudioIO {
    var onCapturedAudio: (@Sendable (Data) -> Void)?
    var onInputLevel: (@Sendable (Double) -> Void)?
    var onEngineFailure: ((Error) -> Void)?
    var configureError: Error?
    private(set) var configureCount = 0
    private let log: EventLog?

    init(log: EventLog?) {
        self.log = log
    }

    func configureAudioSession() throws {
        configureCount += 1
        log?.record("configure-audio")
        if let configureError { throw configureError }
    }

    func start() async throws {}
    func stop() {}
    func enqueue(_: Data) {}
    func flushPlayback() {}
    func setMuted(_: Bool) {}
}

@MainActor
@Observable
private final class FakeVoiceCallSession: VoiceCallSession {
    var phase: VoiceSessionViewModel.Phase = .idle
    var isMuted = false
    /// Park in `start()` on the activation signal, the way the real session's
    /// audio engine does under CallKit.
    var waitsForActivation = false
    private(set) var startCount = 0
    private(set) var endCount = 0
    private(set) var startCancelled = false
    private(set) var isStartRunning = false
    let activation: VoiceAudioActivationSignal

    init(activation: VoiceAudioActivationSignal) {
        self.activation = activation
    }

    func start() async {
        startCount += 1
        guard waitsForActivation else { return }
        isStartRunning = true
        do {
            try await activation.waitForActivation()
        } catch {
            startCancelled = true
        }
        isStartRunning = false
    }

    func end() {
        endCount += 1
        phase = .finished
    }
}

// MARK: - Tests

@MainActor
final class VoiceCallCoordinatorTests: XCTestCase {
    private var provider: FakeCallProvider!
    private var controller: FakeCallController!
    private var sessions: [FakeVoiceCallSession] = []
    private var audios: [FakeVoiceAudioIO] = []
    private var sessionsWaitForActivation = false
    private var eventLog: EventLog?
    private var audioConfigureError: Error?
    private var telemetry: RecordingVoiceCallTelemetry!

    override func setUp() async throws {
        try await super.setUp()
        provider = FakeCallProvider()
        controller = FakeCallController()
        sessions = []
        audios = []
        sessionsWaitForActivation = false
        eventLog = nil
        audioConfigureError = nil
        telemetry = RecordingVoiceCallTelemetry()
    }

    private func makeCoordinator(
        handsFreeAccess: VoiceHandsFreeAccess = VoiceHandsFreeAccess(
            isDeviceUnlocked: { true },
            isCarPlayConnected: { false }
        )
    ) -> VoiceCallCoordinator {
        VoiceCallCoordinator(
            provider: provider,
            controller: controller,
            handsFreeAccess: handsFreeAccess,
            telemetry: telemetry,
            makeSession: { [weak self] signal in
                let session = FakeVoiceCallSession(activation: signal)
                session.waitsForActivation = self?.sessionsWaitForActivation ?? false
                self?.sessions.append(session)
                let audio = FakeVoiceAudioIO(log: self?.eventLog)
                audio.configureError = self?.audioConfigureError
                self?.audios.append(audio)
                return VoiceCallSessionAudio(session: session, audio: audio)
            }
        )
    }

    /// Starts a call and performs the resulting start action, returning the
    /// call's UUID and the session the coordinator built for it.
    private func startCall(
        _ coordinator: VoiceCallCoordinator
    ) async throws -> (uuid: UUID, session: FakeVoiceCallSession) {
        try await coordinator.startCall()
        let uuid = try XCTUnwrap(controller.startRequests.last?.uuid)
        coordinator.performStartCall(uuid: uuid, action: FakeCallAction())
        let session = try XCTUnwrap(sessions.last)
        return (uuid, session)
    }

    /// Runs `body` and returns once the coordinator has handled the session
    /// phase change it causes. An assertion that nothing further was reported
    /// then runs after the handler has definitely run, instead of after a delay
    /// that only assumes it has.
    private func afterPhaseHandled(
        of coordinator: VoiceCallCoordinator,
        _ body: @MainActor () -> Void
    ) async {
        await withCheckedContinuation { continuation in
            coordinator.onSessionPhaseHandled = {
                coordinator.onSessionPhaseHandled = nil
                continuation.resume()
            }
            body()
        }
    }

    private func waitUntil(
        timeout: TimeInterval = 2,
        _ condition: @MainActor () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() > deadline {
                XCTFail("Condition not met before timeout.")
                return
            }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
    }

    func testStartCallRequestsAGenericHandleTransaction() async throws {
        let coordinator = makeCoordinator()
        try await coordinator.startCall()

        XCTAssertEqual(controller.startRequests.count, 1)
        let handle = try XCTUnwrap(controller.startRequests.first?.handle)
        XCTAssertEqual(handle.type, .generic)
        XCTAssertEqual(handle.value, AssistantCallHandle.value)
        XCTAssertTrue(coordinator.isCallActive)
    }

    func testALockedDeviceOffCarPlayNeverReachesTheCallController() async throws {
        let coordinator = makeCoordinator(
            handsFreeAccess: VoiceHandsFreeAccess(
                isDeviceUnlocked: { false },
                isCarPlayConnected: { false }
            )
        )

        // The refusal is deliberately quiet: no throw, and nothing for Siri or
        // the lock screen to show.
        try await coordinator.startCall()

        XCTAssertTrue(controller.startRequests.isEmpty)
        XCTAssertFalse(coordinator.isCallActive)
        XCTAssertTrue(sessions.isEmpty)
        // Quiet to the user, not to the trail: a refusal nobody is told about
        // is the one most in need of a breadcrumb.
        XCTAssertEqual(telemetry.components(), ["Voice.call.handsFreeAccess"])
    }

    func testALockedDeviceOnCarPlayCanStartACall() async throws {
        let coordinator = makeCoordinator(
            handsFreeAccess: VoiceHandsFreeAccess(
                isDeviceUnlocked: { false },
                isCarPlayConnected: { true }
            )
        )

        try await coordinator.startCall()

        XCTAssertEqual(controller.startRequests.count, 1)
        XCTAssertTrue(coordinator.isCallActive)
    }

    func testSecondStartCallIsRefused() async throws {
        let coordinator = makeCoordinator()
        try await coordinator.startCall()
        do {
            try await coordinator.startCall()
            XCTFail("Expected the second call to be refused.")
        } catch {
            XCTAssertEqual(error as? VoiceCallError, .callAlreadyInProgress)
        }
        XCTAssertEqual(controller.startRequests.count, 1)
    }

    func testAFailedTransactionLeavesNoCallBehind() async throws {
        let coordinator = makeCoordinator()
        controller.startError = NSError(domain: "test", code: 1)
        do {
            try await coordinator.startCall()
            XCTFail("Expected the transaction failure to propagate.")
        } catch {
            XCTAssertFalse(coordinator.isCallActive)
        }
    }

    func testPerformingStartReportsConnectingAndStartsTheSession() async throws {
        let coordinator = makeCoordinator()
        try await coordinator.startCall()
        let uuid = try XCTUnwrap(controller.startRequests.last?.uuid)
        let action = FakeCallAction()

        coordinator.performStartCall(uuid: uuid, action: action)

        XCTAssertEqual(provider.startedConnecting, [uuid])
        XCTAssertTrue(action.fulfilled)
        let session = try XCTUnwrap(sessions.last)
        try await waitUntil { session.startCount == 1 }
        XCTAssertTrue(coordinator.session === session)
    }

    /// A call that started fine and a call that was never asked for otherwise
    /// leave the same trail: nothing. The record is made here rather than where
    /// the call was requested, because CallKit accepts the transaction before
    /// any of this runs.
    func testAFulfilledStartActionRecordsThatTheCallBegan() async throws {
        let coordinator = makeCoordinator()
        _ = try await startCall(coordinator)

        XCTAssertEqual(telemetry.extraData(for: "Voice.call.started").count, 1)
        XCTAssertFalse(telemetry.components().contains("Voice.call.startFailed"))
    }

    /// An assistant conversation cannot be held. Saying so is what lets an
    /// incoming phone call take the audio and end this call, instead of CallKit
    /// timing out a hold action that no delegate method answers.
    func testTheReportedCallDeclaresItCannotBeHeldOrGrouped() async throws {
        let coordinator = makeCoordinator()
        try await coordinator.startCall()
        let uuid = try XCTUnwrap(controller.startRequests.last?.uuid)

        coordinator.performStartCall(uuid: uuid, action: FakeCallAction())

        let reported = try XCTUnwrap(provider.updates.last)
        XCTAssertEqual(reported.uuid, uuid)
        XCTAssertFalse(reported.update.supportsHolding)
        XCTAssertFalse(reported.update.supportsGrouping)
        XCTAssertFalse(reported.update.supportsUngrouping)
        XCTAssertFalse(reported.update.supportsDTMF)
    }

    /// CallKit activates the audio session on the back of the fulfilled start
    /// action. A session still on the default category at that moment brings the
    /// call up with no audio, which is worst on the cold launch this whole
    /// feature exists for.
    func testTheAudioSessionIsConfiguredBeforeTheStartActionIsFulfilled() async throws {
        let log = EventLog()
        eventLog = log
        let coordinator = makeCoordinator()
        try await coordinator.startCall()
        let uuid = try XCTUnwrap(controller.startRequests.last?.uuid)

        coordinator.performStartCall(uuid: uuid, action: FakeCallAction(log: log))

        XCTAssertEqual(log.events, ["configure-audio", "fulfill"])
        XCTAssertEqual(audios.last?.configureCount, 1)
    }

    /// The session's own startup stays behind CallKit's activation callback, so
    /// configuring the category cannot wait on it.
    func testTheSessionIsNotStartedByConfiguringTheAudioSession() async throws {
        sessionsWaitForActivation = true
        let coordinator = makeCoordinator()
        let (_, session) = try await startCall(coordinator)

        try await waitUntil { session.isStartRunning }
        XCTAssertFalse(session.activation.isActivated)
        XCTAssertEqual(audios.last?.configureCount, 1)
    }

    func testAnAudioSessionThatCannotBeConfiguredFailsTheStartAction() async throws {
        audioConfigureError = NSError(domain: "test", code: 2)
        let coordinator = makeCoordinator()
        try await coordinator.startCall()
        let uuid = try XCTUnwrap(controller.startRequests.last?.uuid)
        let action = FakeCallAction()

        coordinator.performStartCall(uuid: uuid, action: action)

        XCTAssertTrue(action.failed)
        XCTAssertFalse(action.fulfilled)
        XCTAssertFalse(coordinator.isCallActive)
        XCTAssertNil(coordinator.session)
        XCTAssertEqual(sessions.last?.startCount, 0)
    }

    /// CallKit accepts the transaction before the start action runs, so this
    /// failure happens after everything that asked for the call has been told
    /// the request went through. Claiming a call began from that acceptance is
    /// exactly the false positive the trail exists to rule out.
    func testAStartFailedByAudioConfigurationIsRecordedAndIsNotAStartedCall() async throws {
        audioConfigureError = NSError(domain: "test", code: 2)
        let coordinator = makeCoordinator()
        try await coordinator.startCall()
        let uuid = try XCTUnwrap(controller.startRequests.last?.uuid)

        coordinator.performStartCall(uuid: uuid, action: FakeCallAction())

        XCTAssertFalse(telemetry.components().contains("Voice.call.started"))
        let failures = telemetry.extraData(for: "Voice.call.startFailed")
        XCTAssertEqual(failures.count, 1)
        XCTAssertEqual(failures.first?["reason"], "audio_configuration")
    }

    func testStartActionForAnUnknownCallFails() async throws {
        let coordinator = makeCoordinator()
        let action = FakeCallAction()

        coordinator.performStartCall(uuid: UUID(), action: action)

        XCTAssertTrue(action.failed)
        XCTAssertTrue(sessions.isEmpty)
        XCTAssertEqual(
            telemetry.extraData(for: "Voice.call.startFailed").first?["reason"],
            "unknown_call"
        )
    }

    func testMuteActionAppliesTheRequestedStateToTheSession() async throws {
        let coordinator = makeCoordinator()
        let (uuid, session) = try await startCall(coordinator)

        let muteAction = FakeCallAction()
        coordinator.performSetMuted(uuid: uuid, muted: true, action: muteAction)
        XCTAssertTrue(session.isMuted)
        XCTAssertTrue(muteAction.fulfilled)

        let unmuteAction = FakeCallAction()
        coordinator.performSetMuted(uuid: uuid, muted: false, action: unmuteAction)
        XCTAssertFalse(session.isMuted)
        XCTAssertTrue(unmuteAction.fulfilled)
    }

    func testMuteActionForAnUnknownCallFails() async throws {
        let coordinator = makeCoordinator()
        let (_, session) = try await startCall(coordinator)
        let action = FakeCallAction()

        coordinator.performSetMuted(uuid: UUID(), muted: true, action: action)

        XCTAssertTrue(action.failed)
        XCTAssertFalse(action.fulfilled)
        XCTAssertFalse(session.isMuted)
    }

    func testAudioActivationReleasesTheSessionsAudioEngine() async throws {
        let coordinator = makeCoordinator()
        let (_, session) = try await startCall(coordinator)

        XCTAssertFalse(session.activation.isActivated)
        coordinator.audioSessionActivated()
        XCTAssertTrue(session.activation.isActivated)

        coordinator.audioSessionDeactivated()
        XCTAssertFalse(session.activation.isActivated)
    }

    func testConnectedIsReportedOnceWhenTheSessionGoesActive() async throws {
        let coordinator = makeCoordinator()
        let (uuid, session) = try await startCall(coordinator)

        session.phase = .connecting
        session.phase = .active
        try await waitUntil { self.provider.connected == [uuid] }

        // Reaching `.active` a second time — the session reconnecting, or any
        // later phase churn — must not report a second connection for a call
        // that is already up.
        await afterPhaseHandled(of: coordinator) { session.phase = .connecting }
        await afterPhaseHandled(of: coordinator) { session.phase = .active }
        XCTAssertEqual(provider.connected, [uuid])
    }

    func testRemoteEndEndsTheSessionWithoutReportingBack() async throws {
        let coordinator = makeCoordinator()
        let (uuid, session) = try await startCall(coordinator)
        let action = FakeCallAction()

        // Teardown ends the session, which moves it to `.finished`; the wait is
        // for that phase change to have been handled, because it is what could
        // report the end a second time.
        await afterPhaseHandled(of: coordinator) {
            coordinator.performEndCall(uuid: uuid, action: action)
        }

        XCTAssertTrue(action.fulfilled)
        XCTAssertEqual(session.endCount, 1)
        XCTAssertFalse(coordinator.isCallActive)
        XCTAssertNil(coordinator.session)
        // CallKit ended the call, so reporting an end back to it would be a
        // second end for the same call.
        XCTAssertTrue(provider.ended.isEmpty)
    }

    func testEndCallForwardsTheUsersRequestToTheCallController() async throws {
        let coordinator = makeCoordinator()
        let (uuid, _) = try await startCall(coordinator)

        try await coordinator.endCall()

        XCTAssertEqual(controller.endRequests, [uuid])
    }

    func testASessionThatFailsEndsTheCall() async throws {
        let coordinator = makeCoordinator()
        let (uuid, session) = try await startCall(coordinator)

        session.phase = .failed("no token")
        try await waitUntil { !self.provider.ended.isEmpty }

        XCTAssertEqual(provider.ended.count, 1)
        XCTAssertEqual(provider.ended.first?.uuid, uuid)
        XCTAssertEqual(provider.ended.first?.reason, .failed)
        XCTAssertFalse(coordinator.isCallActive)
    }

    func testADeniedMicrophoneEndsTheCall() async throws {
        let coordinator = makeCoordinator()
        let (uuid, session) = try await startCall(coordinator)

        session.phase = .permissionDenied
        try await waitUntil { !self.provider.ended.isEmpty }

        XCTAssertEqual(provider.ended.first?.uuid, uuid)
        XCTAssertEqual(provider.ended.first?.reason, .failed)
    }

    func testASessionThatFinishesOnItsOwnEndsTheCallOnce() async throws {
        let coordinator = makeCoordinator()
        let (uuid, session) = try await startCall(coordinator)

        session.phase = .finished
        try await waitUntil { !self.provider.ended.isEmpty }
        XCTAssertEqual(provider.ended.first?.uuid, uuid)
        XCTAssertEqual(provider.ended.first?.reason, .remoteEnded)

        // The call is already gone, so a late end action is answered and
        // otherwise ignored — every step of it is synchronous.
        let lateAction = FakeCallAction()
        coordinator.performEndCall(uuid: uuid, action: lateAction)
        XCTAssertTrue(lateAction.fulfilled)
        XCTAssertEqual(provider.ended.count, 1)
    }

    func testProviderResetTearsDownWithoutReporting() async throws {
        let coordinator = makeCoordinator()
        let (_, session) = try await startCall(coordinator)

        await afterPhaseHandled(of: coordinator) { coordinator.callProviderDidReset() }

        XCTAssertEqual(session.endCount, 1)
        XCTAssertFalse(coordinator.isCallActive)
        XCTAssertNil(coordinator.session)
        XCTAssertTrue(provider.ended.isEmpty)
    }

    func testEndingACallParkedOnActivationCancelsTheStartup() async throws {
        sessionsWaitForActivation = true
        let coordinator = makeCoordinator()
        let (uuid, session) = try await startCall(coordinator)
        try await waitUntil { session.isStartRunning }

        coordinator.performEndCall(uuid: uuid, action: FakeCallAction())

        // Teardown clears the activation signal, so nothing else could ever
        // resume the wait; cancelling the startup task is what releases it.
        try await waitUntil { session.startCancelled }
        XCTAssertFalse(session.isStartRunning)
        XCTAssertEqual(session.endCount, 1)
        XCTAssertNil(coordinator.session)
        XCTAssertFalse(coordinator.isCallActive)
        XCTAssertTrue(provider.ended.isEmpty)
    }

    func testAcallCanBeStartedAgainAfterTheFirstOneEnds() async throws {
        let coordinator = makeCoordinator()
        let (uuid, _) = try await startCall(coordinator)
        coordinator.performEndCall(uuid: uuid, action: FakeCallAction())

        let (secondUUID, secondSession) = try await startCall(coordinator)

        XCTAssertNotEqual(secondUUID, uuid)
        XCTAssertEqual(provider.startedConnecting, [uuid, secondUUID])
        secondSession.phase = .active
        try await waitUntil { self.provider.connected == [secondUUID] }
    }
}
