import Foundation
import Observation

/// Top-level destinations shown in the root `TabView`. Tabs are features, not
/// implementations: Chat, Voice, and Notes are native, Documents is a focused
/// web surface, and More hosts the long-tail destinations.
enum AppTab: String, CaseIterable, Hashable {
    case chat
    case voice
    case notes
    case documents
    case more
}

/// Selection state for the native Chat tab. Mirrors the `/chat` web route.
struct ChatRoute: Equatable {
    var conversationID: String?
    var initialPrompt: String?
    var newConversationRequestID: String?
    var sharedAttachmentBatchID: String?

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
        let startsNewConversation = components.queryItems?.contains { item in
            item.name == "new" && item.value != "0" && item.value?.lowercased() != "false"
        } == true
        return ChatRoute(
            conversationID: conversationID,
            initialPrompt: initialPrompt,
            newConversationRequestID: startsNewConversation ? UUID().uuidString : nil
        )
    }
}

/// A read-only conversation shared by its bearer token. Shared transcripts are
/// native Chat destinations, but remain separate from the owner's editable
/// conversation selection and history.
struct SharedConversationRoute: Equatable {
    let token: String

    static func route(for url: URL, relativeTo baseURL: URL) -> SharedConversationRoute? {
        guard url.matchesOrigin(of: baseURL),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else {
            return nil
        }

        let prefix = "/shared/conversations/"
        let path = components.percentEncodedPath
        guard path.hasPrefix(prefix) else {
            return nil
        }
        let encodedToken = String(path.dropFirst(prefix.count))
        guard !encodedToken.isEmpty, !encodedToken.contains("/") else {
            return nil
        }
        let token = encodedToken.removingPercentEncoding ?? encodedToken
        guard !token.contains("/") else {
            return nil
        }
        return SharedConversationRoute(token: token)
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
}

@Observable
final class AppRouter {
    private static let voicePath = "/voice"

    var selectedTab: AppTab = .chat
    var chatSelection = ChatRoute()
    var sharedConversationRoute: SharedConversationRoute?
    var notesRoute: NotesRoute = .list
    var documentsPath: [WebRoute] = []
    var morePath: [MoreRoute] = []

    /// The tab that owns a given same-origin app URL. Returns `nil` for
    /// foreign-origin URLs (which should be opened externally). Resolution is
    /// exhaustive for same-origin URLs: anything not claimed by Chat, Notes, or
    /// Documents falls to More.
    static func owningTab(for url: URL, relativeTo baseURL: URL) -> AppTab? {
        guard url.matchesOrigin(of: baseURL) else { return nil }
        if SharedConversationRoute.route(for: url, relativeTo: baseURL) != nil { return .chat }
        if ChatRoute.route(for: url, relativeTo: baseURL) != nil { return .chat }
        if url.normalizedPath == Self.voicePath { return .voice }
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
            if let sharedRoute = SharedConversationRoute.route(for: url, relativeTo: baseURL) {
                sharedConversationRoute = sharedRoute
            } else {
                sharedConversationRoute = nil
                chatSelection = ChatRoute.route(for: url, relativeTo: baseURL) ?? ChatRoute()
            }
        case .voice:
            break
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
        sharedConversationRoute = nil
        notesRoute = .list
        documentsPath = []
        morePath = []
    }

    func openSharedAttachments(batchID: String) {
        sharedConversationRoute = nil
        chatSelection = ChatRoute(
            conversationID: nil,
            initialPrompt: nil,
            newConversationRequestID: batchID,
            sharedAttachmentBatchID: batchID
        )
        selectedTab = .chat
    }

    func closeSharedConversation() {
        sharedConversationRoute = nil
        selectedTab = .chat
    }

    private static func isDocumentsRoot(_ url: URL) -> Bool {
        url.normalizedPath == "/documents"
    }
}

/// Buffers URLs the OS asks the app to open (custom-scheme deep links and
/// file "Open in Family Assistant" hand-offs) until the SwiftUI layer is ready
/// to dispatch them.
///
/// The app installs a custom `UIWindowSceneDelegate`
/// (`HomeScreenShortcutSceneDelegate`) for home-screen quick actions. Setting
/// `UISceneConfiguration.delegateClass` replaces SwiftUI's internal scene
/// delegate — the object that feeds `.onOpenURL` — so opened URLs must instead
/// be captured by the custom delegate and forwarded here, where
/// `FamilyAssistantApp` drains them with its full dependencies (auth,
/// notifications, shared-attachment inbox) in hand. Mirrors the
/// `IntentNavigationCenter` hand-off pattern.
@MainActor
@Observable
final class OpenURLCenter {
    static let shared = OpenURLCenter()

    private(set) var pendingURLs: [URL] = []

    init() {}

    func receive(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        pendingURLs.append(contentsOf: urls)
    }

    /// Returns and clears any buffered URLs.
    func consumePendingURLs() -> [URL] {
        defer { pendingURLs = [] }
        return pendingURLs
    }
}

struct SharedAttachmentBatch: Equatable, Identifiable {
    let id: String
    let fileURLs: [URL]
    let importErrors: [String]
}

@MainActor
@Observable
final class SharedAttachmentInbox {
    var pendingBatch: SharedAttachmentBatch?

    @ObservationIgnored private let fileManager: FileManager
    @ObservationIgnored private let receiveDebounce: Duration
    @ObservationIgnored private var bufferedReceivedURLs: [URL] = []
    @ObservationIgnored private var receiveTask: Task<Void, Never>?

    init(fileManager: FileManager = .default, receiveDebounce: Duration = .milliseconds(250)) {
        self.fileManager = fileManager
        self.receiveDebounce = receiveDebounce
    }

    static func canReceive(_ url: URL) -> Bool {
        url.isFileURL
    }

    func receive(urls: [URL]) {
        bufferedReceivedURLs.append(contentsOf: urls)
        receiveTask?.cancel()
        receiveTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await Task.sleep(for: receiveDebounce)
            } catch {
                return
            }
            flushBufferedURLs()
        }
    }

    private func flushBufferedURLs() {
        let urls = bufferedReceivedURLs
        bufferedReceivedURLs = []
        receiveTask = nil
        guard !urls.isEmpty else {
            return
        }
        let fileManager = fileManager
        Task.detached { [weak self] in
            let importResult = Self.importURLs(urls, fileManager: fileManager)
            await self?.enqueue(importResult)
        }
    }

    private func enqueue(_ importResult: SharedAttachmentImportResult) {
        guard !importResult.fileURLs.isEmpty || !importResult.errors.isEmpty else { return }
        if let pendingBatch {
            self.pendingBatch = SharedAttachmentBatch(
                id: pendingBatch.id,
                fileURLs: pendingBatch.fileURLs + importResult.fileURLs,
                importErrors: pendingBatch.importErrors + importResult.errors
            )
            return
        }
        pendingBatch = SharedAttachmentBatch(
            id: UUID().uuidString,
            fileURLs: importResult.fileURLs,
            importErrors: importResult.errors
        )
    }

    func consume(batchID: String) -> SharedAttachmentBatch? {
        guard pendingBatch?.id == batchID else {
            return nil
        }
        let batch = pendingBatch
        pendingBatch = nil
        return batch
    }

    nonisolated private static func importURLs(
        _ sourceURLs: [URL],
        fileManager: FileManager
    ) -> SharedAttachmentImportResult {
        var importedURLs: [URL] = []
        var errors: [String] = []
        for sourceURL in sourceURLs {
            do {
                importedURLs.append(try importURL(sourceURL, fileManager: fileManager))
            } catch {
                let filename = sourceURL.lastPathComponent.isEmpty ? sourceURL.absoluteString : sourceURL.lastPathComponent
                errors.append("Could not import \(filename). \(error.localizedDescription)")
            }
        }
        return SharedAttachmentImportResult(fileURLs: importedURLs, errors: errors)
    }

    nonisolated private static func importURL(_ sourceURL: URL, fileManager: FileManager) throws -> URL {
        guard sourceURL.isFileURL else {
            throw SharedAttachmentImportError.unsupportedURL
        }
        let isScoped = sourceURL.startAccessingSecurityScopedResource()
        defer {
            if isScoped {
                sourceURL.stopAccessingSecurityScopedResource()
            }
        }

        let sourceFileSize = try sourceURL.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        guard sourceFileSize <= ChatConstants.maxAttachmentSizeBytes else {
            throw SharedAttachmentImportError.oversizedFile
        }

        let directory = fileManager.temporaryDirectory
            .appendingPathComponent("SharedAttachmentImports", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appendingPathComponent(sourceURL.lastPathComponent)
        if fileManager.fileExists(atPath: destination.path) {
            try fileManager.removeItem(at: destination)
        }
        try fileManager.copyItem(at: sourceURL, to: destination)
        return destination
    }
}

private struct SharedAttachmentImportResult: Sendable {
    let fileURLs: [URL]
    let errors: [String]
}

private enum SharedAttachmentImportError: LocalizedError {
    case unsupportedURL
    case oversizedFile

    var errorDescription: String? {
        switch self {
        case .unsupportedURL:
            "Only local files can be attached."
        case .oversizedFile:
            "File size exceeds 100MB."
        }
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
