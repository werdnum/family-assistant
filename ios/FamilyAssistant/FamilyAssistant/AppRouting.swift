import Foundation

/// Top-level destinations shown in the root `TabView`. Tabs are features, not
/// implementations: Chat and Notes are native, Documents is a focused web
/// surface, and More hosts the long-tail destinations (Events, Voice, …).
enum AppTab: String, CaseIterable, Hashable {
    case chat
    case notes
    case documents
    case more
}

/// Selection state for the native Chat tab. Mirrors the `/chat` web route.
struct ChatRoute: Equatable {
    var conversationID: String?
    var initialPrompt: String?

    static func route(for url: URL, relativeTo baseURL: URL) -> ChatRoute? {
        guard url.matchesOrigin(of: baseURL),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else {
            return nil
        }

        let path = components.percentEncodedPath
        guard path == "/chat" || path == "/chat/" else {
            return nil
        }

        let conversationID = components.queryItems?.first { $0.name == "conversation_id" }?.value
        let initialPrompt = components.queryItems?.first { $0.name == "q" }?.value
        return ChatRoute(conversationID: conversationID, initialPrompt: initialPrompt)
    }
}

enum NotesRoute: Equatable, Hashable {
    case list
    case detail(title: String)
    case add
    case edit(title: String)

    static func route(for url: URL, relativeTo baseURL: URL) -> NotesRoute? {
        guard url.matchesOrigin(of: baseURL),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else {
            return nil
        }

        let path = components.percentEncodedPath
        if path == "/notes" || path == "/notes/" {
            return .list
        }
        if path == "/notes/add" || path == "/notes/add/" {
            return .add
        }

        let editPrefix = "/notes/edit/"
        if path.hasPrefix(editPrefix) {
            let encodedTitle = String(path.dropFirst(editPrefix.count))
                .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            guard !encodedTitle.isEmpty else { return nil }
            return .edit(title: encodedTitle.removingPercentEncoding ?? encodedTitle)
        }

        return nil
    }
}

/// A web page rendered inside a feature tab's navigation stack. `path` is the
/// app-relative path (with any query/fragment), resolved against the server
/// base URL by `WebDestinationView`.
struct WebRoute: Equatable, Hashable {
    let path: String
    var title: String?
}

/// A destination pushed onto the More tab's navigation stack.
enum MoreRoute: Equatable, Hashable {
    case web(WebRoute)
    case settings
    /// Native voice-conversation screen (replaces the web `/voice` page).
    case voice
}

@Observable
final class AppRouter {
    var selectedTab: AppTab = .chat
    var chatSelection = ChatRoute()
    var notesRoute: NotesRoute = .list
    var documentsPath: [WebRoute] = []
    var morePath: [MoreRoute] = []

    /// The tab that owns a given same-origin app URL. Returns `nil` for
    /// foreign-origin URLs (which should be opened externally). Resolution is
    /// exhaustive for same-origin URLs: anything not claimed by Chat, Notes, or
    /// Documents falls to More.
    static func owningTab(for url: URL, relativeTo baseURL: URL) -> AppTab? {
        guard url.matchesOrigin(of: baseURL) else { return nil }
        if ChatRoute.route(for: url, relativeTo: baseURL) != nil { return .chat }
        if NotesRoute.route(for: url, relativeTo: baseURL) != nil { return .notes }
        let path = url.normalizedPath
        if path == "/documents" || path.hasPrefix("/documents/") { return .documents }
        return .more
    }

    /// Deep-link / notification entry point: select the owning tab and set its
    /// route to the resolved destination, replacing that tab's existing stack.
    /// Returns `false` for foreign-origin URLs.
    @discardableResult
    func navigate(to url: URL, relativeTo baseURL: URL) -> Bool {
        guard let tab = Self.owningTab(for: url, relativeTo: baseURL) else {
            return false
        }

        switch tab {
        case .chat:
            chatSelection = ChatRoute.route(for: url, relativeTo: baseURL) ?? ChatRoute()
        case .notes:
            notesRoute = NotesRoute.route(for: url, relativeTo: baseURL) ?? .list
        case .documents:
            documentsPath = Self.isDocumentsRoot(url) ? [] : [WebRoute(path: url.pathAndQuery)]
        case .more:
            morePath = [.web(WebRoute(path: url.pathAndQuery))]
        }
        selectedTab = tab
        return true
    }

    /// A link tap (or in-page SPA navigation) inside a web view shown in
    /// `currentTab`.
    ///
    /// A web-backed tab is a browser: it owns its in-tab navigation via the
    /// WKWebView back/forward swipe gesture and pull-to-refresh. Mirroring the
    /// web view's own history into a native stack double-pushes — the web view
    /// has usually already navigated (React Router calls `history.pushState`
    /// before this runs), so a native push would leave a duplicate view over a
    /// mutated root. So same-tab destinations are left to the web view
    /// (`false`), and only cross-tab destinations switch tabs via `navigate`.
    /// Foreign-origin URLs also return `false` so the caller can open them
    /// externally.
    @discardableResult
    func followWebLink(_ url: URL, from currentTab: AppTab, relativeTo baseURL: URL) -> Bool {
        guard let tab = Self.owningTab(for: url, relativeTo: baseURL), tab != currentTab else {
            return false
        }
        return navigate(to: url, relativeTo: baseURL)
    }

    func reset() {
        selectedTab = .chat
        chatSelection = ChatRoute()
        notesRoute = .list
        documentsPath = []
        morePath = []
    }

    private static func isDocumentsRoot(_ url: URL) -> Bool {
        url.normalizedPath == "/documents"
    }
}

extension URL {
    func matchesOrigin(of other: URL) -> Bool {
        scheme?.lowercased() == other.scheme?.lowercased()
            && host?.lowercased() == other.host?.lowercased()
            && normalizedPort == other.normalizedPort
    }

    var pathAndQuery: String {
        var value = path.isEmpty ? "/" : path
        if let query {
            value += "?\(query)"
        }
        if let fragment {
            value += "#\(fragment)"
        }
        return value
    }

    /// The percent-encoded path with any trailing slash removed (except for the
    /// root "/"). Used for prefix matching when resolving the owning tab.
    var normalizedPath: String {
        let rawPath = URLComponents(url: self, resolvingAgainstBaseURL: false)?.percentEncodedPath ?? path
        if rawPath.count > 1, rawPath.hasSuffix("/") {
            return String(rawPath.dropLast())
        }
        return rawPath
    }

    private var normalizedPort: Int? {
        if let port { return port }
        switch scheme?.lowercased() {
        case "http":
            return 80
        case "https":
            return 443
        default:
            return nil
        }
    }
}
