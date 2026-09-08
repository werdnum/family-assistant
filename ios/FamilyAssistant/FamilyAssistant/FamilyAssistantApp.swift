import SwiftUI

@main
struct FamilyAssistantApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var authManager: AuthManager
    @State private var notificationManager: NotificationManager
    @State private var watchAuthentication: WatchAuthentication
    @State private var sharedAttachmentInbox = SharedAttachmentInbox()

    init() {
        #if DEBUG
        UITestConfiguration.applyIfNeeded()
        let authManager = UITestConfiguration.isEnabled
            ? AuthManager(websiteDataCleaner: {})
            : AuthManager()
        #else
        let authManager = AuthManager()
        #endif
        _authManager = State(initialValue: authManager)
        _watchAuthentication = State(initialValue: WatchAuthentication(auth: authManager))
        _notificationManager = State(initialValue: NotificationManager())

        ErrorReporter.shared.configure(
            baseURLProvider: { [weak authManager] in authManager?.validatedServerURL() },
            authTokenProvider: { [weak authManager] in
                try await authManager?.validAccessTokenIfPresent()
            }
        )
        ErrorReporter.shared.installGlobalHandlers()

        // Siri's start-call intent is delivered before any scene connects, and
        // on a background launch from a locked phone no scene connects at all,
        // so the thing that turns a request into a call is installed here
        // rather than driven from the view layer.
        #if DEBUG
        if !UITestConfiguration.isEnabled, !UITestConfiguration.isHostingUnitTests {
            VoiceCallStarter(authManager: authManager).install(into: .shared)
        }
        #else
        VoiceCallStarter(authManager: authManager).install(into: .shared)
        #endif
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
            .environment(sharedAttachmentInbox)
            .onAppear {
                watchAuthentication.activate()
                appDelegate.notificationManager = notificationManager
                notificationManager.bind(authManager: authManager)
            }
            .onChange(of: authManager.watchPairingIdentity) {
                watchAuthentication.publishPhoneState()
            }
            .task {
                await ErrorReporter.shared.flushPersisted()
            }
            // URL opens arrive via OpenURLCenter, not `.onOpenURL`: the custom
            // scene delegate installed for home-screen quick actions
            // (`HomeScreenShortcutSceneDelegate`) replaces SwiftUI's internal
            // scene delegate, so `.onOpenURL` never fires. The `.task` drains
            // URLs delivered before the first render (cold launches); the
            // `.onChange` drains later arrivals.
            .task {
                await dispatchOpenedURLs()
            }
            .onChange(of: OpenURLCenter.shared.pendingURLs) {
                Task { @MainActor in
                    await dispatchOpenedURLs()
                }
            }
    }

    private func dispatchOpenedURLs() async {
        for url in OpenURLCenter.shared.consumePendingURLs() {
            if SharedAttachmentInbox.canReceive(url) {
                sharedAttachmentInbox.receive(urls: [url])
                continue
            }
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
