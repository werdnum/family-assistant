import Foundation
import UniformTypeIdentifiers

@MainActor
struct ChatAPIClient {
    let authManager: AuthManager
    var urlSession: URLSession = .shared
    private static let conversationPageSize = 100

    func listConversations() async throws -> [ChatConversationSummary] {
        var conversations: [ChatConversationSummary] = []
        var offset = 0

        while true {
            let response = try await listConversationPage(limit: Self.conversationPageSize, offset: offset)
            conversations.append(contentsOf: response.conversations)

            if response.conversations.isEmpty || conversations.count >= response.count {
                return conversations
            }
            offset += response.conversations.count
        }
    }

    private func listConversationPage(limit: Int, offset: Int) async throws -> ChatConversationListResponse {
        let request = try await authManager.authorizedRequest(
            url: conversationListURL(limit: limit, offset: offset),
            method: "GET"
        )
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatConversationListResponse.self, from: data)
    }

    func getMessages(conversationID: String) async throws -> [ChatBackendMessage] {
        let encodedID = Self.encodedPathComponent(conversationID)
        let request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/conversations/\(encodedID)/messages?limit=0"),
            method: "GET"
        )
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatConversationMessagesResponse.self, from: data).messages
    }

    func listProfiles() async throws -> ChatProfilesResponse {
        let request = try await authManager.authorizedRequest(url: apiURL("/api/v1/profiles"), method: "GET")
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatProfilesResponse.self, from: data)
    }

    /// Sends a prompt and waits for the assistant's full reply.
    ///
    /// Unlike ``streamMessage(prompt:conversationID:profileID:attachments:)`` this
    /// uses the non-streaming `/send_message` endpoint, which suits headless
    /// callers (App Intents / Siri) that present a single completed reply rather
    /// than a live token stream.
    func sendMessage(
        prompt: String,
        conversationID: String,
        profileID: String?
    ) async throws -> ChatSendResult {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/send_message"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.chatEncoder.encode(
            ChatSendMessageRequest(
                prompt: prompt,
                conversationID: conversationID,
                profileID: profileID,
                interfaceType: ChatConstants.interfaceType
            )
        )

        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        let decoded = try JSONDecoder.chatDecoder.decode(ChatSendMessageResponse.self, from: data)
        return ChatSendResult(reply: decoded.reply, conversationID: decoded.conversationID)
    }

    func streamMessage(
        prompt: String,
        conversationID: String,
        profileID: String?,
        attachments: [ChatAttachment]
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/send_message_stream"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.chatEncoder.encode(
            ChatStreamRequest(
                prompt: prompt,
                conversationID: conversationID,
                profileID: profileID,
                interfaceType: ChatConstants.interfaceType,
                attachments: attachments.map(ChatStreamAttachment.init(attachment:))
            )
        )

        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await urlSession.bytes(for: request)
                    try validate(response: response, data: Data())
                    let parser = SSEParser()
                    try await streamServerSentEvents(bytes: bytes, parser: parser, continuation: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    func connectEvents(
        conversationID: String,
        after: Date? = nil
    ) async throws -> AsyncThrowingStream<ChatStreamEvent, Error> {
        var components = URLComponents(url: try apiURL("/api/v1/chat/events"), resolvingAgainstBaseURL: false)
        components?.queryItems = [
            URLQueryItem(name: "conversation_id", value: conversationID),
            URLQueryItem(name: "interface_type", value: ChatConstants.interfaceType),
        ]
        if let after {
            components?.queryItems?.append(
                URLQueryItem(name: "after", value: ISO8601DateFormatter().string(from: after))
            )
        }
        guard let url = components?.url else {
            throw ChatAPIError.invalidServerURL
        }
        let request = try await authManager.authorizedRequest(url: url, method: "GET")

        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await urlSession.bytes(for: request)
                    try validate(response: response, data: Data())
                    let parser = SSEParser()
                    try await streamServerSentEvents(bytes: bytes, parser: parser, continuation: continuation)
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    func listPendingConfirmations() async throws -> [ChatPendingConfirmation] {
        let request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/confirmations/pending"),
            method: "GET"
        )
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatPendingConfirmationsResponse.self, from: data).confirmations
    }

    func confirmTool(requestID: String, conversationID: String?, approved: Bool) async throws {
        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/v1/chat/confirm_tool"),
            method: "POST"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.chatEncoder.encode(
            ChatConfirmationActionRequest(
                requestID: requestID,
                approved: approved,
                conversationID: conversationID,
                approvingInterface: "ios"
            )
        )

        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        let result = try JSONDecoder.chatDecoder.decode(ChatConfirmationActionResponse.self, from: data)
        if !result.success {
            throw ChatAPIError.server(statusCode: 200, detail: result.message ?? "Confirmation request failed.")
        }
    }

    func uploadAttachment(fileURL: URL, mimeType: String) async throws -> ChatUploadResponse {
        let values = try fileURL.resourceValues(forKeys: [.fileSizeKey, .nameKey])
        let fileSize = values.fileSize ?? 0
        guard fileSize <= ChatConstants.maxAttachmentSizeBytes else {
            throw ChatAPIError.validation("File size exceeds 100MB.")
        }
        guard ChatConstants.allowedAttachmentMIMETypes.contains(mimeType) else {
            throw ChatAPIError.validation("Unsupported file type: \(mimeType).")
        }

        var request = try await authManager.authorizedRequest(
            url: apiURL("/api/attachments/upload"),
            method: "POST"
        )
        let boundary = "Boundary-\(UUID().uuidString)"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = try await Self.multipartBody(
            fileURL: fileURL,
            filename: values.name ?? fileURL.lastPathComponent,
            mimeType: mimeType,
            boundary: boundary
        )

        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.chatDecoder.decode(ChatUploadResponse.self, from: data)
    }

    func deleteAttachment(attachmentID: String) async throws {
        let request = try await authManager.authorizedRequest(
            url: apiURL("/api/attachments/\(Self.encodedPathComponent(attachmentID))"),
            method: "DELETE"
        )
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
    }

    func downloadAttachment(path: String) async throws -> (Data, String?) {
        let request = try await authManager.authorizedRequest(url: apiURL(path), method: "GET")
        let (data, response) = try await urlSession.data(for: request)
        try validate(response: response, data: data)
        return (data, (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Type"))
    }

    func mimeType(for fileURL: URL) -> String {
        if let contentType = try? fileURL.resourceValues(forKeys: [.contentTypeKey]).contentType,
           let mimeType = contentType.preferredMIMEType
        {
            return mimeType
        }
        let ext = fileURL.pathExtension
        return UTType(filenameExtension: ext)?.preferredMIMEType ?? "application/octet-stream"
    }

    private func conversationListURL(limit: Int, offset: Int) throws -> URL {
        var components = URLComponents(url: try apiURL("/api/v1/chat/conversations"), resolvingAgainstBaseURL: false)
        components?.queryItems = [
            URLQueryItem(name: "interface_type", value: ChatConstants.interfaceType),
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset)),
        ]
        guard let url = components?.url else {
            throw ChatAPIError.invalidServerURL
        }
        return url
    }

    private func apiURL(_ path: String) throws -> URL {
        guard let baseURL = authManager.validatedServerURL(),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL
        else {
            throw ChatAPIError.invalidServerURL
        }
        return url
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let httpResponse = response as? HTTPURLResponse else {
            throw ChatAPIError.invalidResponse
        }
        guard (200 ..< 300).contains(httpResponse.statusCode) else {
            let detail = try? JSONDecoder.chatDecoder.decode(ChatServerError.self, from: data).detail
            throw ChatAPIError.server(statusCode: httpResponse.statusCode, detail: detail)
        }
    }

    private func streamServerSentEvents(
        bytes: URLSession.AsyncBytes,
        parser: SSEParser,
        continuation: AsyncThrowingStream<ChatStreamEvent, Error>.Continuation
    ) async throws {
        var pendingUTF8 = Data()
        for try await byte in bytes {
            pendingUTF8.append(byte)
            guard let chunk = String(data: pendingUTF8, encoding: .utf8) else {
                continue
            }
            pendingUTF8.removeAll(keepingCapacity: true)
            for event in parser.append(chunk) {
                continuation.yield(parser.decode(event))
            }
        }
        if !pendingUTF8.isEmpty {
            for event in parser.append(String(decoding: pendingUTF8, as: UTF8.self)) {
                continuation.yield(parser.decode(event))
            }
        }
        for event in parser.flush() {
            continuation.yield(parser.decode(event))
        }
    }

    private nonisolated static func multipartBody(
        fileURL: URL,
        filename: String,
        mimeType: String,
        boundary: String
    ) async throws -> Data {
        try await Task.detached(priority: .utility) {
            var data = Data()
            data.append("--\(boundary)\r\n")
            data.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n")
            data.append("Content-Type: \(mimeType)\r\n\r\n")
            data.append(try Data(contentsOf: fileURL))
            data.append("\r\n--\(boundary)--\r\n")
            return data
        }.value
    }

    private static func encodedPathComponent(_ value: String) -> String {
        var allowedCharacters = CharacterSet.urlPathAllowed
        allowedCharacters.remove(charactersIn: "/?#")
        return value.addingPercentEncoding(withAllowedCharacters: allowedCharacters) ?? value
    }
}

/// The completed result of a non-streaming chat send.
struct ChatSendResult: Equatable {
    let reply: String
    let conversationID: String
}

private struct ChatSendMessageRequest: Encodable {
    let prompt: String
    let conversationID: String
    let profileID: String?
    let interfaceType: String

    enum CodingKeys: String, CodingKey {
        case prompt
        case conversationID = "conversation_id"
        case profileID = "profile_id"
        case interfaceType = "interface_type"
    }
}

private struct ChatSendMessageResponse: Decodable {
    let reply: String
    let conversationID: String

    enum CodingKeys: String, CodingKey {
        case reply
        case conversationID = "conversation_id"
    }
}

private struct ChatStreamRequest: Encodable {
    let prompt: String
    let conversationID: String
    let profileID: String?
    let interfaceType: String
    let attachments: [ChatStreamAttachment]?

    enum CodingKeys: String, CodingKey {
        case prompt
        case conversationID = "conversation_id"
        case profileID = "profile_id"
        case interfaceType = "interface_type"
        case attachments
    }
}

private struct ChatStreamAttachment: Encodable {
    let id: String
    let type: String
    let name: String
    let content: String

    init(attachment: ChatAttachment) {
        id = attachment.id
        type = attachment.type.rawValue
        name = attachment.name
        content = attachment.contentURL ?? ""
    }
}

private struct ChatConfirmationActionRequest: Encodable {
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

private struct ChatConfirmationActionResponse: Decodable {
    let success: Bool
    let message: String?
}

private struct ChatServerError: Decodable {
    let detail: String?
}

enum ChatAPIError: LocalizedError, Equatable {
    case invalidServerURL
    case invalidResponse
    case validation(String)
    case server(statusCode: Int, detail: String?)

    var errorDescription: String? {
        switch self {
        case .invalidServerURL:
            "Invalid server URL."
        case .invalidResponse:
            "The server returned an invalid response."
        case .validation(let message):
            message
        case .server(let statusCode, let detail):
            if let detail, !detail.isEmpty {
                detail
            } else {
                "Chat request failed with status \(statusCode)."
            }
        }
    }
}

private extension Data {
    mutating func append(_ string: String) {
        append(Data(string.utf8))
    }
}
