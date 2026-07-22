import SwiftUI
import UIKit

struct ContentView: View {
    @Environment(AuthManager.self) private var authManager
    @Environment(NotificationManager.self) private var notificationManager
    @Environment(SharedAttachmentInbox.self) private var sharedAttachmentInbox
    @State private var appRouter = AppRouter()

    var body: some View {
        if authManager.isAuthenticated {
            if authManager.isBootstrapping {
                VStack(spacing: 16) {
                    ProgressView()
                    Text("Signing in…")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .task {
                    await authManager.bootstrapSession()
                }
            } else if let baseURL = authManager.validatedServerURL() {
                RootTabView(
                    appRouter: appRouter,
                    authManager: authManager,
                    baseURL: baseURL,
                    onLogout: logout
                )
                .onChange(of: notificationManager.pendingNavigationPath) { _, path in
                    navigateToPendingNotificationPath(path, baseURL: baseURL)
                }
                .onChange(of: IntentNavigationCenter.shared.pendingNavigationPath) { _, _ in
                    navigateToPendingIntentPath(baseURL: baseURL)
                }
                .onChange(of: sharedAttachmentInbox.pendingBatch?.id) { _, batchID in
                    navigateToPendingSharedAttachmentBatch(batchID)
                }
                .task {
                    notificationManager.bind(authManager: authManager)
                    await notificationManager.syncRegistrationIfNeeded()
                    navigateToPendingNotificationPath(
                        notificationManager.pendingNavigationPath,
                        baseURL: baseURL
                    )
                    navigateToPendingIntentPath(baseURL: baseURL)
                    navigateToPendingSharedAttachmentBatch(sharedAttachmentInbox.pendingBatch?.id)
                }
                .sheet(item: Binding(
                    get: { notificationManager.pendingConfirmationModal },
                    set: { if $0 == nil { notificationManager.clearPendingConfirmationModal() } }
                )) { modal in
                    ConfirmationModalView(
                        request: modal,
                        authManager: authManager,
                        onFinished: { notificationManager.clearPendingConfirmationModal() }
                    )
                }
                // A terminal auth failure clears the tokens but keeps the shell
                // mounted (so re-auth is in-place, not a full teardown). Present a
                // global sign-in affordance over any tab: without it, a rejection
                // reached while off the Chat tab would strand the user in an
                // authenticated-looking shell with no way to recover.
                .fullScreenCover(isPresented: Binding(
                    get: { authManager.authRequired },
                    set: { _ in }
                )) {
                    ReauthenticationPromptView(authManager: authManager, onLogout: logout)
                }
            }
        } else {
            SetupView()
        }
    }

    private func logout() {
        Task {
            await notificationManager.disableNotifications(authManager: authManager)
            await authManager.logout()
            appRouter.reset()
        }
    }

    private func navigateToPendingNotificationPath(_ path: String?, baseURL: URL) {
        guard let path,
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
        else {
            return
        }

        if !appRouter.navigate(to: url, relativeTo: baseURL) {
            // Foreign-origin deep link: hand off to the system browser.
            UIApplication.shared.open(url)
        }
        notificationManager.clearPendingNavigationPath()
    }

    private func navigateToPendingIntentPath(baseURL: URL) {
        guard let path = IntentNavigationCenter.shared.consumePendingNavigationPath(),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
        else {
            return
        }
        appRouter.navigate(to: url, relativeTo: baseURL)
    }

    private func navigateToPendingSharedAttachmentBatch(_ batchID: String?) {
        guard let batchID else {
            return
        }
        appRouter.openSharedAttachments(batchID: batchID)
    }
}

/// Global "sign in again" prompt shown over the mounted shell when the server has
/// rejected the stored credentials (`authManager.authRequired`). Re-running
/// `login()` re-presents the authentication session without tearing down app
/// state; on success `AuthManager` clears `authRequired` and this cover dismisses.
private struct ReauthenticationPromptView: View {
    let authManager: AuthManager
    let onLogout: () -> Void

    var body: some View {
        VStack(spacing: 20) {
            Spacer()
            Image(systemName: "person.crop.circle.badge.exclamationmark")
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            Text("Sign in again")
                .font(.title2.weight(.semibold))
            Text("Your session expired. Sign in again to keep using Family Assistant.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if let error = authManager.errorMessage {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
            }
            Spacer()
            Button(action: { authManager.login() }) {
                if authManager.isLoading {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                } else {
                    Text("Sign in again")
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(authManager.isLoading)
            Button("Log out", role: .destructive, action: onLogout)
                .disabled(authManager.isLoading)
        }
        .padding(24)
        .interactiveDismissDisabled()
    }
}
