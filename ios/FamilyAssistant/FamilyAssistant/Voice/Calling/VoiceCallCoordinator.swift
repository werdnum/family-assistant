import CallKit
import Foundation
import os

/// The part of ``VoiceSessionViewModel`` the call coordinator drives. Injected
/// so the coordinator's state machine can be exercised without a live session.
@MainActor
protocol VoiceCallSession: AnyObject {
    var phase: VoiceSessionViewModel.Phase { get }
    func start() async
    func end()
}

extension VoiceSessionViewModel: VoiceCallSession {}

enum VoiceCallError: LocalizedError {
    case callAlreadyInProgress

    var errorDescription: String? {
        switch self {
        case .callAlreadyInProgress:
            "A voice call is already in progress."
        }
    }
}

/// Presents a voice session to the system as an outgoing call.
///
/// The two lifecycles are mapped onto each other: CallKit's start action builds
/// and starts the session, its audio activation releases the session's audio
/// engine, and either side ending ends the other. Each direction ends the call
/// exactly once — the call UUID is cleared as the first step of teardown, and
/// every entry point is guarded on it, so a session that finishes because
/// CallKit ended it cannot report the end back a second time.
@MainActor
@Observable
final class VoiceCallCoordinator: VoiceCallEventHandling {
    /// How the assistant is addressed on the system call screen and by Siri.
    static let assistantHandleValue = "Assistant"

    private(set) var session: (any VoiceCallSession)?

    private let provider: any CallProviding
    private let controller: any CallRequesting
    private let makeSession: @MainActor (VoiceAudioActivationSignal) -> any VoiceCallSession
    private let handsFreeAccess: VoiceHandsFreeAccess
    private let logger = Logger(subsystem: "com.familyassistant.app", category: "voice-call")

    private var callUUID: UUID?
    private var activationSignal: VoiceAudioActivationSignal?
    private var didReportConnected = false

    init(
        provider: any CallProviding,
        controller: any CallRequesting,
        handsFreeAccess: VoiceHandsFreeAccess = .system,
        makeSession: @escaping @MainActor (VoiceAudioActivationSignal) -> any VoiceCallSession
    ) {
        self.provider = provider
        self.controller = controller
        self.handsFreeAccess = handsFreeAccess
        self.makeSession = makeSession
    }

    /// Build a coordinator against the real CallKit provider and controller,
    /// running the same session machinery as the in-app Voice tab on an
    /// externally-managed audio engine.
    static func system(
        authManager: AuthManager,
        profileID: String? = nil
    ) -> VoiceCallCoordinator {
        let systemProvider = SystemCallProvider()
        let coordinator = VoiceCallCoordinator(
            provider: systemProvider,
            controller: SystemCallController(),
            makeSession: { signal in
                let api = ChatAPIClient(authManager: authManager)
                return VoiceSessionViewModel(
                    tokenProvider: api,
                    toolExecutor: api,
                    transcriptStore: api,
                    audio: VoiceAudioEngine(activation: .externallyManaged(signal)),
                    profileID: profileID
                )
            }
        )
        systemProvider.handler = coordinator
        return coordinator
    }

    var isCallActive: Bool {
        callUUID != nil
    }

    /// Ask the system to place the outgoing call. The session itself is not
    /// built until CallKit performs the start action.
    ///
    /// A refusal on hands-free access is quiet: the request arrives as a user
    /// activity with no channel back to Siri, and showing a failed call on the
    /// lock screen of a phone whose holder we have just decided not to talk to
    /// would be worse than saying nothing. It leaves a breadcrumb instead.
    func startCall() async throws {
        guard handsFreeAccess.isAllowed else {
            logger.notice("Refusing a call start: the device is locked and not connected to CarPlay")
            ErrorReporter.shared.report(
                message: "Refused a hands-free assistant call: device locked, not on CarPlay",
                component: "Voice.call.handsFreeAccess",
                errorType: .component
            )
            return
        }
        guard callUUID == nil else { throw VoiceCallError.callAlreadyInProgress }
        let uuid = UUID()
        callUUID = uuid
        let handle = CXHandle(type: .generic, value: Self.assistantHandleValue)
        do {
            try await controller.requestStartCall(uuid: uuid, handle: handle)
        } catch {
            if callUUID == uuid {
                callUUID = nil
            }
            throw error
        }
    }

    /// End the call at the user's request from inside the app.
    func endCall() async throws {
        guard let uuid = callUUID else { return }
        try await controller.requestEndCall(uuid: uuid)
    }

    // MARK: - VoiceCallEventHandling

    func performStartCall(uuid: UUID, action: any CallAction) {
        guard uuid == callUUID else {
            action.fail()
            return
        }
        provider.reportOutgoingCall(with: uuid, startedConnectingAt: Date())

        let signal = VoiceAudioActivationSignal()
        activationSignal = signal
        didReportConnected = false
        let session = makeSession(signal)
        self.session = session
        action.fulfill()

        observePhase(of: session, uuid: uuid)
        Task { await session.start() }
    }

    func performEndCall(uuid: UUID, action: any CallAction) {
        // The call is already gone from CallKit's point of view, so the action is
        // fulfilled whether or not it matches the call we are running.
        if uuid == callUUID {
            teardown(uuid: uuid, reporting: nil)
        }
        action.fulfill()
    }

    func audioSessionActivated() {
        activationSignal?.signalActivated()
    }

    func audioSessionDeactivated() {
        activationSignal?.signalDeactivated()
    }

    func callProviderDidReset() {
        // Every call the provider knew about is gone; reporting an end to it
        // would be meaningless, so just release our side.
        guard let uuid = callUUID else { return }
        logger.warning("Call provider reset; tearing down the voice session")
        teardown(uuid: uuid, reporting: nil)
    }

    // MARK: - Session lifecycle

    /// Re-armed after each change, because `withObservationTracking` fires once.
    /// `onChange` runs *before* the new value is stored, so the phase is read
    /// back on the next main-actor turn.
    private func observePhase(of session: any VoiceCallSession, uuid: UUID) {
        withObservationTracking {
            _ = session.phase
        } onChange: { [weak self] in
            Task { @MainActor in
                self?.handlePhase(of: session, uuid: uuid)
            }
        }
    }

    private func handlePhase(of session: any VoiceCallSession, uuid: UUID) {
        guard uuid == callUUID else { return }
        switch session.phase {
        case .active:
            if !didReportConnected {
                didReportConnected = true
                provider.reportOutgoingCall(with: uuid, connectedAt: Date())
            }
            observePhase(of: session, uuid: uuid)
        case .finished:
            teardown(uuid: uuid, reporting: .remoteEnded)
        case .failed:
            teardown(uuid: uuid, reporting: .failed)
        case .permissionDenied:
            teardown(uuid: uuid, reporting: .failed)
        case .idle, .requestingPermission, .connecting:
            observePhase(of: session, uuid: uuid)
        }
    }

    /// Release the call and the session together. `reason` is non-nil only when
    /// the session ended on its own and the system call screen has to be told;
    /// when CallKit ended the call it already knows.
    private func teardown(uuid: UUID, reporting reason: CXCallEndedReason?) {
        guard callUUID == uuid else { return }
        callUUID = nil
        activationSignal = nil
        didReportConnected = false
        let session = self.session
        self.session = nil
        session?.end()
        if let reason {
            provider.reportCall(with: uuid, endedAt: Date(), reason: reason)
        }
    }
}
