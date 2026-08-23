import Foundation

struct NativeNote: Decodable, Equatable, Identifiable {
    let title: String
    let content: String
    let includeInPrompt: Bool
    let attachmentIds: [String]
    let visibilityLabels: [String]
    let isSkill: Bool
    let skillName: String?
    let skillDescription: String?

    var id: String { title }

    enum CodingKeys: String, CodingKey {
        case title
        case content
        case includeInPrompt = "include_in_prompt"
        case attachmentIds = "attachment_ids"
        case visibilityLabels = "visibility_labels"
        case isSkill = "is_skill"
        case skillName = "skill_name"
        case skillDescription = "skill_description"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        title = try container.decode(String.self, forKey: .title)
        content = try container.decode(String.self, forKey: .content)
        includeInPrompt = try container.decodeIfPresent(Bool.self, forKey: .includeInPrompt) ?? true
        attachmentIds = try container.decodeIfPresent([String].self, forKey: .attachmentIds) ?? []
        visibilityLabels = try container.decodeIfPresent([String].self, forKey: .visibilityLabels) ?? []
        isSkill = try container.decodeIfPresent(Bool.self, forKey: .isSkill) ?? false
        skillName = try container.decodeIfPresent(String.self, forKey: .skillName)
        skillDescription = try container.decodeIfPresent(String.self, forKey: .skillDescription)
    }
}

struct NativeNoteSaveRequest: Encodable {
    let title: String
    let content: String
    let includeInPrompt: Bool
    let originalTitle: String?
    let attachmentIds: [String]
    let visibilityLabels: [String]

    enum CodingKeys: String, CodingKey {
        case title
        case content
        case includeInPrompt = "include_in_prompt"
        case originalTitle = "original_title"
        case attachmentIds = "attachment_ids"
        case visibilityLabels = "visibility_labels"
    }
}

@MainActor
struct NotesAPIClient {
    let authManager: AuthManager

    func listNotes() async throws -> [NativeNote] {
        let request = try await authManager.authorizedRequest(url: apiURL("/api/notes/"), method: "GET")
        let (data, response) = try await URLSession.shared.dataExpectingJSON(for: request, authWallError: NotesAPIError.authWall)
        try validate(response: response, data: data)
        return try JSONDecoder().decode([NativeNote].self, from: data)
    }

    func getNote(title: String) async throws -> NativeNote {
        let request = try await authManager.authorizedRequest(
            url: apiURL("/api/notes/\(Self.encodedPathComponent(title))"),
            method: "GET"
        )
        let (data, response) = try await URLSession.shared.dataExpectingJSON(for: request, authWallError: NotesAPIError.authWall)
        try validate(response: response, data: data)
        return try JSONDecoder().decode(NativeNote.self, from: data)
    }

    func saveNote(_ note: NativeNoteSaveRequest) async throws {
        var request = try await authManager.authorizedRequest(url: apiURL("/api/notes/"), method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(note)

        let (data, response) = try await URLSession.shared.dataExpectingJSON(for: request, authWallError: NotesAPIError.authWall)
        try validate(response: response, data: data)
    }

    func deleteNote(title: String) async throws {
        let request = try await authManager.authorizedRequest(
            url: apiURL("/api/notes/\(Self.encodedPathComponent(title))"),
            method: "DELETE"
        )
        let (data, response) = try await URLSession.shared.dataExpectingJSON(for: request, authWallError: NotesAPIError.authWall)
        try validate(response: response, data: data)
    }

    private func apiURL(_ path: String) throws -> URL {
        guard let baseURL = authManager.validatedServerURL(),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
        else {
            throw NotesAPIError.invalidServerURL
        }
        return url
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let httpResponse = response as? HTTPURLResponse else {
            throw NotesAPIError.invalidResponse
        }
        try AuthWallDetection.rejectIfLikely(
            response: httpResponse,
            data: data,
            throwing: NotesAPIError.authWall
        )
        guard (200 ..< 300).contains(httpResponse.statusCode) else {
            let detail = try? JSONDecoder().decode(ServerError.self, from: data).detail
            throw NotesAPIError.server(statusCode: httpResponse.statusCode, detail: detail)
        }
    }

    private static func encodedPathComponent(_ value: String) -> String {
        var allowedCharacters = CharacterSet.urlPathAllowed
        allowedCharacters.remove(charactersIn: "/?#")
        return value.addingPercentEncoding(withAllowedCharacters: allowedCharacters) ?? value
    }
}

private struct ServerError: Decodable {
    let detail: String?
}

enum NotesAPIError: LocalizedError {
    case invalidServerURL
    case invalidResponse
    /// The server answered with an HTML sign-in page (an edge authentication
    /// wall) instead of the expected API payload.
    case authWall
    case server(statusCode: Int, detail: String?)

    var errorDescription: String? {
        switch self {
        case .invalidServerURL:
            return "Invalid server URL"
        case .invalidResponse:
            return "The server returned an invalid response."
        case .authWall:
            return "Server requires sign-in or is unreachable (authentication wall detected)."
        case .server(let statusCode, let detail):
            if let detail, !detail.isEmpty {
                return detail
            }
            return "Notes request failed with status \(statusCode)."
        }
    }
}
