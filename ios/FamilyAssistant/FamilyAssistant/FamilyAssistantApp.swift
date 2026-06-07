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
}
