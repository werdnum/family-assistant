import SwiftUI

/// A focused web page rendered inside a feature tab's `NavigationStack`.
///
/// Wraps `WebViewContainer` with its own `WebViewState`, a navigation title
/// driven by the page (falling back to a caller-supplied label), and same-origin
/// link interception that pushes within the current tab via the `AppRouter`.
/// System back (the navigation stack) and pull-to-refresh replace the old
/// bottom `WebViewToolbar`.
struct WebDestinationView: View {
    let path: String
    let baseURL: URL
    let currentTab: AppTab
    let fallbackTitle: String
    let appRouter: AppRouter

    @Environment(AuthManager.self) private var authManager
    @Environment(\.scenePhase) private var scenePhase
    @State private var sessionReady = false
    @State private var sessionError: String?
    @State private var retryID = 0
    @State private var authObserver: UUID?
    @State private var webViewState = WebViewState()

    private var url: URL {
        EmbeddedWebRoute.pageURL(
            URL(string: path, relativeTo: baseURL)?.absoluteURL ?? baseURL,
            relativeTo: baseURL
        )
    }

    var body: some View {
        Group {
            if let sessionError {
                ContentUnavailableView {
                    Label("Couldn’t open page", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(sessionError)
                } actions: {
                    if authManager.authRequired {
                        Button("Sign in") { authManager.login() }
                    } else {
                        Button("Retry") { retryID += 1 }
                    }
                }
            } else if sessionReady {
                WebViewContainer(
                    url: url,
                    serverBaseURL: baseURL,
                    webViewState: webViewState
                ) { tappedURL in
                    appRouter.followWebLink(tappedURL, from: currentTab, relativeTo: baseURL)
                }
            } else {
                ProgressView("Connecting…")
            }
        }
        .task(id: "\(scenePhase)-\(retryID)") {
            guard scenePhase == .active else { return }
            do {
                sessionError = nil
                try await prepareSession()
                try Task.checkCancellation()
                sessionReady = true
                while !Task.isCancelled {
                    try await Task.sleep(for: .seconds(30))
                    try await prepareSession()
                }
            } catch is CancellationError {
                // Disappearance and backgrounding cancel credential maintenance.
            } catch {
                sessionReady = false
                sessionError = error.localizedDescription
                ErrorReporter.shared.report(error, component: "WebView.session")
            }
        }
        .onAppear {
            authObserver = authManager.addAuthStateObserver { signal in
                if signal == .ok || signal == .authRequired { retryID += 1 }
            }
        }
        .onDisappear {
            if let authObserver { authManager.removeAuthStateObserver(authObserver) }
            authObserver = nil
        }
        .navigationTitle(webViewState.title.isEmpty ? fallbackTitle : webViewState.title)
        .navigationBarTitleDisplayMode(.inline)
    }

    @MainActor
    private func prepareSession() async throws {
        var bridgeError: Error?
        let completed = await authManager.runWithWatchdog(seconds: authManager.bootstrapWatchdogSeconds) {
            do {
                try await authManager.prepareWebSession()
            } catch {
                bridgeError = error
            }
        }
        guard completed else { throw AuthError.transient(underlying: nil) }
        if let bridgeError { throw bridgeError }
    }

}
