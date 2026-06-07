import Foundation

enum AppRoute: Equatable {
    case web(path: String)
    case notes(NotesRoute)
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

@Observable
final class AppRouter {
    var route: AppRoute = .web(path: "/chat")

    @discardableResult
    func openNativeURL(_ url: URL, relativeTo baseURL: URL) -> Bool {
        guard let notesRoute = NotesRoute.route(for: url, relativeTo: baseURL) else {
            return false
        }
        route = .notes(notesRoute)
        return true
    }

    func openWebPath(_ path: String) {
        route = .web(path: path)
    }

    func openNotesList() {
        route = .notes(.list)
    }

    func reset() {
        route = .web(path: "/chat")
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
