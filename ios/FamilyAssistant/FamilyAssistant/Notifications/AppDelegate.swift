import UIKit
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    weak var notificationManager: NotificationManager?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        NotificationManager.registerNotificationCategories()
        if let shortcutItem = launchOptions?[.shortcutItem] as? UIApplicationShortcutItem {
            return HomeScreenShortcutRouter.handle(shortcutItem)
        }
        return true
    }

    func application(
        _ application: UIApplication,
        configurationForConnecting connectingSceneSession: UISceneSession,
        options: UIScene.ConnectionOptions
    ) -> UISceneConfiguration {
        let configuration = UISceneConfiguration(
            name: connectingSceneSession.configuration.name,
            sessionRole: connectingSceneSession.role
        )
        configuration.delegateClass = HomeScreenShortcutSceneDelegate.self
        return configuration
    }

    func application(
        _ application: UIApplication,
        performActionFor shortcutItem: UIApplicationShortcutItem,
        completionHandler: @escaping (Bool) -> Void
    ) {
        completionHandler(HomeScreenShortcutRouter.handle(shortcutItem))
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in
            notificationManager?.handleAPNsRegistration(deviceToken: deviceToken)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        Task { @MainActor in
            notificationManager?.handleAPNsRegistrationFailure(error)
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        let userInfo = notification.request.content.userInfo
        await MainActor.run {
            notificationManager?.handleForegroundPushPresentation(userInfo: userInfo)
        }
        return [.banner, .list, .sound, .badge]
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        await MainActor.run {
            notificationManager?.handleNotificationResponse(response)
        }
    }
}

/// Installed via `UISceneConfiguration.delegateClass`, which replaces SwiftUI's
/// internal scene delegate. That internal delegate is what feeds `.onOpenURL`,
/// so this class must forward every URL-open event (Universal Links,
/// custom-scheme deep links, and file "Open in Family Assistant" hand-offs)
/// into `OpenURLCenter`, from which `FamilyAssistantApp` dispatches them.
/// Dropping any hook silently breaks an external URL delivery path.
final class HomeScreenShortcutSceneDelegate: NSObject, UIWindowSceneDelegate {
    func scene(
        _ scene: UIScene,
        willConnectTo session: UISceneSession,
        options connectionOptions: UIScene.ConnectionOptions
    ) {
        if let shortcutItem = connectionOptions.shortcutItem {
            _ = HomeScreenShortcutRouter.handle(shortcutItem)
        }
        Self.forwardOpenedURLs(connectionOptions.urlContexts)
        Self.forwardUserActivities(connectionOptions.userActivities)
    }

    func windowScene(
        _ windowScene: UIWindowScene,
        performActionFor shortcutItem: UIApplicationShortcutItem,
        completionHandler: @escaping (Bool) -> Void
    ) {
        completionHandler(HomeScreenShortcutRouter.handle(shortcutItem))
    }

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        Self.forwardOpenedURLs(URLContexts)
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        Self.forwardUserActivities([userActivity])
    }

    private static func forwardOpenedURLs(_ contexts: Set<UIOpenURLContext>) {
        forwardOpenedURLs(contexts.map(\.url))
    }

    static func forwardUserActivities(_ activities: Set<NSUserActivity>) {
        forwardOpenedURLs(activities.compactMap { activity in
            guard activity.activityType == NSUserActivityTypeBrowsingWeb else {
                return nil
            }
            return activity.webpageURL
        })
    }

    static func forwardOpenedURLs(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        if Thread.isMainThread {
            MainActor.assumeIsolated {
                OpenURLCenter.shared.receive(urls)
            }
        } else {
            Task { @MainActor in
                OpenURLCenter.shared.receive(urls)
            }
        }
    }
}

enum HomeScreenShortcutRouter {
    static let newChatType = "com.familyassistant.app.new-chat"
    static let voiceType = "com.familyassistant.app.voice"

    static func handle(_ shortcutItem: UIApplicationShortcutItem) -> Bool {
        let action: @MainActor () -> Void
        switch shortcutItem.type {
        case newChatType:
            action = { IntentNavigationCenter.shared.requestNewChat() }
        case voiceType:
            action = { IntentNavigationCenter.shared.requestVoice() }
        default:
            return false
        }

        if Thread.isMainThread {
            MainActor.assumeIsolated {
                action()
            }
        } else {
            Task { @MainActor in
                action()
            }
        }
        return true
    }
}
