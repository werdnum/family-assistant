import SwiftUI

struct ContentView: View {
    @Environment(AuthManager.self) private var authManager
    @Environment(NotificationManager.self) private var notificationManager
    @State private var webViewState = WebViewState()
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
            } else {
                Group {
                    if let baseURL = authManager.validatedServerURL() {
                        switch appRouter.route {
                        case .web(let path):
                            VStack(spacing: 0) {
                                WebViewContainer(
                                    url: webURL(path: path, baseURL: baseURL),
                                    serverBaseURL: baseURL,
                                    webViewState: webViewState
                                ) { url in
                                    appRouter.openNativeURL(url, relativeTo: baseURL)
                                }
                                .onChange(of: notificationManager.pendingNavigationPath) { _, path in
                                    navigateToPendingNotificationPath(path)
                                }

                                Divider()

                                WebViewToolbar(
                                    webViewState: webViewState,
                                    onShowNotes: { appRouter.openNotesList() },
                                    onLogout: logout
                                )
                            }

                        case .notes(let route):
                            NotesRootView(
                                route: route,
                                onRouteChange: { appRouter.route = .notes($0) },
                                onOpenWebPath: openWebPath,
                                onLogout: logout
                            )
                            .onChange(of: notificationManager.pendingNavigationPath) { _, path in
                                navigateToPendingNotificationPath(path)
                            }
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

    private func webURL(path: String, baseURL: URL) -> URL {
        URL(string: path, relativeTo: baseURL)?.absoluteURL
            ?? baseURL.appendingPathComponent("chat")
    }

    private func openWebPath(_ path: String) {
        guard let baseURL = authManager.validatedServerURL() else {
            return
        }
        let url = webURL(path: path, baseURL: baseURL)
        appRouter.openWebPath(url.pathAndQuery)
        webViewState.load(url)
    }

    private func logout() {
        Task {
            await notificationManager.disableNotifications(authManager: authManager)
            await authManager.logout()
            appRouter.reset()
        }
    }

    private func navigateToPendingNotificationPath(_ path: String?) {
        guard let path,
              let baseURL = authManager.validatedServerURL(),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
        else {
            return
        }

        if !appRouter.openNativeURL(url, relativeTo: baseURL) {
            appRouter.openWebPath(url.pathAndQuery)
            webViewState.load(url)
        }
        notificationManager.clearPendingNavigationPath()
    }
}
