import Foundation
import Observation

/// Turns a delivered start-call request into a call, from process scope.
///
/// The scenario the call path exists for — a locked phone with the app not
/// running — launches the app into the background, where no scene need ever
/// connect. The starter therefore owns everything a call needs (the auth state
/// and the coordinator) outside the view layer, and is installed as the
/// ``VoiceCallRequestCenter`` handler at launch. The center holds it for the
/// life of the process.
///
/// The coordinator is built on first use rather than at launch, so a process
/// that is never asked for a call never creates a `CXProvider`.
@MainActor
@Observable
final class VoiceCallStarter {
    private let isAuthenticated: @MainActor () -> Bool
    private let makeCoordinator: @MainActor () -> VoiceCallCoordinator
    private let telemetry: VoiceCallTelemetryRecording
    private var coordinator: VoiceCallCoordinator?

    /// Whether a call is running right now. The single answer to that question:
    /// the coordinator owns the call, and this is the only way anything outside
    /// the call path — the Voice tab, which must not start a session competing
    /// for the same audio session — can ask.
    var isCallActive: Bool {
        coordinator?.isCallActive ?? false
    }

    init(
        isAuthenticated: @escaping @MainActor () -> Bool,
        makeCoordinator: @escaping @MainActor () -> VoiceCallCoordinator,
        telemetry: VoiceCallTelemetryRecording = VoiceCallTelemetry.shared
    ) {
        self.isAuthenticated = isAuthenticated
        self.makeCoordinator = makeCoordinator
        self.telemetry = telemetry
    }

    convenience init(authManager: AuthManager) {
        self.init(
            isAuthenticated: { authManager.isAuthenticated },
            makeCoordinator: { VoiceCallCoordinator.system(authManager: authManager) }
        )
    }

    /// Take over the center's requests, including any that arrived before now.
    func install(into center: VoiceCallRequestCenter) {
        center.installHandler { [self] _ in
            Task { await startCall() }
        }
    }

    /// End a running call when the user signs out. The call is owned at process
    /// scope and outlives the authenticated UI, so nothing else would stop it:
    /// signing out would revoke the credentials while the conversation carried
    /// on listening and talking to the same session.
    func signedInStateChanged(to isSignedIn: Bool) {
        guard !isSignedIn, let coordinator, coordinator.isCallActive else { return }
        Task {
            do {
                try await coordinator.endCall()
            } catch {
                ErrorReporter.shared.report(error, component: "Voice.call.signOut")
            }
        }
    }

    /// A request that arrives while a call is already running is the same
    /// conversation asked for twice, so it is dropped rather than queued.
    ///
    /// A request from a signed-out app is dropped too — there is no session to
    /// authenticate the voice socket with, and the request arrives on a path
    /// with no channel back to Siri to say so.
    ///
    /// Every outcome leaves a breadcrumb, the call that starts included: a
    /// refusal that records and a success that does not are indistinguishable
    /// from a request that never arrived, which is the question this path is
    /// most often asked.
    func startCall() async {
        telemetry.record(
            "iOS was asked to start a call",
            component: VoiceCallTelemetryComponent.starting,
            extraData: [
                "is_authenticated": String(isAuthenticated()),
                "is_call_active": String(isCallActive),
            ]
        )
        guard isAuthenticated() else {
            telemetry.record(
                "Dropped a start-call request: the app is signed out",
                component: VoiceCallTelemetryComponent.signedOut
            )
            return
        }

        let coordinator = coordinator ?? makeCoordinator()
        self.coordinator = coordinator

        guard !coordinator.isCallActive else {
            telemetry.record(
                "Dropped a start-call request: a call is already running",
                component: VoiceCallTelemetryComponent.duplicate
            )
            return
        }
        do {
            try await coordinator.startCall()
            // The coordinator refuses a hands-free start from a locked phone
            // that is not in a car, and says so itself. Only a call it actually
            // placed is a call that started.
            if coordinator.isCallActive {
                telemetry.record("iOS started a call", component: VoiceCallTelemetryComponent.started)
            }
        } catch {
            ErrorReporter.shared.report(error, component: "Voice.call.start")
        }
    }
}
