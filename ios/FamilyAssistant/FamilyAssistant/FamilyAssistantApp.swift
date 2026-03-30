import SwiftUI

@main
struct FamilyAssistantApp: App {
    @State private var authManager = AuthManager()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(authManager)
                .onOpenURL { url in
                    // Handle Universal Links callback from ASWebAuthenticationSession
                    Task { @MainActor in
                        await authManager.handleCallback(url: url)
                    }
                }
        }
    }
}
