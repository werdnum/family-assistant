import CallKit
@testable import FamilyAssistant
import Foundation
import UIKit
import XCTest

@MainActor
private final class RecordingCallController: CallRequesting {
    private(set) var startRequests: [UUID] = []

    func requestStartCall(uuid: UUID, handle _: CXHandle) async throws {
        startRequests.append(uuid)
    }

    func requestEndCall(uuid _: UUID) async throws {}
}

@MainActor
private final class SilentCallProvider: CallProviding {
    func reportOutgoingCall(with _: UUID, startedConnectingAt _: Date?) {}
    func reportOutgoingCall(with _: UUID, connectedAt _: Date?) {}
    func reportCall(with _: UUID, endedAt _: Date?, reason _: CXCallEndedReason) {}
    func invalidate() {}
}

@MainActor
private final class UnusedVoiceCallSession: VoiceCallSession {
    var phase: VoiceSessionViewModel.Phase = .idle
    var isMuted = false

    func start() async {}
    func end() {}
}

private final class UnusedVoiceAudioIO: VoiceAudioIO {
    var onCapturedAudio: (@Sendable (Data) -> Void)?
    var onInputLevel: (@Sendable (Double) -> Void)?
    var onEngineFailure: ((Error) -> Void)?

    func configureAudioSession() throws {}
    func start() async throws {}
    func stop() {}
    func enqueue(_: Data) {}
    func flushPlayback() {}
    func setMuted(_: Bool) {}
}

/// Counts how many coordinators the starter builds, so a test can tell a call
/// that was refused before any CallKit machinery existed from one that was not.
@MainActor
private final class CoordinatorFactory {
    private(set) var buildCount = 0
    let controller = RecordingCallController()

    func make() -> VoiceCallCoordinator {
        buildCount += 1
        return VoiceCallCoordinator(
            provider: SilentCallProvider(),
            controller: controller,
            handsFreeAccess: VoiceHandsFreeAccess(isDeviceUnlocked: { true }, isCarPlayConnected: { false }),
            makeSession: { _ in
                VoiceCallSessionAudio(session: UnusedVoiceCallSession(), audio: UnusedVoiceAudioIO())
            }
        )
    }
}

@MainActor
final class VoiceCallRequestCenterTests: XCTestCase {
    override func setUp() {
        super.setUp()
        resetSharedCenter()
    }

    override func tearDown() {
        resetSharedCenter()
        super.tearDown()
    }

    func testAStartCallActivityProducesAPendingRequest() {
        let activity = NSUserActivity(activityType: VoiceCallRequestCenter.startCallActivityType)

        XCTAssertTrue(HomeScreenShortcutSceneDelegate.forwardUserActivities([activity]))

        XCTAssertEqual(VoiceCallRequestCenter.shared.pendingRequests.count, 1)
    }

    func testAnUnrelatedActivityProducesNoRequest() {
        let activity = NSUserActivity(activityType: "com.familyassistant.app.something-else")

        XCTAssertFalse(HomeScreenShortcutSceneDelegate.forwardUserActivities([activity]))

        XCTAssertTrue(VoiceCallRequestCenter.shared.pendingRequests.isEmpty)
    }

    /// Universal Links keep going to `OpenURLCenter`; only the call activity is
    /// diverted.
    func testABrowsingWebActivityStillForwardsItsURL() {
        let activity = NSUserActivity(activityType: NSUserActivityTypeBrowsingWeb)
        activity.webpageURL = URL(string: "https://assistant.example.test/chat")

        XCTAssertTrue(HomeScreenShortcutSceneDelegate.forwardUserActivities([activity]))

        XCTAssertTrue(VoiceCallRequestCenter.shared.pendingRequests.isEmpty)
        XCTAssertEqual(
            OpenURLCenter.shared.consumePendingURLs(),
            [URL(string: "https://assistant.example.test/chat")!]
        )
    }

    func testTheApplicationDelegateAcceptsAColdStartCallActivity() {
        let delegate = AppDelegate()
        let activity = NSUserActivity(activityType: VoiceCallRequestCenter.startCallActivityType)

        let handled = delegate.application(
            UIApplication.shared,
            continue: activity,
            restorationHandler: { _ in }
        )

        XCTAssertTrue(handled)
        XCTAssertEqual(VoiceCallRequestCenter.shared.pendingRequests.count, 1)
    }

    func testConsumingClearsTheBuffer() {
        VoiceCallRequestCenter.shared.receiveStartCallRequest()
        VoiceCallRequestCenter.shared.receiveStartCallRequest()

        XCTAssertEqual(VoiceCallRequestCenter.shared.consumePendingRequests().count, 2)
        XCTAssertTrue(VoiceCallRequestCenter.shared.pendingRequests.isEmpty)
    }

    func testAnInstalledHandlerRunsImmediatelyAndNothingIsBuffered() {
        let center = VoiceCallRequestCenter()
        var handled = 0
        center.installHandler { _ in handled += 1 }

        center.receiveStartCallRequest()

        XCTAssertEqual(handled, 1)
        XCTAssertTrue(center.pendingRequests.isEmpty)
    }

    func testInstallingAHandlerDrainsWhatArrivedBeforeIt() {
        let center = VoiceCallRequestCenter()
        center.receiveStartCallRequest()
        center.receiveStartCallRequest()

        var handled = 0
        center.installHandler { _ in handled += 1 }

        XCTAssertEqual(handled, 2)
        XCTAssertTrue(center.pendingRequests.isEmpty)
    }

    /// The whole point of installing at launch: a request delivered on a
    /// background launch places a call with no view in existence.
    func testARequestDeliveredAfterInstallationStartsACall() async {
        let center = VoiceCallRequestCenter()
        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { true }, makeCoordinator: factory.make)
        starter.install(into: center)

        center.receiveStartCallRequest()
        await settleMainActor()

        XCTAssertEqual(factory.controller.startRequests.count, 1)
    }

    func testARequestDeliveredBeforeInstallationStartsACallWhenTheHandlerArrives() async {
        let center = VoiceCallRequestCenter()
        center.receiveStartCallRequest()

        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { true }, makeCoordinator: factory.make)
        starter.install(into: center)
        await settleMainActor()

        XCTAssertEqual(factory.controller.startRequests.count, 1)
    }

    func testASecondRequestDuringACallIsRefused() async {
        let center = VoiceCallRequestCenter()
        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { true }, makeCoordinator: factory.make)
        starter.install(into: center)

        center.receiveStartCallRequest()
        await settleMainActor()
        center.receiveStartCallRequest()
        await settleMainActor()

        XCTAssertEqual(factory.controller.startRequests.count, 1)
        XCTAssertEqual(factory.buildCount, 1)
    }

    /// A signed-out app has nothing to authenticate the voice socket with, and
    /// must not build the call machinery for a request it cannot serve.
    func testARequestWhileSignedOutStartsNoCall() async {
        let center = VoiceCallRequestCenter()
        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { false }, makeCoordinator: factory.make)
        starter.install(into: center)

        center.receiveStartCallRequest()
        await settleMainActor()

        XCTAssertEqual(factory.buildCount, 0)
        XCTAssertTrue(factory.controller.startRequests.isEmpty)
    }

    /// A request that arrives while signed out is not carried over into a later
    /// sign-in: it is answered and dropped, not left buffered.
    func testASignedOutRequestIsNotLeftBuffered() async {
        let center = VoiceCallRequestCenter()
        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { false }, makeCoordinator: factory.make)
        starter.install(into: center)

        center.receiveStartCallRequest()
        await settleMainActor()

        XCTAssertTrue(center.pendingRequests.isEmpty)
    }

    /// The starter hands its work to a `Task`, so let the main actor run it.
    private func settleMainActor() async {
        for _ in 0 ..< 5 {
            await Task.yield()
        }
    }

    private func resetSharedCenter() {
        VoiceCallRequestCenter.shared.installHandler(nil)
        _ = VoiceCallRequestCenter.shared.consumePendingRequests()
        _ = OpenURLCenter.shared.consumePendingURLs()
    }
}
