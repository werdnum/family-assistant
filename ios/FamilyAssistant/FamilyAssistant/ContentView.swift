import SwiftUI

struct ContentView: View {
    @Environment(AuthManager.self) private var authManager
    @State private var webViewState = WebViewState()

    var body: some View {
        if authManager.isAuthenticated {
            VStack(spacing: 0) {
                if let url = authManager.validatedServerURL()?.appendingPathComponent("chat") {
                    WebViewContainer(url: url, webViewState: webViewState)
                }

                Divider()

                WebViewToolbar(webViewState: webViewState) {
                    Task { await authManager.logout() }
                }
            }
            .task {
                await authManager.bootstrapSession()
            }
        } else {
            SetupView()
        }
    }
}
