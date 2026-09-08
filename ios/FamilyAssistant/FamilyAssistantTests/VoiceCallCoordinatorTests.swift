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
    private(set) var invalidated = false

    func reportOutgoingCall(with uuid: UUID, startedConnectingAt _: Date?) {
        startedConnecting.append(uuid)
    }

    func reportOutgoingCall(with uuid: UUID, connectedAt _: Date?) {
        connected.append(uuid)
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

@MainActor
private final class FakeCallAction: CallAction {
    private(set) var fulfilled = false
    private(set) var failed = false

    func fulfill() {
        fulfilled = true
    }

    func fail() {
        failed = true
    }
}

@MainActor
@Observable
private final class FakeVoiceCallSession: VoiceCallSession {
    var phase: VoiceSessionViewModel.Phase = .idle
    var isMuted = false
    private(set) var startCount = 0
    private(set) var endCount = 0
    let activation: VoiceAudioActivationSignal

    init(activation: VoiceAudioActivationSignal) {
        self.activation = activation
    }

    func start() async {
        startCount += 1
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

    override func setUp() async throws {
        try await super.setUp()
        provider = FakeCallProvider()
        controller = FakeCallController()
        sessions = []
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
            makeSession: { [weak self] signal in
                let session = FakeVoiceCallSession(activation: signal)
                self?.sessions.append(session)
                return session
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
        XCTAssertEqual(handle.value, VoiceCallCoordinator.assistantHandleValue)
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

    func testStartActionForAnUnknownCallFails() async throws {
        let coordinator = makeCoordinator()
        let action = FakeCallAction()

        coordinator.performStartCall(uuid: UUID(), action: action)

        XCTAssertTrue(action.failed)
        XCTAssertTrue(sessions.isEmpty)
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

        session.phase = .active
        try await Task.sleep(nanoseconds: 5_000_000)
        XCTAssertEqual(provider.connected, [uuid])
    }

    func testRemoteEndEndsTheSessionWithoutReportingBack() async throws {
        let coordinator = makeCoordinator()
        let (uuid, session) = try await startCall(coordinator)
        let action = FakeCallAction()

        coordinator.performEndCall(uuid: uuid, action: action)

        XCTAssertTrue(action.fulfilled)
        XCTAssertEqual(session.endCount, 1)
        XCTAssertFalse(coordinator.isCallActive)
        XCTAssertNil(coordinator.session)
        // CallKit ended the call, so reporting an end back to it would be a
        // second end for the same call.
        try await Task.sleep(nanoseconds: 5_000_000)
        XCTAssertTrue(provider.ended.isEmpty)
        XCTAssertEqual(session.endCount, 1)
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

        // The teardown calls end() on the session, which moves it to .finished
        // again; that must not report a second end.
        let lateAction = FakeCallAction()
        coordinator.performEndCall(uuid: uuid, action: lateAction)
        try await Task.sleep(nanoseconds: 5_000_000)
        XCTAssertTrue(lateAction.fulfilled)
        XCTAssertEqual(provider.ended.count, 1)
    }

    func testProviderResetTearsDownWithoutReporting() async throws {
        let coordinator = makeCoordinator()
        let (_, session) = try await startCall(coordinator)

        coordinator.callProviderDidReset()

        XCTAssertEqual(session.endCount, 1)
        XCTAssertFalse(coordinator.isCallActive)
        XCTAssertNil(coordinator.session)
        try await Task.sleep(nanoseconds: 5_000_000)
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
