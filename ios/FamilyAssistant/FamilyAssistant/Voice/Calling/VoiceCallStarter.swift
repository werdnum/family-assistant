import Foundation

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
final class VoiceCallStarter {
    private let isAuthenticated: @MainActor () -> Bool
    private let makeCoordinator: @MainActor () -> VoiceCallCoordinator
    private var coordinator: VoiceCallCoordinator?

    init(
        isAuthenticated: @escaping @MainActor () -> Bool,
        makeCoordinator: @escaping @MainActor () -> VoiceCallCoordinator
    ) {
        self.isAuthenticated = isAuthenticated
        self.makeCoordinator = makeCoordinator
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

    /// A request that arrives while a call is already running is the same
    /// conversation asked for twice, so it is dropped rather than queued.
    ///
    /// A request from a signed-out app is dropped too — there is no session to
    /// authenticate the voice socket with, and the request arrives on a path
    /// with no channel back to Siri to say so. It leaves a breadcrumb instead,
    /// as a hands-free refusal does.
    func startCall() async {
        guard isAuthenticated() else {
            ErrorReporter.shared.report(
                message: "Dropped a start-call request: the app is signed out",
                component: "Voice.call.signedOut",
                errorType: .component
            )
            return
        }

        let coordinator = coordinator ?? makeCoordinator()
        self.coordinator = coordinator

        guard !coordinator.isCallActive else { return }
        do {
            try await coordinator.startCall()
        } catch {
            ErrorReporter.shared.report(error, component: "Voice.call.start")
        }
    }
}
