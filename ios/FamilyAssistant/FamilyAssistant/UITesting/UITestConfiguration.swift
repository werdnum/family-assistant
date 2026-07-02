import Foundation

#if DEBUG
enum UITestConfiguration {
    static var isEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains("--ui-testing")
    }

    static var isLiveBackendEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains("--live-ui-testing")
    }

    static var initialNavigationPath: String? {
        guard isEnabled || isLiveBackendEnabled else { return nil }
        return ProcessInfo.processInfo.environment["FAMILY_ASSISTANT_UITEST_INITIAL_PATH"]
    }

    /// True when this app process is hosting the XCTest unit-test bundle.
    ///
    /// Xcode sets `XCTestConfigurationFilePath` in the host app's environment for
    /// app-hosted unit tests, but not for the separately-launched app under UI
    /// test (that env var lives on the UI-test runner instead). When true the app
    /// must not boot its real UI: `ContentView` would run `AuthManager`
    /// bootstrap and start the chat live-events stream against the unit tests'
    /// shared `URLProtocol` mock, racing stray `GET …/stream` requests into
    /// unrelated tests (e.g. `AppIntentsTests`) and making them flaky.
    static var isHostingUnitTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }

    static func applyIfNeeded() {
        if isLiveBackendEnabled {
            applyLiveBackendConfiguration()
            return
        }

        guard isEnabled else { return }

        URLProtocol.registerClass(UITestBackendURLProtocol.self)
        UITestBackendURLProtocol.reset()

        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        KeychainHelper.save(key: "fa_api_token", string: "ui-test-api-token")
        KeychainHelper.save(key: "fa_refresh_token", string: "ui-test-refresh-token")

        UserDefaults.standard.set("https://assistant.example.test", forKey: "fa_server_url")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
        UserDefaults.standard.set(false, forKey: "fa_notifications_enabled")
        UserDefaults.standard.removeObject(forKey: "fa_apns_device_token")
    }

    private static func applyLiveBackendConfiguration() {
        let environment = ProcessInfo.processInfo.environment
        let serverURL = environment["FAMILY_ASSISTANT_LIVE_SERVER_URL"]
            ?? "https://family-assistant-dev.andrewgarrett.dev"
        let apiToken = environment["FAMILY_ASSISTANT_LIVE_API_TOKEN"] ?? "live-ui-test-token"

        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        KeychainHelper.save(key: "fa_api_token", string: apiToken)

        UserDefaults.standard.set(serverURL, forKey: "fa_server_url")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
        UserDefaults.standard.set(false, forKey: "fa_notifications_enabled")
        UserDefaults.standard.removeObject(forKey: "fa_apns_device_token")
    }
}

private final class UITestBackendURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var notes: [String: UITestNote] = [:]
    private static var chatMessages: [String: [UITestChatMessage]] = [:]
    // Reply text + client turn_id captured at POST /turns time so the
    // follow-up GET stream can replay them (mirrors the production two-step
    // resumable-streaming flow).
    private static var pendingChatReplies: [String: String] = [:]
    private static var pendingChatTurnIDs: [String: String] = [:]

    static func reset() {
        lock.withLock {
            notes = [
                "Shopping": UITestNote(
                    title: "Shopping",
                    content: "Buy milk and apples",
                    includeInPrompt: true,
                    attachmentIds: [],
                    visibilityLabels: ["family"],
                    isSkill: false,
                    skillName: nil,
                    skillDescription: nil
                ),
                "School Pickup": UITestNote(
                    title: "School Pickup",
                    content: "Pickup is at 3:15 by the north gate.",
                    includeInPrompt: false,
                    attachmentIds: ["calendar"],
                    visibilityLabels: ["parents"],
                    isSkill: false,
                    skillName: nil,
                    skillDescription: nil
                ),
            ]
            chatMessages = [
                "web_conv_seed": [
                    UITestChatMessage(
                        internalID: 1,
                        role: "user",
                        content: "What is on the list?",
                        timestamp: "2026-06-08T12:00:00Z",
                        toolCalls: nil,
                        toolCallID: nil,
                        attachments: nil
                    ),
                    UITestChatMessage(
                        internalID: 2,
                        role: "assistant",
                        content: "Milk and apples.",
                        timestamp: "2026-06-08T12:00:01Z",
                        toolCalls: nil,
                        toolCallID: nil,
                        attachments: nil
                    ),
                ],
                // Repro/verify harness for the chat-thread layout watchdog crash:
                // a long thread whose assistant turns are markdown-heavy (nested
                // lists, a wide table, a code block). Launch with --ui-testing,
                // open this conversation, and confirm it scrolls smoothly and shows
                // a "Load earlier messages" control rather than hanging. See
                // docs/design/ios-chat-layout-watchdog-crash.md.
                "web_conv_large": largeMarkdownThread(),
                // Repro for the scene-update watchdog hang on build 23
                // (scratch/FamilyAssistant-2026-06-27-192549.ips): a tool-using
                // turn whose (always-visible) assistant answer is a very large
                // markdown document. Sending a follow-up re-lays out that bubble's
                // unbounded block tree on the main thread and overruns the 10s
                // budget. (A collapsed tool group is NOT laid out, so the large
                // result is incidental; the visible answer is what hangs.) See
                // docs/design/ios-chat-layout-watchdog-crash.md.
                "web_conv_tool_heavy": toolHeavyThread(),
            ]
            pendingChatReplies = [:]
            pendingChatTurnIDs = [:]
        }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "assistant.example.test"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        let response = Self.response(for: request)
        client?.urlProtocol(self, didReceive: response.httpResponse(for: request), cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: response.data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func response(for request: URLRequest) -> UITestResponse {
        guard request.value(forHTTPHeaderField: "Authorization") == "Bearer ui-test-api-token"
            || request.url?.path.hasPrefix("/api/auth/") == true
        else {
            return .json(#"{"detail":"missing auth"}"#, statusCode: 401)
        }

        switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
        case ("POST", "/api/auth/token-session"):
            return .json("{}", headers: ["Set-Cookie": "session=ui-test; Path=/; HttpOnly"])
        case ("POST", "/api/auth/refresh"):
            return .json(
                #"{"api_token":"ui-test-api-token","refresh_token":"ui-test-refresh-token","expires_in":7200}"#
            )
        case ("POST", "/api/auth/logout"):
            return .json("{}")
        case ("GET", "/api/v1/profiles"):
            return .json(
                """
                {
                  "profiles": [
                    {
                      "id": "default_assistant",
                      "description": "Default assistant",
                      "llm_model": "ui-test-model",
                      "available_tools": ["notes", "calendar"],
                      "enabled_mcp_servers": [],
                      "delegation_only": false
                    },
                    {
                      "id": "research",
                      "description": "Research profile",
                      "llm_model": "ui-test-research",
                      "available_tools": ["search"],
                      "enabled_mcp_servers": [],
                      "delegation_only": false
                    }
                  ],
                  "default_profile_id": "default_assistant"
                }
                """
            )
        case ("GET", "/api/v1/chat/conversations"):
            return encode(UITestConversationListResponse(
                conversations: chatConversationSummaries(),
                count: lock.withLock { chatMessages.count }
            ))
        case ("GET", "/api/v1/chat/confirmations/pending"):
            return .json(#"{"confirmations":[]}"#)
        case ("POST", "/api/v1/chat/turns"):
            return startChatTurn(from: request)
        case ("GET", "/api/notes"), ("GET", "/api/notes/"):
            return encode(lock.withLock { notes.values.sorted { $0.title < $1.title } })
        case ("POST", "/api/notes"), ("POST", "/api/notes/"):
            return saveNote(from: request)
        default:
            if request.httpMethod == "GET", let conversationID = streamConversationID(from: request) {
                return chatStreamResponse(conversationID: conversationID)
            }
            if request.httpMethod == "GET", let conversationID = conversationID(from: request) {
                let messages = lock.withLock { chatMessages[conversationID] ?? [] }
                return encode(UITestConversationMessagesResponse(
                    conversationID: conversationID,
                    messages: messages,
                    count: messages.count,
                    totalMessages: messages.count,
                    hasMoreBefore: false,
                    hasMoreAfter: false
                ))
            }
            if (request.httpMethod ?? "GET") == "GET", let title = noteTitle(from: request) {
                guard let note = lock.withLock({ notes[title] }) else {
                    return .json(#"{"detail":"Note not found."}"#, statusCode: 404)
                }
                return encode(note)
            }
            if request.httpMethod == "DELETE", let title = noteTitle(from: request) {
                lock.withLock {
                    _ = notes.removeValue(forKey: title)
                }
                return .json("{}")
            }
            return .json(#"{"detail":"Unhandled UI test route."}"#, statusCode: 404)
        }
    }

    private static func startChatTurn(from request: URLRequest) -> UITestResponse {
        do {
            let body = try JSONDecoder().decode(UITestChatStreamRequest.self, from: request.bodyData)
            let conversationID = body.conversationID
            let reply = "Native reply to \(body.prompt)"
            lock.withLock {
                var messages = chatMessages[conversationID] ?? []
                let nextID = (messages.map(\.internalID).max() ?? 0) + 1
                messages.append(UITestChatMessage(
                    internalID: nextID,
                    role: "user",
                    content: body.prompt,
                    timestamp: "2026-06-08T12:00:02Z",
                    toolCalls: nil,
                    toolCallID: nil,
                    attachments: nil
                ))
                messages.append(UITestChatMessage(
                    internalID: nextID + 1,
                    role: "assistant",
                    content: reply,
                    timestamp: "2026-06-08T12:00:03Z",
                    toolCalls: [
                        UITestToolCall(
                            id: "call-ui-test",
                            type: "function",
                            function: UITestToolFunction(
                                name: "search_notes",
                                arguments: #"{"query":"shopping"}"#
                            )
                        ),
                    ],
                    toolCallID: nil,
                    attachments: [
                        UITestAttachment(
                            attachmentID: "33333333-3333-3333-3333-333333333333",
                            name: "reply.md",
                            contentURL: "/api/attachments/33333333-3333-3333-3333-333333333333",
                            mimeType: "text/markdown",
                            size: 20
                        ),
                    ]
                ))
                chatMessages[conversationID] = messages
                pendingChatReplies[conversationID] = reply
                pendingChatTurnIDs[conversationID] = body.turnID
            }
            return encode(UITestChatTurnResponse(
                turnID: body.turnID,
                conversationID: conversationID,
                firstSeq: 0
            ))
        } catch {
            return .json(#"{"detail":"Invalid chat payload."}"#, statusCode: 400)
        }
    }

    private static func chatStreamResponse(conversationID: String) -> UITestResponse {
        let (reply, turnID) = lock.withLock {
            (pendingChatReplies[conversationID] ?? "Native reply",
             pendingChatTurnIDs[conversationID] ?? "mock-turn")
        }
        let escapedReply = reply.replacingOccurrences(of: "\"", with: "\\\"")
        // Stamp every event with the real (client-supplied) turn_id so the
        // native client's send-and-watch turn filter accepts them.
        return .json(
            """
            event: turn_started
            data: {"turn_id":"\(turnID)","seq":0}

            event: text
            data: {"turn_id":"\(turnID)","content":"\(escapedReply)"}

            event: tool_call
            data: {"turn_id":"\(turnID)","tool_call":{"id":"call-ui-test","type":"function","function":{"name":"search_notes","arguments":"{\\"query\\":\\"shopping\\"}"}}}

            event: tool_result
            data: {"turn_id":"\(turnID)","tool_call_id":"call-ui-test","result":"Found shopping note"}

            event: turn_ended
            data: {"turn_id":"\(turnID)","status":"complete"}

            """,
            headers: ["Content-Type": "text/event-stream"]
        )
    }

    private static func streamConversationID(from request: URLRequest) -> String? {
        guard let url = request.url,
              let path = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath,
              path.hasPrefix("/api/v1/chat/conversations/"),
              path.hasSuffix("/stream")
        else {
            return nil
        }
        let withoutPrefix = String(path.dropFirst("/api/v1/chat/conversations/".count))
        let encodedID = String(withoutPrefix.dropLast("/stream".count))
        return encodedID.removingPercentEncoding
    }

    private static func saveNote(from request: URLRequest) -> UITestResponse {
        do {
            let body = try JSONDecoder().decode(UITestNoteSaveRequest.self, from: request.bodyData)
            let originalTitle = body.originalTitle ?? body.title
            lock.withLock {
                let existing = notes[originalTitle]
                notes.removeValue(forKey: originalTitle)
                notes[body.title] = UITestNote(
                    title: body.title,
                    content: body.content,
                    includeInPrompt: body.includeInPrompt,
                    attachmentIds: body.attachmentIds.isEmpty ? existing?.attachmentIds ?? [] : body.attachmentIds,
                    visibilityLabels: body.visibilityLabels.isEmpty ? existing?.visibilityLabels ?? [] : body.visibilityLabels,
                    isSkill: existing?.isSkill ?? false,
                    skillName: existing?.skillName,
                    skillDescription: existing?.skillDescription
                )
            }
            return .json("{}")
        } catch {
            return .json(#"{"detail":"Invalid note payload."}"#, statusCode: 400)
        }
    }

    private static func noteTitle(from request: URLRequest) -> String? {
        guard let url = request.url,
              let path = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath,
              path.hasPrefix("/api/notes/")
        else {
            return nil
        }
        let encodedTitle = String(path.dropFirst("/api/notes/".count))
        return encodedTitle.removingPercentEncoding
    }

    private static func conversationID(from request: URLRequest) -> String? {
        guard let url = request.url,
              let path = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath,
              path.hasPrefix("/api/v1/chat/conversations/"),
              path.hasSuffix("/messages")
        else {
            return nil
        }
        let withoutPrefix = String(path.dropFirst("/api/v1/chat/conversations/".count))
        let encodedID = String(withoutPrefix.dropLast("/messages".count))
        return encodedID.removingPercentEncoding
    }

    /// A long, markdown-heavy thread used to reproduce / verify the chat-thread
    /// layout watchdog fix. Assistant turns carry nested lists, a wide table, and
    /// a code block so the layout cost per bubble is non-trivial; the thread is
    /// long enough to exercise the rendered-message window.
    private static func largeMarkdownThread() -> [UITestChatMessage] {
        let assistantBody = """
        ## Summary for step \u{2116}

        Here is a paragraph with **bold**, _italic_, and `inline code` that wraps
        across several lines so the text engine has real work to measure.

        - First point about the topic
          - A nested detail
          - Another nested detail
            - A deeply nested detail
        - Second point about the topic

        > A block quote that also contains a list:
        > - quoted item one
        > - quoted item two

        | Column A | Column B | Column C | Column D | Column E |
        | --- | --- | --- | --- | --- |
        | alpha | beta | gamma | delta | epsilon |
        | one | two | three | four | five |

        ```swift
        let result = compute(aLongIdentifier, anotherLongArgument, thirdArgument)
        ```
        """

        var messages: [UITestChatMessage] = []
        let turns = 30
        for turn in 0..<turns {
            let base = turn * 2
            let minute = String(format: "%02d", turn % 60)
            messages.append(UITestChatMessage(
                internalID: base + 1,
                role: "user",
                content: "Question number \(turn + 1): can you explain topic \(turn + 1)?",
                timestamp: "2026-06-08T10:\(minute):00Z",
                toolCalls: nil,
                toolCallID: nil,
                attachments: nil
            ))
            messages.append(UITestChatMessage(
                internalID: base + 2,
                role: "assistant",
                content: assistantBody.replacingOccurrences(of: "\u{2116}", with: "\(turn + 1)"),
                timestamp: "2026-06-08T10:\(minute):30Z",
                toolCalls: nil,
                toolCallID: nil,
                attachments: nil
            ))
        }
        return messages
    }

    /// A tool-using turn whose (always-visible) assistant answer is a very large
    /// markdown document. Reproduces the production scene-update watchdog hang
    /// (0x8BADF00D) seen when sending a follow-up after a tool-using turn:
    /// re-laying out the bubble's unbounded block tree (thousands of `Text` nodes
    /// across headings, wrapping paragraphs, nested lists, and wide tables) under
    /// the stack layout's multi-proposal sizing overruns the main-thread budget.
    /// The tool call/result are present so the turn matches the reported trigger,
    /// but the collapsed tool group is not laid out — the visible answer is the
    /// hang.
    private static func toolHeavyThread() -> [UITestChatMessage] {
        let chunk = """
        ## Section \u{2116}

        Paragraph with **bold**, _italic_, and `inline code` that wraps across
        several lines so the text engine has real work to measure on each pass.

        - point one
          - nested detail a
          - nested detail b
        - point two

        | Col A | Col B | Col C | Col D |
        | --- | --- | --- | --- |
        | alpha | beta | gamma | delta |
        | one | two | three | four |
        """
        let largeResult = (0..<1000)
            .map { chunk.replacingOccurrences(of: "\u{2116}", with: "\($0 + 1)") }
            .joined(separator: "\n\n")
        return [
            UITestChatMessage(
                internalID: 1,
                role: "user",
                content: "Search everything and summarize.",
                timestamp: "2026-06-08T09:00:00Z",
                toolCalls: nil,
                toolCallID: nil,
                attachments: nil
            ),
            UITestChatMessage(
                internalID: 2,
                role: "assistant",
                content: "",
                timestamp: "2026-06-08T09:00:01Z",
                toolCalls: [
                    UITestToolCall(
                        id: "call-large",
                        type: "function",
                        function: UITestToolFunction(
                            name: "search_notes",
                            arguments: #"{"query":"everything"}"#
                        )
                    ),
                ],
                toolCallID: nil,
                attachments: nil
            ),
            UITestChatMessage(
                internalID: 3,
                role: "tool",
                content: "Found 3 matching notes.",
                timestamp: "2026-06-08T09:00:02Z",
                toolCalls: nil,
                toolCallID: "call-large",
                attachments: nil
            ),
            UITestChatMessage(
                internalID: 4,
                role: "assistant",
                content: largeResult + "\n\nHere is what I found.",
                timestamp: "2026-06-08T09:00:03Z",
                toolCalls: nil,
                toolCallID: nil,
                attachments: nil
            ),
            // A small final message so the open-time scroll-to-bottom lands on
            // content that is reliably visible and unique to this seeded thread
            // (the huge answer above is a tall LazyVStack item whose interior is
            // not realized in the accessibility tree once scrolled). The follow-up
            // still re-lays out the huge answer just above it, exercising the
            // watchdog scenario.
            UITestChatMessage(
                internalID: 5,
                role: "assistant",
                content: "Tool-heavy summary complete.",
                timestamp: "2026-06-08T09:00:04Z",
                toolCalls: nil,
                toolCallID: nil,
                attachments: nil
            ),
        ]
    }

    private static func chatConversationSummaries() -> [UITestConversationSummary] {
        lock.withLock {
            chatMessages.map { conversationID, messages in
                UITestConversationSummary(
                    conversationID: conversationID,
                    lastMessage: messages.last?.content ?? "",
                    lastTimestamp: messages.last?.timestamp ?? "2026-06-08T12:00:00Z",
                    messageCount: messages.count
                )
            }
            .sorted { $0.lastTimestamp > $1.lastTimestamp }
        }
    }

    private static func encode<T: Encodable>(_ value: T) -> UITestResponse {
        do {
            return UITestResponse(
                statusCode: 200,
                data: try JSONEncoder().encode(value),
                headers: ["Content-Type": "application/json"]
            )
        } catch {
            return .json(#"{"detail":"Failed to encode response."}"#, statusCode: 500)
        }
    }
}

private struct UITestNote: Codable {
    let title: String
    let content: String
    let includeInPrompt: Bool
    let attachmentIds: [String]
    let visibilityLabels: [String]
    let isSkill: Bool
    let skillName: String?
    let skillDescription: String?

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
}

private struct UITestNoteSaveRequest: Decodable {
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

private struct UITestConversationSummary: Codable {
    let conversationID: String
    let lastMessage: String
    let lastTimestamp: String
    let messageCount: Int

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case lastMessage = "last_message"
        case lastTimestamp = "last_timestamp"
        case messageCount = "message_count"
    }
}

private struct UITestConversationListResponse: Codable {
    let conversations: [UITestConversationSummary]
    let count: Int
}

private struct UITestConversationMessagesResponse: Codable {
    let conversationID: String
    let messages: [UITestChatMessage]
    let count: Int
    let totalMessages: Int
    let hasMoreBefore: Bool
    let hasMoreAfter: Bool

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case messages
        case count
        case totalMessages = "total_messages"
        case hasMoreBefore = "has_more_before"
        case hasMoreAfter = "has_more_after"
    }
}

private struct UITestChatMessage: Codable {
    let internalID: Int
    let role: String
    let content: String
    let timestamp: String
    let toolCalls: [UITestToolCall]?
    let toolCallID: String?
    let attachments: [UITestAttachment]?

    enum CodingKeys: String, CodingKey {
        case internalID = "internal_id"
        case role
        case content
        case timestamp
        case toolCalls = "tool_calls"
        case toolCallID = "tool_call_id"
        case attachments
    }
}

private struct UITestToolCall: Codable {
    let id: String
    let type: String
    let function: UITestToolFunction
}

private struct UITestToolFunction: Codable {
    let name: String
    let arguments: String
}

private struct UITestAttachment: Codable {
    let attachmentID: String
    let name: String
    let contentURL: String
    let mimeType: String
    let size: Int

    enum CodingKeys: String, CodingKey {
        case attachmentID = "attachment_id"
        case name
        case contentURL = "content_url"
        case mimeType = "mime_type"
        case size
    }
}

private struct UITestChatStreamRequest: Decodable {
    let turnID: String
    let prompt: String
    let conversationID: String

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case prompt
        case conversationID = "conversation_id"
    }
}

private struct UITestChatTurnResponse: Encodable {
    let turnID: String
    let conversationID: String
    let firstSeq: Int

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case conversationID = "conversation_id"
        case firstSeq = "first_seq"
    }
}

private struct UITestResponse {
    let statusCode: Int
    let data: Data
    let headers: [String: String]

    static func json(
        _ json: String,
        statusCode: Int = 200,
        headers: [String: String] = ["Content-Type": "application/json"]
    ) -> UITestResponse {
        UITestResponse(statusCode: statusCode, data: Data(json.utf8), headers: headers)
    }

    func httpResponse(for request: URLRequest) -> HTTPURLResponse {
        HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!
    }
}

private extension URLRequest {
    var bodyData: Data {
        if let httpBody {
            return httpBody
        }
        guard let stream = httpBodyStream else {
            return Data()
        }
        var data = Data()
        stream.open()
        defer { stream.close() }

        let bufferSize = 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }

        while stream.hasBytesAvailable {
            let bytesRead = stream.read(buffer, maxLength: bufferSize)
            if bytesRead <= 0 {
                break
            }
            data.append(buffer, count: bytesRead)
        }
        return data
    }
}
#endif
