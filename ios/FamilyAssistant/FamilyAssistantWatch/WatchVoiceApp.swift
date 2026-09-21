import SwiftUI

@main
struct WatchVoiceApp: App {
    @State private var auth: AuthManager
    @State private var watchAuthentication: WatchAuthentication

    init() {
        let auth = AuthManager()
        if auth.isAuthenticated, UserDefaults.standard.string(forKey: "fa_paired_phone_session") == nil {
            auth.markAuthRequired()
            auth.isBootstrapping = false
        }
        _auth = State(initialValue: auth)
        _watchAuthentication = State(initialValue: WatchAuthentication(auth: auth))
    }

    var body: some Scene {
        WindowGroup {
            WatchVoiceView()
                .environment(auth)
                .environment(watchAuthentication)
                .task {
                    watchAuthentication.activate()
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
