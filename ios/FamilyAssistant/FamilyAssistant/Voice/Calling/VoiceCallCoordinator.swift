import CallKit
import Foundation
import os

/// The part of ``VoiceSessionViewModel`` the call coordinator drives. Injected
/// so the coordinator's state machine can be exercised without a live session.
@MainActor
protocol VoiceCallSession: AnyObject {
    var phase: VoiceSessionViewModel.Phase { get }
    var isMuted: Bool { get set }
    func start() async
    func end()
}

extension VoiceSessionViewModel: VoiceCallSession {}

/// A voice session together with the audio I/O it drives. The coordinator needs
/// both, because the two are used at different moments: the audio session's
/// category is configured before the start action is fulfilled, while the
/// session's own startup waits for CallKit to activate that audio session.
@MainActor
struct VoiceCallSessionAudio {
    let session: any VoiceCallSession
    let audio: any VoiceAudioIO
}

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
/// the session and configures its audio, its audio activation releases the
/// session's audio engine, and either side ending ends the other. Each direction ends the call
/// exactly once — the call UUID is cleared as the first step of teardown, and
/// every entry point is guarded on it, so a session that finishes because
/// CallKit ended it cannot report the end back a second time.
@MainActor
@Observable
final class VoiceCallCoordinator: VoiceCallEventHandling {
    /// What the system may do with the call, reported as soon as the call
    /// exists. None of the multi-call operations mean anything for an assistant
    /// conversation: it cannot be held or swapped, merged with another call,
    /// taken private, or sent tones. Declaring that is what makes an incoming
    /// phone call end this one, rather than leaving CallKit waiting out a hold
    /// action nothing here answers.
    static func makeCallUpdate() -> CXCallUpdate {
        let update = CXCallUpdate()
        update.supportsHolding = false
        update.supportsGrouping = false
        update.supportsUngrouping = false
        update.supportsDTMF = false
        return update
    }

    private(set) var session: (any VoiceCallSession)?

    private let provider: any CallProviding
    private let controller: any CallRequesting
    private let makeSession: @MainActor (VoiceAudioActivationSignal) -> VoiceCallSessionAudio
    private let handsFreeAccess: VoiceHandsFreeAccess
    private let telemetry: VoiceCallTelemetryRecording
    private let logger = Logger(subsystem: "com.familyassistant.app", category: "voice-call")

    /// Called after an observed session phase change has been handled, including
    /// one belonging to a call that is already torn down. Nothing else marks the
    /// end of that work, so a test asserting that a phase change reported
    /// nothing has no other point to assert from.
    var onSessionPhaseHandled: (@MainActor () -> Void)?

    private var callUUID: UUID?
    private var activationSignal: VoiceAudioActivationSignal?
    private var startupTask: Task<Void, Never>?
    private var didReportConnected = false

    init(
        provider: any CallProviding,
        controller: any CallRequesting,
        handsFreeAccess: VoiceHandsFreeAccess = .system,
        telemetry: VoiceCallTelemetryRecording = VoiceCallTelemetry.shared,
        makeSession: @escaping @MainActor (VoiceAudioActivationSignal) -> VoiceCallSessionAudio
    ) {
        self.provider = provider
        self.controller = controller
        self.handsFreeAccess = handsFreeAccess
        self.telemetry = telemetry
        self.makeSession = makeSession
    }

    /// Build a coordinator against the real CallKit provider and controller,
    /// running the same session machinery as the in-app Voice tab on an
    /// externally-managed audio engine.
    ///
    /// `profileID` is read when a call is actually placed rather than captured
    /// here, because the coordinator is built once and outlives any number of
    /// calls: a profile the user chooses later must reach the next call without
    /// waiting for a relaunch.
    static func system(
        authManager: AuthManager,
        profileID: @escaping @MainActor () -> String? = { nil }
    ) -> VoiceCallCoordinator {
        let systemProvider = SystemCallProvider()
        let coordinator = VoiceCallCoordinator(
            provider: systemProvider,
            controller: SystemCallController(),
            makeSession: { signal in
                let api = ChatAPIClient(authManager: authManager)
                let audio = VoiceAudioEngine(activation: .externallyManaged(signal))
                return VoiceCallSessionAudio(
                    session: VoiceSessionViewModel(
                        tokenProvider: api,
                        toolExecutor: api,
                        transcriptStore: api,
                        audio: audio,
                        profileID: profileID()
                    ),
                    audio: audio
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
            telemetry.record(
                "Refused a hands-free assistant call: device locked, not on CarPlay",
                component: VoiceCallTelemetryComponent.handsFreeRefused
            )
            return
        }
        guard callUUID == nil else { throw VoiceCallError.callAlreadyInProgress }
        let uuid = UUID()
        callUUID = uuid
        let handle = CXHandle(type: .generic, value: AssistantCallHandle.value)
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

    /// Fulfilling the start action is what lets CallKit activate the audio
    /// session, so everything that must be true of that audio session happens
    /// first. The category is the whole of it: activating on the default
    /// category gives a call with no audio, and the session's own startup —
    /// microphone permission, a token fetch, the socket — is far too slow to sit
    /// in front of it, which is why it stays behind the activation callback.
    ///
    /// This is also where the call path's success is recorded, because this is
    /// the only place it is known. CallKit accepts the transaction long before
    /// the start action runs, so whoever asked for the call has already been
    /// told the request went through while the audio configuration that can
    /// still fail it has not happened yet. Every exit from here therefore
    /// leaves a breadcrumb, and only the one that fulfilled the action and
    /// started the session says a call began.
    func performStartCall(uuid: UUID, action: any CallAction) {
        guard uuid == callUUID else {
            telemetry.record(
                "Failed a start action for a call this coordinator is not running",
                component: VoiceCallTelemetryComponent.startFailed,
                extraData: ["reason": "unknown_call"]
            )
            action.fail()
            return
        }
        provider.reportOutgoingCall(with: uuid, startedConnectingAt: Date())
        provider.reportCall(with: uuid, updated: Self.makeCallUpdate())

        let signal = VoiceAudioActivationSignal()
        let built = makeSession(signal)
        do {
            try built.audio.configureAudioSession()
        } catch {
            logger.error(
                "Could not configure the call's audio session: \(error.localizedDescription, privacy: .public)"
            )
            ErrorReporter.shared.report(error, component: "Voice.call.audioConfiguration")
            telemetry.record(
                "Failed a start action: the call's audio session could not be configured",
                component: VoiceCallTelemetryComponent.startFailed,
                extraData: ["reason": "audio_configuration"]
            )
            callUUID = nil
            action.fail()
            return
        }

        activationSignal = signal
        didReportConnected = false
        session = built.session
        action.fulfill()

        observePhase(of: built.session, uuid: uuid)
        startupTask = Task { await built.session.start() }
        telemetry.record("iOS started a call", component: VoiceCallTelemetryComponent.started)
    }

    func performEndCall(uuid: UUID, action: any CallAction) {
        // The call is already gone from CallKit's point of view, so the action is
        // fulfilled whether or not it matches the call we are running.
        if uuid == callUUID {
            teardown(uuid: uuid, reporting: nil)
        }
        action.fulfill()
    }

    /// The system call screen — including a CarPlay head unit — is the only
    /// mute control over a call, so its state is pushed straight onto the
    /// session, which gates microphone forwarding on it.
    func performSetMuted(uuid: UUID, muted: Bool, action: any CallAction) {
        guard uuid == callUUID else {
            action.fail()
            return
        }
        session?.isMuted = muted
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
        defer { onSessionPhaseHandled?() }
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
    ///
    /// The startup task is cancelled rather than left to finish: the session's
    /// audio engine parks on CallKit's activation, and a call that ends before
    /// that arrives would otherwise leave the task suspended forever, holding
    /// the session, with the signal that could resume it already released.
    /// Cancelling resumes the wait with a `CancellationError`, and because the
    /// call UUID is cleared first, whatever phase the session then settles into
    /// cannot re-enter teardown or report the call's end a second time.
    private func teardown(uuid: UUID, reporting reason: CXCallEndedReason?) {
        guard callUUID == uuid else { return }
        callUUID = nil
        startupTask?.cancel()
        startupTask = nil
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
