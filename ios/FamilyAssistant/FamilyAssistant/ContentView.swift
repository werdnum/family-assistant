import SwiftUI

struct ContentView: View {
    @Environment(AuthManager.self) private var authManager
    @Environment(NotificationManager.self) private var notificationManager
    @State private var webViewState = WebViewState()

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
            } else {
                VStack(spacing: 0) {
                    if let url = authManager.validatedServerURL()?.appendingPathComponent("chat") {
                        WebViewContainer(url: url, webViewState: webViewState)
                            .onChange(of: notificationManager.pendingNavigationPath) { _, path in
                                navigateToPendingNotificationPath(path)
                            }
                    }

                    Divider()

                    WebViewToolbar(webViewState: webViewState) {
                        Task {
                            await notificationManager.disableNotifications(authManager: authManager)
                            await authManager.logout()
                        }
                    }
                }
                .task {
                    notificationManager.bind(authManager: authManager)
                    await notificationManager.syncRegistrationIfNeeded()
                    navigateToPendingNotificationPath(notificationManager.pendingNavigationPath)
                }
            }
        } else {
            SetupView()
        }
    }

    private func navigateToPendingNotificationPath(_ path: String?) {
        guard let path,
              let baseURL = authManager.validatedServerURL(),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
        else {
            return
        }
        webViewState.load(url)
        notificationManager.clearPendingNavigationPath()
    }
}
