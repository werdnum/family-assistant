import SwiftUI

@main
struct WatchVoiceApp: App {
    @State private var auth = AuthManager()

    var body: some Scene {
        WindowGroup {
            WatchVoiceView()
                .environment(auth)
                .task {
                    ErrorReporter.shared.configure(
                        baseURLProvider: { auth.validatedServerURL() },
                        authTokenProvider: { try await auth.validAccessTokenIfPresent() }
                    )
                    await auth.bootstrapSession()
                    await ErrorReporter.shared.flushPersisted()
                }
        }
    }
}
