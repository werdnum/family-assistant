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
