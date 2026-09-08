import CallKit
@testable import FamilyAssistant
import Foundation
import Intents
import UIKit
import XCTest

/// Collects what the call path records, so a test can assert the breadcrumb a
/// step leaves as well as the work it does. The path records from whichever
/// thread delivered the activity, so this locks.
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
private final class RecordingCallController: CallRequesting {
    private(set) var startRequests: [UUID] = []

    func requestStartCall(uuid: UUID, handle _: CXHandle) async throws {
        startRequests.append(uuid)
    }

    private(set) var endRequests: [UUID] = []

    func requestEndCall(uuid: UUID) async throws {
        endRequests.append(uuid)
    }
}

@MainActor
private final class SilentCallProvider: CallProviding {
    func reportOutgoingCall(with _: UUID, startedConnectingAt _: Date?) {}
    func reportOutgoingCall(with _: UUID, connectedAt _: Date?) {}
    func reportCall(with _: UUID, updated _: CXCallUpdate) {}
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

@MainActor
private final class SilentCallAction: CallAction {
    func fulfill() {}
    func fail() {}
}

/// Counts how many coordinators the starter builds, so a test can tell a call
/// that was refused before any CallKit machinery existed from one that was not.
@MainActor
private final class CoordinatorFactory {
    private(set) var buildCount = 0
    private(set) var lastCoordinator: VoiceCallCoordinator?
    let controller = RecordingCallController()

    private let isDeviceUnlocked: Bool
    private let telemetry: VoiceCallTelemetryRecording

    init(
        isDeviceUnlocked: Bool = true,
        telemetry: VoiceCallTelemetryRecording = RecordingVoiceCallTelemetry()
    ) {
        self.isDeviceUnlocked = isDeviceUnlocked
        self.telemetry = telemetry
    }

    func make() -> VoiceCallCoordinator {
        buildCount += 1
        let isDeviceUnlocked = isDeviceUnlocked
        let coordinator = VoiceCallCoordinator(
            provider: SilentCallProvider(),
            controller: controller,
            handsFreeAccess: VoiceHandsFreeAccess(
                isDeviceUnlocked: { isDeviceUnlocked },
                isCarPlayConnected: { false }
            ),
            telemetry: telemetry,
            makeSession: { _ in
                VoiceCallSessionAudio(session: UnusedVoiceCallSession(), audio: UnusedVoiceAudioIO())
            }
        )
        lastCoordinator = coordinator
        return coordinator
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
        let activity = NSUserActivity(activityType: AssistantCallHandle.startCallActivityType)

        XCTAssertTrue(
            HomeScreenShortcutSceneDelegate.forwardUserActivities([activity], from: .sceneContinue)
        )

        XCTAssertEqual(VoiceCallRequestCenter.shared.pendingRequests.count, 1)
    }

    /// "Call Bob using Family Assistant" resolves to this app with Bob as the
    /// destination. Discarding Bob and calling the assistant instead answers a
    /// question nobody asked.
    func testAStartCallIntentNamingSomebodyElseIsRejected() {
        let intent = makeStartCallIntent(contacts: [makePerson(handle: "+15550100", displayName: "Bob")])

        XCTAssertFalse(AssistantCallHandle.isAddressedToAssistant(intent))
    }

    /// The intent Siri matches is the one this app donated, so the donation and
    /// the check have to agree about the handle.
    func testTheDonatedIntentIsAccepted() {
        XCTAssertTrue(
            AssistantCallHandle.isAddressedToAssistant(AssistantCallDonation.makeStartCallIntent())
        )
    }

    /// Whitespace and casing come from what Siri heard, not from the donation.
    func testTheAssistantHandleMatchesRegardlessOfCaseAndSpacing() {
        let intent = makeStartCallIntent(
            contacts: [makePerson(handle: "  family assistant  ", displayName: "  family assistant  ")]
        )

        XCTAssertTrue(AssistantCallHandle.isAddressedToAssistant(intent))
    }

    /// An assistant call has one destination, so a request that also names
    /// somebody else is not one this app can serve — picking the assistant out
    /// of the list would drop the other destination silently.
    func testAStartCallIntentNamingTheAssistantAmongOthersIsRejected() {
        let intent = makeStartCallIntent(contacts: [
            makePerson(handle: "+15550100", displayName: "Bob"),
            makePerson(handle: AssistantCallHandle.value, displayName: AssistantCallHandle.value),
        ])

        XCTAssertFalse(AssistantCallHandle.isAddressedToAssistant(intent))
    }

    /// Siri resolved the app as the destination and told us nothing more. The
    /// primary path is not worth breaking over a payload shape that cannot be
    /// verified from a simulator.
    func testAStartCallIntentNamingNobodyIsAccepted() {
        XCTAssertTrue(AssistantCallHandle.isAddressedToAssistant(makeStartCallIntent(contacts: nil)))
        XCTAssertTrue(AssistantCallHandle.isAddressedToAssistant(makeStartCallIntent(contacts: [])))
        XCTAssertTrue(AssistantCallHandle.isAddressedToAssistant(nil))
    }

    /// What the Intents extension resolves a spoken destination to. The
    /// extension's handler cannot be reached from a test bundle hosted by the
    /// app, so the decision it makes is tested where both targets compile it.
    func testTheDestinationResolvesToTheAssistantAndNothingElse() {
        let bob = makePerson(handle: "+15550100", displayName: "Bob")
        let assistant = makePerson(handle: AssistantCallHandle.value, displayName: AssistantCallHandle.value)

        XCTAssertEqual(AssistantCallHandle.resolveDestination(in: [assistant]), .assistant(assistant))
        XCTAssertEqual(AssistantCallHandle.resolveDestination(in: [bob]), .unsupported)
        XCTAssertEqual(AssistantCallHandle.resolveDestination(in: [bob, assistant]), .unsupported)
        XCTAssertEqual(AssistantCallHandle.resolveDestination(in: [bob, bob]), .unsupported)
        XCTAssertEqual(AssistantCallHandle.resolveDestination(in: []), .unnamed)
        XCTAssertEqual(AssistantCallHandle.resolveDestination(in: nil), .unnamed)
    }

    /// SiriKit reads the resolution array positionally: one result per contact
    /// the intent named. A rejected request has to say so about each of them,
    /// not answer a two-contact request with one result.
    func testTheResolutionAnswersEveryNamedContact() {
        let bob = makePerson(handle: "+15550100", displayName: "Bob")
        let assistant = makePerson(handle: AssistantCallHandle.value, displayName: AssistantCallHandle.value)

        XCTAssertEqual(AssistantCallHandle.resolveContacts(in: nil).count, 1)
        XCTAssertEqual(AssistantCallHandle.resolveContacts(in: []).count, 1)
        XCTAssertEqual(AssistantCallHandle.resolveContacts(in: [assistant]).count, 1)
        XCTAssertEqual(AssistantCallHandle.resolveContacts(in: [bob]).count, 1)
        XCTAssertEqual(AssistantCallHandle.resolveContacts(in: [bob, assistant]).count, 2)
        XCTAssertEqual(AssistantCallHandle.resolveContacts(in: [bob, bob]).count, 2)
    }

    /// Signing in is the only prerequisite the user is told about, so a user
    /// who signs in and never opens the Voice tab must still be resolvable by
    /// Siri.
    func testSigningInDonatesTheCallableHandle() {
        var donated: [INInteraction] = []
        let donor = AssistantCallDonor { donated.append($0) }

        donor.signedInStateChanged(to: false)
        XCTAssertTrue(donated.isEmpty)

        donor.signedInStateChanged(to: true)

        XCTAssertEqual(donated.count, 1)
        let intent = donated.first?.intent as? INStartCallIntent
        XCTAssertTrue(AssistantCallHandle.isAddressedToAssistant(intent))
        XCTAssertEqual(donated.first?.direction, .outgoing)
    }

    func testTheHandleIsDonatedOnlyOncePerProcess() {
        var donationCount = 0
        let donor = AssistantCallDonor { _ in donationCount += 1 }

        donor.signedInStateChanged(to: true)
        donor.signedInStateChanged(to: true)
        donor.signedInStateChanged(to: false)
        donor.signedInStateChanged(to: true)

        XCTAssertEqual(donationCount, 1)
    }

    func testAnUnrelatedActivityProducesNoRequest() {
        let activity = NSUserActivity(activityType: "com.familyassistant.app.something-else")

        XCTAssertFalse(
            HomeScreenShortcutSceneDelegate.forwardUserActivities([activity], from: .sceneContinue)
        )

        XCTAssertTrue(VoiceCallRequestCenter.shared.pendingRequests.isEmpty)
    }

    /// Universal Links keep going to `OpenURLCenter`; only the call activity is
    /// diverted.
    func testABrowsingWebActivityStillForwardsItsURL() {
        let activity = NSUserActivity(activityType: NSUserActivityTypeBrowsingWeb)
        activity.webpageURL = URL(string: "https://assistant.example.test/chat")

        XCTAssertTrue(
            HomeScreenShortcutSceneDelegate.forwardUserActivities([activity], from: .sceneWillConnect)
        )

        XCTAssertTrue(VoiceCallRequestCenter.shared.pendingRequests.isEmpty)
        XCTAssertEqual(
            OpenURLCenter.shared.consumePendingURLs(),
            [URL(string: "https://assistant.example.test/chat")!]
        )
    }

    func testTheApplicationDelegateAcceptsAColdStartCallActivity() {
        let delegate = AppDelegate()
        let activity = NSUserActivity(activityType: AssistantCallHandle.startCallActivityType)

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

    /// The call is owned at process scope and outlives the authenticated UI, so
    /// signing out has to end it rather than leave it listening on revoked
    /// credentials.
    func testSigningOutEndsARunningCall() async {
        let center = VoiceCallRequestCenter()
        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { true }, makeCoordinator: factory.make)
        starter.install(into: center)
        center.receiveStartCallRequest()
        await settleMainActor()

        starter.signedInStateChanged(to: false)
        await settleMainActor()

        XCTAssertEqual(factory.controller.endRequests, factory.controller.startRequests)
    }

    func testStayingSignedInLeavesARunningCallAlone() async {
        let center = VoiceCallRequestCenter()
        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { true }, makeCoordinator: factory.make)
        starter.install(into: center)
        center.receiveStartCallRequest()
        await settleMainActor()

        starter.signedInStateChanged(to: true)
        await settleMainActor()

        XCTAssertTrue(factory.controller.endRequests.isEmpty)
    }

    func testTheVoiceTabStateFollowsTheCallAndTheTabsOwnSession() {
        XCTAssertEqual(
            VoiceTabState.decide(isCallInProgress: false, hasSession: false),
            .startingSession
        )
        XCTAssertEqual(
            VoiceTabState.decide(isCallInProgress: false, hasSession: true),
            .session
        )
        // A call owns the audio session either way: with a tab session of its
        // own already running, the tab still has to stand aside.
        XCTAssertEqual(
            VoiceTabState.decide(isCallInProgress: true, hasSession: false),
            .deferringToCall
        )
        XCTAssertEqual(
            VoiceTabState.decide(isCallInProgress: true, hasSession: true),
            .deferringToCall
        )
    }

    /// The Voice tab reads the running call from the starter, so opening the tab
    /// during a Siri-started call cannot build a second session driving the same
    /// `AVAudioSession`.
    func testTheVoiceTabDoesNotStartASessionDuringACall() async {
        let center = VoiceCallRequestCenter()
        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { true }, makeCoordinator: factory.make)
        starter.install(into: center)

        XCTAssertEqual(
            VoiceTabState.decide(isCallInProgress: starter.isCallActive, hasSession: false),
            .startingSession
        )

        center.receiveStartCallRequest()
        await settleMainActor()

        XCTAssertTrue(starter.isCallActive)
        XCTAssertEqual(
            VoiceTabState.decide(isCallInProgress: starter.isCallActive, hasSession: false),
            .deferringToCall
        )
    }

    func testTheVoiceTabStartsItsOwnSessionAgainOnceTheCallEnds() async throws {
        let center = VoiceCallRequestCenter()
        let factory = CoordinatorFactory()
        let starter = VoiceCallStarter(isAuthenticated: { true }, makeCoordinator: factory.make)
        starter.install(into: center)

        center.receiveStartCallRequest()
        await settleMainActor()
        let coordinator = try XCTUnwrap(factory.lastCoordinator)
        let uuid = try XCTUnwrap(factory.controller.startRequests.last)
        coordinator.performEndCall(uuid: uuid, action: SilentCallAction())

        XCTAssertFalse(starter.isCallActive)
        XCTAssertEqual(
            VoiceTabState.decide(isCallInProgress: starter.isCallActive, hasSession: false),
            .startingSession
        )
    }

    // MARK: - Breadcrumbs

    /// The activity type is what the app matches on, and until the extension
    /// set it the system chose it. An activity arriving under a type we do not
    /// expect is therefore the failure most worth seeing, and recording only
    /// the activities that match makes it look identical to no activity at all.
    func testAnActivityOfAnUnexpectedTypeIsRecordedRatherThanDroppedSilently() {
        let telemetry = RecordingVoiceCallTelemetry()
        let activity = NSUserActivity(activityType: "INStartAudioCallIntent")

        XCTAssertFalse(
            HomeScreenShortcutSceneDelegate.forwardUserActivities(
                [activity],
                from: .appDelegateContinue,
                telemetry: telemetry
            )
        )

        let arrivals = telemetry.extraData(for: "Voice.call.activity")
        XCTAssertEqual(arrivals.count, 1)
        XCTAssertEqual(arrivals.first?["activity_type"], "INStartAudioCallIntent")
        XCTAssertEqual(arrivals.first?["delivery"], "app_delegate_continue")
        XCTAssertEqual(arrivals.first?["has_interaction"], "false")
        XCTAssertEqual(arrivals.first?["has_start_call_intent"], "false")
        XCTAssertTrue(telemetry.extraData(for: "Voice.call.destination").isEmpty)
    }

    /// Three hooks deliver activities and a request that reaches none of them
    /// is a different problem from a request the destination rule refused, so
    /// the arrival says which hook it came through.
    func testEachDeliveryHookNamesItselfOnTheArrival() {
        let hooks: [(UserActivityDelivery, String)] = [
            (.sceneContinue, "scene_continue"),
            (.sceneWillConnect, "scene_will_connect"),
            (.appDelegateContinue, "app_delegate_continue"),
        ]

        for (delivery, name) in hooks {
            let telemetry = RecordingVoiceCallTelemetry()
            HomeScreenShortcutSceneDelegate.forwardUserActivities(
                [NSUserActivity(activityType: "com.familyassistant.app.something-else")],
                from: delivery,
                telemetry: telemetry
            )

            XCTAssertEqual(telemetry.extraData(for: "Voice.call.activity").first?["delivery"], name)
        }
    }

    /// The destination rule is the drop point between an activity that arrived
    /// and a call that started, so it says what it decided and how many names
    /// it decided it over.
    func testAStartCallActivityRecordsTheDestinationDecision() {
        let telemetry = RecordingVoiceCallTelemetry()
        let activity = NSUserActivity(activityType: AssistantCallHandle.startCallActivityType)

        XCTAssertTrue(
            HomeScreenShortcutSceneDelegate.forwardUserActivities(
                [activity],
                from: .sceneContinue,
                telemetry: telemetry
            )
        )

        let decisions = telemetry.extraData(for: "Voice.call.destination")
        XCTAssertEqual(decisions.count, 1)
        XCTAssertEqual(decisions.first?["decision"], "unnamed")
        XCTAssertEqual(decisions.first?["contact_count"], "0")
        XCTAssertEqual(decisions.first?["delivery"], "scene_continue")
    }

    /// Whether a request was acted on or parked waiting for a handler is the
    /// difference between a call that is starting and one that never will.
    func testAStartCallRequestRecordsWhetherItWasHandledOrBuffered() {
        let telemetry = RecordingVoiceCallTelemetry()
        let center = VoiceCallRequestCenter(telemetry: telemetry)

        center.receiveStartCallRequest()
        center.installHandler { _ in }
        center.receiveStartCallRequest()

        let requests = telemetry.extraData(for: "Voice.call.request")
        XCTAssertEqual(requests.map { $0["disposition"] }, ["buffered", "handled"])
        XCTAssertEqual(requests.first?["pending_count"], "1")
    }

    /// The starter records the attempt and nothing about how it turned out.
    /// It cannot know: the coordinator returns once CallKit has accepted the
    /// transaction, and configuring the call's audio — which can still fail the
    /// start — happens afterwards, in the start action. The coordinator records
    /// the outcome where it sees it.
    func testTheStarterRecordsTheAttemptAndLeavesTheOutcomeToTheCoordinator() async {
        let telemetry = RecordingVoiceCallTelemetry()
        let center = VoiceCallRequestCenter(telemetry: telemetry)
        let factory = CoordinatorFactory(telemetry: telemetry)
        let starter = VoiceCallStarter(
            isAuthenticated: { true },
            makeCoordinator: factory.make,
            telemetry: telemetry
        )
        starter.install(into: center)

        center.receiveStartCallRequest()
        await settleMainActor()

        XCTAssertEqual(factory.controller.startRequests.count, 1)
        XCTAssertEqual(telemetry.extraData(for: "Voice.call.starting").count, 1)
        XCTAssertFalse(telemetry.components().contains("Voice.call.started"))
    }

    /// The coordinator refuses a hands-free start from a locked phone that is
    /// not in a car, and says so where it decides it. Nothing claims a call
    /// began — a breadcrumb that says "started" for a refusal is worse than
    /// none.
    func testARefusedHandsFreeStartRecordsTheRefusalAndNoStartedCall() async {
        let telemetry = RecordingVoiceCallTelemetry()
        let factory = CoordinatorFactory(isDeviceUnlocked: false, telemetry: telemetry)
        let starter = VoiceCallStarter(
            isAuthenticated: { true },
            makeCoordinator: factory.make,
            telemetry: telemetry
        )

        await starter.startCall()

        XCTAssertTrue(factory.controller.startRequests.isEmpty)
        XCTAssertTrue(telemetry.components().contains("Voice.call.handsFreeAccess"))
        XCTAssertFalse(telemetry.components().contains("Voice.call.started"))
    }

    /// The refusals are the other half of the same question, and one of them
    /// was silent.
    func testTheStarterRecordsWhyItRefused() async {
        let signedOut = RecordingVoiceCallTelemetry()
        let refusing = VoiceCallStarter(
            isAuthenticated: { false },
            makeCoordinator: CoordinatorFactory().make,
            telemetry: signedOut
        )
        await refusing.startCall()

        XCTAssertEqual(signedOut.components(), ["Voice.call.starting", "Voice.call.signedOut"])

        let duplicate = RecordingVoiceCallTelemetry()
        let starter = VoiceCallStarter(
            isAuthenticated: { true },
            makeCoordinator: CoordinatorFactory().make,
            telemetry: duplicate
        )
        await starter.startCall()
        await starter.startCall()

        XCTAssertTrue(duplicate.components().contains("Voice.call.duplicate"))
    }

    /// What the extension sets the continuation's type to, what the app matches
    /// on, and what `NSUserActivityTypes` declares are one string: an activity
    /// whose type the app does not declare is never delivered to it.
    func testTheStartCallActivityTypeIsTheIntentClassAndIsDeclaredByTheApp() throws {
        XCTAssertEqual(
            AssistantCallHandle.startCallActivityType,
            NSStringFromClass(INStartCallIntent.self)
        )

        let declared = try XCTUnwrap(
            Bundle.main.object(forInfoDictionaryKey: "NSUserActivityTypes") as? [String]
        )
        XCTAssertTrue(declared.contains(AssistantCallHandle.startCallActivityType))
    }

    private func makePerson(handle: String, displayName: String) -> INPerson {
        INPerson(
            personHandle: INPersonHandle(value: handle, type: .unknown),
            nameComponents: nil,
            displayName: displayName,
            image: nil,
            contactIdentifier: nil,
            customIdentifier: nil
        )
    }

    private func makeStartCallIntent(contacts: [INPerson]?) -> INStartCallIntent {
        INStartCallIntent(
            callRecordFilter: nil,
            callRecordToCallBack: nil,
            audioRoute: .unknown,
            destinationType: .normal,
            contacts: contacts,
            callCapability: .audioCall
        )
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
