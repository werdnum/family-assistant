import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class ChatViewModelTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"

    override func setUp() {
        super.setUp()
        resetStoredAuth()
        KeychainHelper.save(key: "fa_api_token", string: "chat-view-model-token")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
        URLProtocol.registerClass(ChatMockBackendURLProtocol.self)
        ChatMockBackendURLProtocol.reset()
    }

    override func tearDown() {
        ChatMockBackendURLProtocol.reset()
        URLProtocol.unregisterClass(ChatMockBackendURLProtocol.self)
        resetStoredAuth()
        super.tearDown()
    }

    func testNewChatGeneratesWebConversationID() {
        let model = makeViewModel(conversationID: nil)

        model.startNewConversation()

        XCTAssertTrue(model.conversationID?.hasPrefix("web_conv_") == true)
        XCTAssertTrue(model.messages.isEmpty)
    }

    func testBackNavigationClearsSelectionButKeepsActiveConversation() async throws {
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_back",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let model = makeViewModel(conversationID: nil)

        // Open a conversation from the sidebar list.
        model.updateSelection("web_conv_back")
        try await waitUntil { model.conversationID == "web_conv_back" }
        XCTAssertEqual(model.conversationSelection, "web_conv_back")

        // NavigationSplitView writes nil when the user taps Back in compact width.
        model.updateSelection(nil)
        XCTAssertNil(model.conversationSelection)
        // The active conversation is preserved so the detail stays populated.
        XCTAssertEqual(model.conversationID, "web_conv_back")

        // Re-selecting the same conversation must take effect again. Before the
        // fix the selection was stuck on this id, so the tap was a no-op.
        model.updateSelection("web_conv_back")
        XCTAssertEqual(model.conversationSelection, "web_conv_back")
        try await waitUntil { model.conversationID == "web_conv_back" }
    }

    func testRouteToActiveConversationAfterBackRestoresSelection() async throws {
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_route_back",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let model = makeViewModel(conversationID: "web_conv_route_back")
        await model.selectConversation("web_conv_route_back")

        // User navigates back to the list, clearing the selection.
        model.updateSelection(nil)
        XCTAssertNil(model.conversationSelection)

        // A deep link/notification routes to the still-active conversation. The
        // selection must be restored so the thread reopens.
        await model.applyRoute(conversationID: "web_conv_route_back", initialPrompt: nil)

        XCTAssertEqual(model.conversationSelection, "web_conv_route_back")
        XCTAssertEqual(model.conversationID, "web_conv_route_back")
    }

    func testProfileSwitchPersistsSelectionAndCreatesNewConversation() {
        let model = makeViewModel(conversationID: "web_conv_existing")
        model.profiles = [
            ChatProfile(
                id: "default_assistant",
                description: "Default",
                llmModel: nil,
                availableTools: [],
                enabledMCPServers: [],
                delegationOnly: false
            ),
            ChatProfile(
                id: "research",
                description: "Research",
                llmModel: nil,
                availableTools: [],
                enabledMCPServers: [],
                delegationOnly: false
            ),
        ]

        model.changeProfile(to: "research")

        XCTAssertEqual(model.selectedProfileID, "research")
        XCTAssertEqual(UserDefaults.standard.string(forKey: "selectedProfileId"), "research")
        XCTAssertNotEqual(model.conversationID, "web_conv_existing")
        XCTAssertTrue(model.conversationID?.hasPrefix("web_conv_") == true)
    }

    func testSendDraftStreamsAssistantTextAndReloadsPersistedMessages() async throws {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["interface_type"] as? String, "web")
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_send")
                return .json(
                    #"{"turn_id":"turn-send","conversation_id":"web_conv_send","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_send/stream"):
                return .text(
                    """
                    event: turn_started
                    data: {"turn_id":"turn-send","seq":0}

                    event: text
                    data: {"content":"Streamed"}

                    event: turn_ended
                    data: {"turn_id":"turn-send","status":"complete"}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_send/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_send",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Persisted reply","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_send")
        model.draftText = "Hi"

        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Persisted reply"])
    }

    func testSendDraftAcknowledgesHighestSeqAfterTurnEnds() async throws {
        var ackedSeq: Int?
        // The client generates the turn_id and the server echoes it; the producer
        // tags every event with it and the send-and-watch flow filters events to
        // that turn. Echo the posted turn_id so the mock matches that contract.
        var streamedTurnID = "turn-ack"
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_ack","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_ack/stream"):
                return .text(
                    """
                    event: text
                    data: {"turn_id":"\(streamedTurnID)","content":"Hi","seq":4}

                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"complete","seq":5}

                    """
                )
            case ("POST", "/api/v1/chat/ack"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                ackedSeq = payload["ack_seq"] as? Int
                return .json("{}")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_ack/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_ack",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_ack")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }
        try await waitUntil { ackedSeq == 5 }

        XCTAssertEqual(ackedSeq, 5)
    }

    func testSendDraftAlreadyCompleteReloadsHistoryWithoutStreaming() async throws {
        var sawStream = false
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-dup","conversation_id":"web_conv_dup","first_seq":0,"already_complete":true}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_dup/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_dup",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Durable reply","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", (request.url?.path ?? "").hasSuffix("/stream") {
                    sawStream = true
                    return .text("event: turn_ended\ndata: {\"turn_id\":\"turn-dup\"}\n\n")
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_dup")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Durable reply"])
        XCTAssertFalse(sawStream)
    }

    func testSendMidTurnDropReloadsHistoryInsteadOfError() async throws {
        // A mobile connection dropping mid-turn (the stream delivers some text
        // then the socket dies before turn_ended) must NOT leave a permanent
        // error bubble: the turn keeps running durably on the server, so the
        // client reloads persisted history to surface the saved reply.
        var streamedTurnID = "turn-drop"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_drop","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_drop/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_drop",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Durable reply","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    return .droppedStream(
                        """
                        event: text
                        data: {"turn_id":"\(streamedTurnID)","content":"Partial","seq":1}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_drop")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // The durable reply is surfaced from persisted history, not a fabricated
        // "Done." and not a hard error bubble.
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Durable reply"])
        XCTAssertFalse(model.messages.contains { $0.text.contains("Sorry, I encountered an error") })
        XCTAssertFalse(model.messages.contains { $0.text == "Done." })
        XCTAssertEqual(model.errorMessage, "The connection was interrupted before the reply finished.")
    }

    func testSendStreamDroppedEventResubscribesFromLastSeqAndCompletes() async throws {
        // The hub drops an overflowed subscriber with a stream_dropped frame. The
        // client must resubscribe from the last applied seq and finish the turn,
        // rather than ignoring the frame and fabricating a completion.
        let streamRequests = AtomicCounter()
        var streamedTurnID = "turn-redrop"
        var resumeFromSeq: String?
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_redrop","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_redrop/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_redrop",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Resumed reply","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    if streamRequests.increment() == 1 {
                        return .text(
                            """
                            event: text
                            data: {"turn_id":"\(streamedTurnID)","content":"Partial","seq":1}

                            event: stream_dropped
                            data: {}

                            """
                        )
                    }
                    resumeFromSeq = Self.queryItems(from: request)["from_seq"]
                    return .text(
                        """
                        event: text
                        data: {"turn_id":"\(streamedTurnID)","content":" rest","seq":2}

                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","status":"complete","seq":3}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_redrop")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(streamRequests.value, 2)
        // The resubscribe resumes from the last applied seq (the partial text).
        XCTAssertEqual(resumeFromSeq, "1")
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Resumed reply"])
        XCTAssertNil(model.errorMessage)
    }

    func testSend410ReloadsHistoryWithoutError() async throws {
        // The turn's events rotated out of the hub buffer (410). There is nothing
        // to replay, but the reply is durably persisted — reload history rather
        // than show an error for a turn that likely succeeded.
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-410","conversation_id":"web_conv_410","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_410/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_410",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Persisted reply","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    return .json(#"{"detail":"events rotated out of buffer"}"#, statusCode: 410)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_410")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Persisted reply"])
        XCTAssertNil(model.errorMessage)
    }

    func testSendTransientServerErrorResubscribesAndCompletes() async throws {
        // A transient 5xx on the subscribe GET doesn't stop the durable turn; the
        // client resubscribes rather than failing the whole turn.
        let streamRequests = AtomicCounter()
        var streamedTurnID = "turn-5xx"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_5xx","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_5xx/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_5xx",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Recovered reply","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    if streamRequests.increment() == 1 {
                        return .json(#"{"detail":"temporary"}"#, statusCode: 503)
                    }
                    return .text(
                        """
                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","status":"complete","seq":1}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_5xx")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(streamRequests.value, 2)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Recovered reply"])
        XCTAssertNil(model.errorMessage)
    }

    func testSendFatalSubscribeErrorSurfacesError() async throws {
        // Recovery must not paper over a genuinely fatal failure (e.g. auth):
        // a 401 on the subscribe GET surfaces as an error rather than silently
        // reloading history.
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-401","conversation_id":"web_conv_401","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    return .json(#"{"detail":"expired token"}"#, statusCode: 401)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_401")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // The failure surfaces rather than being silently recovered. (Byte-stream
        // errors validate against an empty body, so the message is the generic
        // status text rather than the server's detail string.)
        XCTAssertEqual(model.errorMessage, "Chat request failed with status 401.")
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed)
    }

    func testSendThenClosedMidTurnRecoversReplyOnReopen() async throws {
        // End-to-end "send a message, then close the app mid-turn, then reopen":
        // the first client drops before the reply is persisted; a fresh client
        // reopening the conversation surfaces the durably saved reply.
        var replyPersisted = false
        var streamedTurnID = "turn-reopen"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_reopen","first_seq":0}"#
                )
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_reopen/messages"):
                let assistantRow = replyPersisted
                    ? #",{"internal_id":2,"role":"assistant","content":"Reply after reopen","timestamp":"2026-06-08T12:00:01Z"}"#
                    : ""
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_reopen",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"}\(assistantRow)
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    // The send-and-watch subscribe (follow=false) drops mid-turn;
                    // the reopen's live follow stream (follow=true) is benign so it
                    // doesn't spin and leak requests into later tests.
                    if Self.queryItems(from: request)["follow"] == "false" {
                        return .droppedStream(
                            """
                            event: text
                            data: {"turn_id":"\(streamedTurnID)","content":"Thinking","seq":1}

                            """
                        )
                    }
                    return .text("")
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        // Phase 1: send, the stream drops mid-turn before the reply is persisted.
        var sender: ChatViewModel? = makeViewModel(conversationID: "web_conv_reopen")
        sender?.draftText = "Hi"
        await sender?.sendDraft()
        try await waitUntil { sender?.isStreaming == false }
        XCTAssertEqual(sender?.messages.map(\.text), ["Hi"])
        // Closing the app tears down the in-flight view model.
        sender = nil

        // The durable turn finishes server-side while the app is closed.
        replyPersisted = true

        // Phase 2: reopen the conversation in a fresh client; the saved reply is
        // recovered from persisted history.
        var reopened: ChatViewModel? = makeViewModel(conversationID: "web_conv_reopen")
        await reopened?.selectConversation("web_conv_reopen")
        try await waitUntil { reopened?.messages.map(\.text) == ["Hi", "Reply after reopen"] }

        XCTAssertEqual(reopened?.messages.map(\.text), ["Hi", "Reply after reopen"])
        // Release so deinit cancels the idle live-updates loop before teardown.
        reopened = nil
    }

    func testApplyRouteProcessesEachNewInitialPrompt() async throws {
        var sentPrompts: [String] = []
        var conversationIDs: [String] = []
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                sentPrompts.append(try XCTUnwrap(payload["prompt"] as? String))
                let conversationID = try XCTUnwrap(payload["conversation_id"] as? String)
                conversationIDs.append(conversationID)
                return .json(
                    """
                    {"turn_id":"turn-route","conversation_id":"\(conversationID)","first_seq":0}
                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    return .text(
                        """
                        event: turn_ended
                        data: {"turn_id":"turn-route","status":"complete"}

                        """
                    )
                }
                if request.httpMethod == "GET", path.hasSuffix("/messages") {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_route",
                          "messages":[],
                          "count":0,
                          "total_messages":0,
                          "has_more_before":false,
                          "has_more_after":false
                        }
                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_seed")

        await model.applyRoute(conversationID: nil, initialPrompt: "First")
        try await waitUntil { !model.isStreaming }
        await model.applyRoute(conversationID: nil, initialPrompt: "Second")
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(sentPrompts, ["First", "Second"])
        XCTAssertEqual(conversationIDs.count, 2)
        XCTAssertNotEqual(conversationIDs.first, conversationIDs.last)
    }

    func testLiveUpdatesAutoReconnectAfterStreamDrops() async throws {
        // A mobile follow stream drops routinely; the view model must reopen it
        // on its own instead of stranding the disconnected indicator until a
        // manual tap. Each /stream response here finishes immediately (an empty
        // event stream), simulating a dropped connection, so a second /stream
        // request can only happen if the view model auto-reconnects.
        let streamRequests = AtomicCounter()
        let reconnected = expectation(description: "live updates auto-reconnect after a drop")
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                if streamRequests.increment() >= 2 {
                    reconnected.fulfill()
                }
                return .text("")
            }
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_reconnect",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_reconnect",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_reconnect")

        await fulfillment(of: [reconnected], timeout: 5)
        XCTAssertGreaterThanOrEqual(streamRequests.value, 2)

        // Release the view model so `deinit` cancels the fast reconnect loop;
        // otherwise it would keep hitting the mock and bleed into the next test.
        model = nil
    }

    func testPendingConfirmationsPollAndApprove() async throws {
        var approvalPayload: [String: Any]?
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(
                    """
                    {
                      "confirmations": [
                        {
                          "request_id": "req-1",
                          "tool_name": "calendar",
                          "tool_call_id": "call-1",
                          "confirmation_prompt": "Approve calendar?",
                          "args": {"title":"Dentist"},
                          "created_at": "2026-06-08T12:00:00Z",
                          "expires_at": "2026-06-08T12:01:00Z",
                          "timeout_seconds": 60,
                          "time_remaining_seconds": 45
                        }
                      ]
                    }
                    """
                )
            case ("POST", "/api/v1/chat/confirm_tool"):
                approvalPayload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                return .json(#"{"success":true,"message":"approved"}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_confirm")
        await model.confirm(
            ChatPendingConfirmation(
                requestID: "req-1",
                toolName: "calendar",
                toolCallID: "call-1",
                confirmationPrompt: "Approve calendar?",
                args: ["title": .string("Dentist")],
                createdAt: nil,
                expiresAt: nil,
                timeoutSeconds: 60,
                timeRemainingSeconds: 45
            ),
            approved: true
        )

        XCTAssertEqual(approvalPayload?["approving_interface"] as? String, "ios")
        XCTAssertEqual(approvalPayload?["request_id"] as? String, "req-1")
    }

    func testAttachmentUploadStateTransitionAndRemoval() async throws {
        let fileURL = FileManager.default.temporaryDirectory.appendingPathComponent("vm-upload.txt")
        try Data("file".utf8).write(to: fileURL)

        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/attachments/upload"):
                return .json(
                    """
                    {
                      "attachment_id": "22222222-2222-2222-2222-222222222222",
                      "filename": "vm-upload.txt",
                      "content_type": "text/plain",
                      "size": 4,
                      "url": "/api/attachments/22222222-2222-2222-2222-222222222222"
                    }
                    """
                )
            case ("DELETE", "/api/attachments/22222222-2222-2222-2222-222222222222"):
                return .json(#"{"message":"deleted"}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_upload")
        await model.addAttachment(fileURL: fileURL)
        try await waitUntil { model.draftAttachments.first?.uploadState == .uploaded }

        let attachment = try XCTUnwrap(model.draftAttachments.first)
        XCTAssertEqual(attachment.attachmentID, "22222222-2222-2222-2222-222222222222")

        await model.removeDraftAttachment(attachment)

        XCTAssertTrue(model.draftAttachments.isEmpty)
    }

    func testRemoveUploadedDraftAttachmentSurfacesDeleteFailureAndKeepsAttachment() async {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("DELETE", "/api/attachments/uploaded-id"):
                return .json(#"{"detail":"expired token"}"#, statusCode: 401)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_delete_failure")
        let attachment = makeAttachment(uploadState: .uploaded)
        model.draftAttachments = [attachment]

        await model.removeDraftAttachment(attachment)

        XCTAssertEqual(model.draftAttachments, [attachment])
        XCTAssertTrue(model.errorMessage?.contains("Could not remove attachment") == true)
    }

    func testRemoveUploadingDraftAttachmentKeepsAttachmentUntilUploadFinishes() async {
        let model = makeViewModel(conversationID: "web_conv_uploading_remove")
        let attachment = makeAttachment(uploadState: .uploading)
        model.draftAttachments = [attachment]

        await model.removeDraftAttachment(attachment)

        XCTAssertEqual(model.draftAttachments, [attachment])
        XCTAssertEqual(model.errorMessage, "Wait for attachment upload to finish before removing.")
    }

    func testDownloadAttachmentForSharingSurfacesFailure() async {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/attachments/uploaded-id"):
                return .json(#"{"detail":"expired token"}"#, statusCode: 401)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_download_failure")
        let attachment = makeAttachment(uploadState: .uploaded)

        let shareURL = await model.downloadAttachmentForSharing(attachment)

        XCTAssertNil(shareURL)
        XCTAssertTrue(model.errorMessage?.contains("Could not download attachment") == true)
        XCTAssertTrue(model.errorMessage?.contains("expired token") == true)
    }

    func testAddImageDataUsesProvidedMimeTypeAndFilename() async throws {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/attachments/upload"):
                let body = String(data: request.bodyData, encoding: .utf8)
                XCTAssertTrue(body?.contains(#"filename="photo.png""#) == true)
                XCTAssertTrue(body?.contains("Content-Type: image/png") == true)
                return .json(
                    """
                    {
                      "attachment_id": "33333333-3333-3333-3333-333333333333",
                      "filename": "photo.png",
                      "content_type": "image/png",
                      "size": 4,
                      "url": "/api/attachments/33333333-3333-3333-3333-333333333333"
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_image")

        await model.addImageData(Data("file".utf8), filename: "photo.png", mimeType: "image/png")
        try await waitUntil { model.draftAttachments.first?.uploadState == .uploaded }

        let attachment = try XCTUnwrap(model.draftAttachments.first)
        XCTAssertEqual(attachment.name, "photo.png")
        XCTAssertEqual(attachment.mimeType, "image/png")
    }

    func testPickedHEICPhotosAreAcceptedForJPEGUpload() {
        XCTAssertTrue(ChatConstants.allowedPhotoPickerMIMETypes.contains("image/heic"))
        XCTAssertTrue(ChatConstants.allowedPhotoPickerMIMETypes.contains("image/heif"))
        XCTAssertFalse(ChatConstants.allowedAttachmentMIMETypes.contains("image/heic"))
        XCTAssertEqual(ChatConstants.uploadMIMEType(forPickedPhotoMIMEType: "image/heic"), "image/jpeg")
        XCTAssertEqual(ChatConstants.uploadMIMEType(forPickedPhotoMIMEType: "image/heif"), "image/jpeg")
        XCTAssertEqual(
            ChatConstants.uploadFilenameExtension(forPickedPhotoMIMEType: "image/heic", fallback: "heic"),
            "jpg"
        )
        XCTAssertEqual(
            ChatConstants.uploadFilenameExtension(forPickedPhotoMIMEType: "image/png", fallback: "png"),
            "png"
        )
    }

    func testUploadingAttachmentBlocksSendAndPreservesDraft() async {
        let model = makeViewModel(conversationID: "web_conv_uploading")
        model.draftText = "Send later"
        model.draftAttachments = [
            makeAttachment(uploadState: .uploading),
        ]

        await model.sendDraft()

        XCTAssertFalse(model.canSendDraft)
        XCTAssertEqual(model.draftText, "Send later")
        XCTAssertEqual(model.draftAttachments.first?.uploadState, .uploading)
        XCTAssertTrue(model.messages.isEmpty)
        XCTAssertEqual(model.errorMessage, "Wait for attachments to finish uploading before sending.")
    }

    func testFailedAttachmentBlocksSendAndPreservesDraft() async {
        let model = makeViewModel(conversationID: "web_conv_failed")
        model.draftText = "Fix attachment"
        model.draftAttachments = [
            makeAttachment(uploadState: .failed),
        ]

        await model.sendDraft()

        XCTAssertFalse(model.canSendDraft)
        XCTAssertEqual(model.draftText, "Fix attachment")
        XCTAssertEqual(model.draftAttachments.first?.uploadState, .failed)
        XCTAssertTrue(model.messages.isEmpty)
        XCTAssertEqual(model.errorMessage, "Remove failed attachments before sending.")
    }

    func testConfirmUpdatesToolStatusBeforeRemovingPendingConfirmation() async {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/confirm_tool"):
                return .json(#"{"success":true,"message":"approved"}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let confirmation = ChatPendingConfirmation(
            requestID: "req-status",
            toolName: "calendar",
            toolCallID: "call-status",
            confirmationPrompt: "Approve?",
            args: [:],
            createdAt: nil,
            expiresAt: nil,
            timeoutSeconds: 60,
            timeRemainingSeconds: 45
        )
        let model = makeViewModel(conversationID: "web_conv_status")
        model.pendingConfirmations = [confirmation]
        model.messages = [
            ChatMessage(
                id: "assistant-status",
                role: .assistant,
                text: "",
                createdAt: Date(),
                toolCalls: [
                    ChatToolCall(
                        id: "call-status",
                        name: "calendar",
                        argumentsText: "{}",
                        resultText: nil,
                        attachments: [],
                        status: .awaitingApproval
                    ),
                ],
                attachments: [],
                isLoading: false,
                status: .running,
                processingProfileID: "default_assistant",
                errorTraceback: nil
            ),
        ]

        await model.confirm(confirmation, approved: true)

        XCTAssertTrue(model.pendingConfirmations.isEmpty)
        XCTAssertEqual(model.messages.first?.toolCalls.first?.status, .approved)
    }

    private func makeViewModel(
        conversationID: String?,
        liveReconnectInitialDelaySeconds: Double = 2,
        liveReconnectMaxDelaySeconds: Double = 30
    ) -> ChatViewModel {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return ChatViewModel(
            authManager: authManager,
            conversationID: conversationID,
            initialPrompt: nil,
            liveReconnectInitialDelaySeconds: liveReconnectInitialDelaySeconds,
            liveReconnectMaxDelaySeconds: liveReconnectMaxDelaySeconds
        )
    }

    private func makeAttachment(uploadState: ChatAttachmentUploadState) -> ChatAttachment {
        ChatAttachment(
            id: "attachment-\(uploadState.rawValue)",
            attachmentID: uploadState == .uploaded ? "uploaded-id" : nil,
            type: .document,
            name: "draft.txt",
            contentURL: uploadState == .uploaded ? "/api/attachments/uploaded-id" : nil,
            mimeType: "text/plain",
            size: 5,
            localFileURL: nil,
            uploadState: uploadState,
            errorMessage: uploadState == .failed ? "Upload failed" : nil
        )
    }

    private func resetStoredAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
        UserDefaults.standard.removeObject(forKey: "lastConversationId")
        UserDefaults.standard.removeObject(forKey: "selectedProfileId")
    }

    private static func jsonObject(from request: URLRequest) throws -> Any {
        try JSONSerialization.jsonObject(with: request.bodyData)
    }

    private static func queryItems(from request: URLRequest) -> [String: String] {
        let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        return Dictionary(uniqueKeysWithValues: items.compactMap { item in
            item.value.map { (item.name, $0) }
        })
    }

    private func waitUntil(
        timeout: TimeInterval = 4,
        predicate: @escaping @MainActor () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate() {
                return
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTFail("Timed out waiting for predicate")
    }
}

/// Lock-guarded counter for tallying mock requests off the URLProtocol loading
/// thread.
private final class AtomicCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    @discardableResult
    func increment() -> Int {
        lock.withLock {
            count += 1
            return count
        }
    }

    var value: Int {
        lock.withLock { count }
    }
}
