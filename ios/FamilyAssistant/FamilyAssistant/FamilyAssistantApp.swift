import SwiftUI

@main
struct FamilyAssistantApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var authManager: AuthManager
    @State private var notificationManager: NotificationManager

    init() {
        #if DEBUG
        UITestConfiguration.applyIfNeeded()
        #endif
        _authManager = State(initialValue: AuthManager())
        _notificationManager = State(initialValue: NotificationManager())
    }

    var body: some Scene {
        WindowGroup {
            #if DEBUG
            if UITestConfiguration.isHostingUnitTests {
                // App-hosted unit tests: render nothing. Booting the real UI here
                // would run AuthManager bootstrap and the chat live-events stream
                // against the tests' shared URL mock, leaking requests into
                // unrelated tests. See `UITestConfiguration.isHostingUnitTests`.
                Color.clear
            } else {
                appContent
            }
            #else
            appContent
            #endif
        }
    }

    private var appContent: some View {
        ContentView()
            .environment(authManager)
            .environment(notificationManager)
            .onAppear {
                appDelegate.notificationManager = notificationManager
                notificationManager.bind(authManager: authManager)
            }
            .onOpenURL { url in
                Task { @MainActor in
                    if URLComponents(url: url, resolvingAgainstBaseURL: false)?
                        .queryItems?
                        .contains(where: { $0.name == "code" }) == true
                    {
                        await authManager.handleCallback(url: url)
                    } else {
                        _ = notificationManager.handleDeepLink(url)
                    }
                }
            }
    }
}
