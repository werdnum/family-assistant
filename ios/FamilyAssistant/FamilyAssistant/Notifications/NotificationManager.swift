import Foundation
import Observation
import os
import UIKit
import UserNotifications

@MainActor
@Observable
final class NotificationManager {
    enum RegistrationState: String {
        case disabled
        case registering
        case registered
        case failed
    }

    var authorizationStatus: UNAuthorizationStatus = .notDetermined
    var registrationState: RegistrationState = .disabled
    var notificationsEnabled: Bool {
        didSet {
            UserDefaults.standard.set(notificationsEnabled, forKey: Keys.notificationsEnabled)
        }
    }
    var errorMessage: String?
    var pendingNavigationPath: String?
    var pendingConfirmationModal: PendingConfirmationModal?
    // A push that arrived while the app is foregrounded. Mirrors
    // `pendingNavigationPath`: an @Observable hint `ContentView` observes and hands
    // to the chat view model for a targeted conversation + list refresh (§4.6).
    // Carries a fresh id so a repeat push for the same conversation still fires the
    // `onChange`.
    var pendingPushHint: PushHint?

    @ObservationIgnored private weak var authManager: AuthManager?
    @ObservationIgnored private let logger = Logger(
        subsystem: "com.familyassistant.app",
        category: "notifications"
    )

    private enum Keys {
        static let notificationsEnabled = "fa_notifications_enabled"
        static let deviceToken = "fa_apns_device_token"
        static let installationID = "fa_installation_id"
    }

    private enum Actions {
        static let approve = "FAMILY_ASSISTANT_APPROVE"
        static let deny = "FAMILY_ASSISTANT_DENY"
    }

    private enum Categories {
        static let confirmation = "FAMILY_ASSISTANT_CONFIRMATION"
        static let message = "FAMILY_ASSISTANT_MESSAGE"
    }

    init() {
        notificationsEnabled = UserDefaults.standard.bool(forKey: Keys.notificationsEnabled)
        registrationState = notificationsEnabled ? .registering : .disabled
        #if DEBUG
        pendingNavigationPath = UITestConfiguration.initialNavigationPath
        #endif
    }

    static func registerNotificationCategories() {
        let approve = UNNotificationAction(
            identifier: Actions.approve,
            title: "Approve",
            options: [.authenticationRequired]
        )
        let deny = UNNotificationAction(
            identifier: Actions.deny,
            title: "Deny",
            options: [.authenticationRequired, .destructive]
        )
        let confirmation = UNNotificationCategory(
            identifier: Categories.confirmation,
            actions: [approve, deny],
            intentIdentifiers: [],
            options: []
        )
        let message = UNNotificationCategory(
            identifier: Categories.message,
            actions: [],
            intentIdentifiers: [],
            options: []
        )
        UNUserNotificationCenter.current().setNotificationCategories([confirmation, message])
    }

    func bind(authManager: AuthManager) {
        self.authManager = authManager
    }

    func refreshAuthorizationStatus() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        authorizationStatus = settings.authorizationStatus
        if authorizationStatus == .denied {
            registrationState = .failed
        } else if !notificationsEnabled {
            registrationState = .disabled
        }
    }

    func syncRegistrationIfNeeded() async {
        await refreshAuthorizationStatus()
        guard notificationsEnabled else {
            registrationState = .disabled
            return
        }
        guard canRegisterForRemoteNotifications else {
            return
        }

        registrationState = .registering
        UIApplication.shared.registerForRemoteNotifications()

        if let token = storedDeviceToken {
            await syncDeviceTokenWithServer(token)
        }
    }

    func enableNotifications(authManager: AuthManager) async {
        bind(authManager: authManager)
        notificationsEnabled = true
        errorMessage = nil
        registrationState = .registering

        do {
            let granted = try await UNUserNotificationCenter.current().requestAuthorization(
                options: [.alert, .badge, .sound]
            )
            await refreshAuthorizationStatus()
            guard granted else {
                notificationsEnabled = false
                registrationState = .failed
                errorMessage = "Notification permission was not granted."
                return
            }
            UIApplication.shared.registerForRemoteNotifications()
            if let token = storedDeviceToken {
                await syncDeviceTokenWithServer(token)
            }
        } catch {
            notificationsEnabled = false
            registrationState = .failed
            errorMessage = error.localizedDescription
            ErrorReporter.shared.report(error, component: "Notifications.enable")
        }
    }

    func disableNotifications(authManager: AuthManager) async {
        bind(authManager: authManager)
        notificationsEnabled = false
        registrationState = .disabled
        errorMessage = nil
        UIApplication.shared.unregisterForRemoteNotifications()

        if let token = storedDeviceToken {
            do {
                try await unregisterDeviceTokenFromServer(token)
            } catch {
                logger.warning(
                    "Failed to unregister APNs token: \(error.localizedDescription, privacy: .public)"
                )
                errorMessage = "Notifications were disabled locally, but the server could not be updated."
                ErrorReporter.shared.report(error, component: "Notifications.disable")
            }
        }

        storedDeviceToken = nil
    }

    func openSystemNotificationSettings() {
        guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
        UIApplication.shared.open(url)
    }

    func handleAPNsRegistration(deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02.2hhx", $0) }.joined()
        storedDeviceToken = token
        guard notificationsEnabled else { return }

        Task {
            await syncDeviceTokenWithServer(token)
        }
    }

    func handleAPNsRegistrationFailure(_ error: Error) {
        registrationState = .failed
        errorMessage = error.localizedDescription
        ErrorReporter.shared.report(error, component: "Notifications.apnsRegistration")
        logger.error("APNs registration failed: \(error.localizedDescription, privacy: .public)")
    }

    /// Publish a foreground push hint from `AppDelegate`'s `willPresent`. The OS
    /// still presents the banner; this only mirrors the payload's `conversation_id`
    /// into an @Observable value so the chat view model can do a targeted refresh
    /// (§4.6). Silent-push/background refresh stays out of scope (§4.8).
    func handleForegroundPushPresentation(userInfo: [AnyHashable: Any]) {
        pendingPushHint = PushHint(conversationID: stringValue(userInfo["conversation_id"]))
    }

    func clearPendingPushHint() {
        pendingPushHint = nil
    }

    func handleNotificationResponse(_ response: UNNotificationResponse) {
        let content = response.notification.request.content
        handleNotificationAction(
            actionIdentifier: response.actionIdentifier,
            categoryIdentifier: content.categoryIdentifier,
            title: content.title,
            body: content.body,
            userInfo: content.userInfo
        )
    }

    /// Route a notification response to confirmation submission, a confirmation modal, or a deep
    /// link. Exposed separately from ``handleNotificationResponse(_:)`` because
    /// ``UNNotificationResponse`` cannot be constructed directly in unit tests.
    func handleNotificationAction(
        actionIdentifier: String,
        categoryIdentifier: String,
        title: String,
        body: String,
        userInfo: [AnyHashable: Any]
    ) {
        switch actionIdentifier {
        case Actions.approve:
            submitConfirmation(from: userInfo, approved: true)
        case Actions.deny:
            submitConfirmation(from: userInfo, approved: false)
        case UNNotificationDefaultActionIdentifier:
            // Tapping the notification body opens the in-app confirmation modal when the
            // notification is a confirmation; otherwise it deep-links like any other notification.
            if categoryIdentifier == Categories.confirmation,
               let requestID = stringValue(userInfo["request_id"])
            {
                pendingConfirmationModal = PendingConfirmationModal(
                    requestID: requestID,
                    conversationID: stringValue(userInfo["conversation_id"]),
                    title: title,
                    body: body
                )
            } else if let path = navigationPath(from: userInfo) {
                pendingNavigationPath = path
            }
        default:
            break
        }
    }

    func handleDeepLink(_ url: URL) -> Bool {
        if let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
           let path = components.queryItems?.first(where: { $0.name == "path" })?.value,
           let normalized = normalizeNavigationPath(path)
        {
            pendingNavigationPath = normalized
            return true
        }

        if url.scheme == "familyassistant", let host = url.host {
            let rawPath = "/\(host)\(url.path)"
            var components = URLComponents()
            components.path = rawPath
            // `url.query` is already percent-encoded; assigning it to `query`
            // (which expects an unencoded string) double-encodes it, turning
            // familyassistant://chat?q=fix%20this into the literal prompt
            // "fix%20this".
            components.percentEncodedQuery = url.query
            if let path = normalizeNavigationPath(components.string ?? rawPath) {
                pendingNavigationPath = path
                return true
            }
        }

        if let path = normalizeNavigationPath(url.path) {
            pendingNavigationPath = path
            return true
        }

        return false
    }

    func clearPendingNavigationPath() {
        pendingNavigationPath = nil
    }

    func clearPendingConfirmationModal() {
        pendingConfirmationModal = nil
    }

    var statusLabel: String {
        switch authorizationStatus {
        case .authorized, .ephemeral, .provisional:
            switch registrationState {
            case .disabled:
                return "Off"
            case .registering:
                return "Registering"
            case .registered:
                return "On"
            case .failed:
                return "Needs attention"
            }
        case .denied:
            return "Denied"
        case .notDetermined:
            return notificationsEnabled ? "Permission needed" : "Off"
        @unknown default:
            return "Unknown"
        }
    }

    private var canRegisterForRemoteNotifications: Bool {
        switch authorizationStatus {
        case .authorized, .ephemeral, .provisional:
            return true
        case .denied, .notDetermined:
            return false
        @unknown default:
            return false
        }
    }

    private var storedDeviceToken: String? {
        get {
            UserDefaults.standard.string(forKey: Keys.deviceToken)
        }
        set {
            UserDefaults.standard.set(newValue, forKey: Keys.deviceToken)
        }
    }

    private func syncDeviceTokenWithServer(_ token: String) async {
        do {
            try await registerDeviceTokenWithServer(token)
            registrationState = .registered
            errorMessage = nil
        } catch {
            registrationState = .failed
            errorMessage = error.localizedDescription
            ErrorReporter.shared.report(error, component: "Notifications.tokenSync")
            logger.warning(
                "Failed to sync APNs token: \(error.localizedDescription, privacy: .public)"
            )
        }
    }

    private func registerDeviceTokenWithServer(_ token: String) async throws {
        guard let authManager else { throw NotificationError.notAuthenticated }
        guard let baseURL = authManager.validatedServerURL() else {
            throw NotificationError.invalidServerURL
        }
        var request = try await authManager.authorizedRequest(
            url: baseURL.appendingPathComponent("api/ios/push-tokens"),
            method: "POST"
        )

        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            PushTokenRegistrationRequest(
                token: token,
                environment: apnsEnvironment,
                bundleID: Bundle.main.bundleIdentifier ?? "",
                installationID: installationID,
                deviceName: UIDevice.current.name
            )
        )

        try await performRequest(request)
    }

    private func unregisterDeviceTokenFromServer(_ token: String) async throws {
        guard let authManager else { throw NotificationError.notAuthenticated }
        guard let baseURL = authManager.validatedServerURL() else {
            throw NotificationError.invalidServerURL
        }
        var request = try await authManager.authorizedRequest(
            url: baseURL
                .appendingPathComponent("api/ios/push-tokens")
                .appendingPathComponent(token),
            method: "DELETE"
        )

        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        try await performRequest(request)
    }

    private func submitConfirmation(from userInfo: [AnyHashable: Any], approved: Bool) {
        guard let requestID = stringValue(userInfo["request_id"]) else {
            if let path = navigationPath(from: userInfo) {
                pendingNavigationPath = path
            }
            return
        }

        let conversationID = stringValue(userInfo["conversation_id"])
        Task {
            do {
                try await submitConfirmation(
                    requestID: requestID,
                    conversationID: conversationID,
                    approved: approved
                )
                if let path = navigationPath(from: userInfo) {
                    pendingNavigationPath = path
                }
            } catch {
                errorMessage = error.localizedDescription
                ErrorReporter.shared.report(error, component: "Notifications.confirm")
                if let path = navigationPath(from: userInfo) {
                    pendingNavigationPath = path
                }
            }
        }
    }

    private func submitConfirmation(
        requestID: String,
        conversationID: String?,
        approved: Bool
    ) async throws {
        guard let authManager else { throw NotificationError.notAuthenticated }
        guard let baseURL = authManager.validatedServerURL() else {
            throw NotificationError.invalidServerURL
        }
        var request = try await authManager.authorizedRequest(
            url: baseURL.appendingPathComponent("api/v1/chat/confirm_tool"),
            method: "POST"
        )

        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            ConfirmationActionRequest(
                requestID: requestID,
                approved: approved,
                conversationID: conversationID,
                approvingInterface: "ios"
            )
        )

        try await performRequest(request)
    }

    private func performRequest(_ request: URLRequest) async throws {
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw NotificationError.invalidResponse
        }
        guard 200..<300 ~= httpResponse.statusCode else {
            let body = String(data: data, encoding: .utf8)
            throw NotificationError.server(statusCode: httpResponse.statusCode, body: body)
        }
    }

    private func navigationPath(from userInfo: [AnyHashable: Any]) -> String? {
        if let path = stringValue(userInfo["path"]), let normalized = normalizeNavigationPath(path) {
            return normalized
        }

        if let url = stringValue(userInfo["url"]),
           let normalized = normalizeNavigationPath(url)
        {
            return normalized
        }

        if let conversationID = stringValue(userInfo["conversation_id"]) {
            return "/chat?conversation_id=\(conversationID)"
        }

        return "/chat"
    }

    private func normalizeNavigationPath(_ rawValue: String) -> String? {
        if rawValue.hasPrefix("//") {
            return nil
        }

        if rawValue.hasPrefix("/") {
            return rawValue
        }

        if let url = URL(string: rawValue), url.scheme != nil {
            var components = URLComponents(url: url, resolvingAgainstBaseURL: false)
            components?.scheme = nil
            components?.host = nil
            components?.port = nil
            components?.user = nil
            components?.password = nil
            let path = components?.string ?? url.path
            return path.hasPrefix("/") ? path : "/\(path)"
        }

        return nil
    }

    private func stringValue(_ value: Any?) -> String? {
        value as? String
    }

    private var installationID: String {
        if let stored = UserDefaults.standard.string(forKey: Keys.installationID) {
            return stored
        }
        let generated = UUID().uuidString
        UserDefaults.standard.set(generated, forKey: Keys.installationID)
        return generated
    }

    private var apnsEnvironment: String {
        #if DEBUG
        "sandbox"
        #else
        "production"
        #endif
    }
}

/// A foreground push hint carrying the payload's `conversation_id`. `Identifiable`
/// with a fresh id per push so a repeat notification for the same conversation
/// still triggers the `ContentView` `onChange` that drives the targeted refresh.
struct PushHint: Identifiable, Equatable {
    let id = UUID()
    let conversationID: String?
}

private struct PushTokenRegistrationRequest: Encodable {
    let token: String
    let environment: String
    let bundleID: String
    let installationID: String
    let deviceName: String

    enum CodingKeys: String, CodingKey {
        case token
        case environment
        case bundleID = "bundle_id"
        case installationID = "installation_id"
        case deviceName = "device_name"
    }
}

private struct ConfirmationActionRequest: Encodable {
    let requestID: String
    let approved: Bool
    let conversationID: String?
    let approvingInterface: String

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case approved
        case conversationID = "conversation_id"
        case approvingInterface = "approving_interface"
    }
}

private enum NotificationError: LocalizedError {
    case invalidServerURL
    case invalidResponse
    case notAuthenticated
    case server(statusCode: Int, body: String?)

    var errorDescription: String? {
        switch self {
        case .invalidServerURL:
            "Invalid server URL."
        case .invalidResponse:
            "The server returned an invalid response."
        case .notAuthenticated:
            "Sign in before enabling notifications."
        case .server(let statusCode, let body):
            if let body, !body.isEmpty {
                "Notification server request failed with status \(statusCode): \(body)"
            } else {
                "Notification server request failed with status \(statusCode)."
            }
        }
    }
}
