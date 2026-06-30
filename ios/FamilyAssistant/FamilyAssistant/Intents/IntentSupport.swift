import Foundation
import Observation

/// Errors surfaced to Siri / the Shortcuts app when an App Intent cannot run.
///
/// `AppIntent.perform()` presents a thrown error's `localizedDescription` to the
/// user, so these messages are written to be read aloud or shown in a sheet.
enum AssistantIntentError: Error, LocalizedError {
    /// No stored credentials. A background intent cannot run the interactive
    /// `ASWebAuthenticationSession` login, so we ask the user to open the app.
    case needsSignIn
    case requestFailed(String)

    var errorDescription: String? {
        switch self {
        case .needsSignIn:
            "Open Family Assistant and sign in before using this shortcut."
        case .requestFailed(let detail):
            detail
        }
    }
}

/// Shared helpers for the App Intents layer.
///
/// All credential and server-URL access flows through `AuthManager`, keeping a
/// single source of truth. In-app intents run in the app's own process, so they
/// read the same Keychain item and `UserDefaults` the UI uses — no App Group or
/// Keychain access group is required. (A future Share Extension or Widget would
/// run out-of-process and need those; the deep-link construction here is
/// centralized so it can be reused from such a target.)
enum IntentSupport {
    /// Matches the default profile selected by the native chat UI
    /// (`ChatViewModel`), so intent turns route through the same profile.
    static let defaultProfileID = "default_assistant"

    /// Constrained profile for processing untrusted shared content (Quick
    /// Capture). It denies destructive/browser/delegation tools and gates writes
    /// behind confirmation, and treats the supplied content as untrusted — the
    /// Rule-of-Two posture for content the user did not author. See the backend
    /// `email_intake` service profile.
    static let captureProfileID = "email_intake"

    private static let deepLinkScheme = "familyassistant"

    /// Builds an `AuthManager` only if the user is signed in, otherwise throws
    /// `AssistantIntentError.needsSignIn`. `AuthManager()` reads the server URL from
    /// `UserDefaults` and sets `isAuthenticated` from the stored Keychain token.
    @MainActor
    static func makeAuthenticatedManager() throws -> AuthManager {
        let manager = AuthManager()
        guard !manager.serverURL.isEmpty, manager.isAuthenticated else {
            throw AssistantIntentError.needsSignIn
        }
        return manager
    }

    /// A fresh conversation ID using the same prefix as the native chat client so
    /// intent-started conversations appear in the Chat tab's conversation list.
    static func newConversationID() -> String {
        "\(ChatConstants.conversationPrefix)\(UUID().uuidString)"
    }

    /// Wraps captured content with an instruction asking the assistant to file it.
    static func quickCapturePrompt(for content: String) -> String {
        """
        Please save this for me and file it wherever is most appropriate:

        \(content)
        """
    }

    /// A `familyassistant://chat` deep link. A non-empty `prompt` is delivered as
    /// the `q` query item, which the app auto-sends on open; `conversationID`
    /// opens an existing thread. See `NotificationManager.handleDeepLink` and
    /// `ChatRoute.route`.
    static func chatDeepLink(prompt: String? = nil, conversationID: String? = nil) -> URL {
        var components = URLComponents()
        components.scheme = deepLinkScheme
        components.host = "chat"

        var items: [URLQueryItem] = []
        if let prompt, !prompt.isEmpty {
            items.append(URLQueryItem(name: "q", value: prompt))
        }
        if let conversationID, !conversationID.isEmpty {
            items.append(URLQueryItem(name: "conversation_id", value: conversationID))
        }
        if !items.isEmpty {
            components.queryItems = items
        }

        // The scheme/host are constants, so URL construction cannot realistically
        // fail; fall back to the bare chat link to keep a non-optional API.
        return components.url ?? URL(string: "\(deepLinkScheme)://chat")!
    }

    /// Sends a prompt to the assistant in a fresh conversation and returns the
    /// reply. Shared by `AskAssistantIntent` and `QuickCaptureIntent`. Throws
    /// `AssistantIntentError` (errors are mapped via ``intentError(from:)``).
    @MainActor
    static func sendAssistantMessage(
        prompt: String,
        profileID: String = defaultProfileID
    ) async throws -> ChatSendResult {
        let manager = try makeAuthenticatedManager()
        do {
            return try await ChatAPIClient(authManager: manager).sendMessage(
                prompt: prompt,
                conversationID: newConversationID(),
                profileID: profileID
            )
        } catch {
            throw intentError(from: error)
        }
    }

    /// Normalizes an error thrown during an intent into an `AssistantIntentError`.
    /// A rejected/cleared session (e.g. an unrefreshable expired token) becomes
    /// `needsSignIn` so the user is told to re-open the app, rather than a raw
    /// "No stored credentials" message.
    static func intentError(from error: Error) -> AssistantIntentError {
        if let intentError = error as? AssistantIntentError {
            return intentError
        }
        if let authError = error as? AuthError {
            switch authError {
            case .authRejected, .noCredentials, .invalidServerURL:
                return .needsSignIn
            case .exchangeFailed, .transient:
                return .requestFailed(authError.localizedDescription)
            }
        }
        return .requestFailed(error.localizedDescription)
    }
}

/// Bridges an `openAppWhenRun` App Intent to the app's router.
///
/// In-app intents run in the app's own process, so a shared singleton is the
/// simplest reliable channel on iOS 17 (`OpenURLIntent` is iOS 18+). The intent
/// records an app-relative path; `ContentView` drains it through the same
/// `AppRouter` flow used for notification deep links. (A future out-of-process
/// Share Extension / Widget would instead open a `familyassistant://` URL — see
/// `IntentSupport.chatDeepLink` — since it cannot share this in-memory state.)
@Observable
@MainActor
final class IntentNavigationCenter {
    static let shared = IntentNavigationCenter()

    /// An app-relative path (e.g. `/chat?q=...`), resolved against the server
    /// base URL by `ContentView`. A non-empty `q` is auto-sent by the chat view.
    private(set) var pendingChatPath: String?

    private init() {}

    func requestChat(prompt: String?) {
        var components = URLComponents()
        components.path = "/chat"
        if let prompt, !prompt.isEmpty {
            components.queryItems = [URLQueryItem(name: "q", value: prompt)]
        }
        pendingChatPath = components.string ?? "/chat"
    }

    func requestNewChat() {
        pendingChatPath = "/chat?new=1"
    }

    /// Returns and clears any pending path.
    func consumePendingChatPath() -> String? {
        defer { pendingChatPath = nil }
        return pendingChatPath
    }
}
