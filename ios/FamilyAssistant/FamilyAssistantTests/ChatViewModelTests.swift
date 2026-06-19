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

    func testLaunchRestoresRecentlyActiveConversation() {
        storeLastConversation("web_conv_recent", activeSecondsAgo: 60)

        let model = makeViewModel(conversationID: nil)

        XCTAssertEqual(model.conversationID, "web_conv_recent")
        XCTAssertEqual(
            model.conversationSelection,
            "web_conv_recent",
            "A conversation used within the restore window should reopen on launch."
        )
    }

    func testLaunchWithStaleConversationLandsOnList() {
        storeLastConversation("web_conv_stale", activeSecondsAgo: 16 * 60)

        let model = makeViewModel(conversationID: nil)

        XCTAssertNil(
            model.conversationSelection,
            "A conversation last used beyond the restore window should not reopen; land on the list."
        )
        XCTAssertNotEqual(model.conversationID, "web_conv_stale")
        XCTAssertTrue(model.conversationID?.hasPrefix("web_conv_") == true)
        XCTAssertEqual(
            UserDefaults.standard.string(forKey: "lastConversationId"),
            "web_conv_stale",
            "Landing on the list must not overwrite the stored last conversation."
        )
    }

    func testLaunchWithoutActivityTimestampLandsOnList() {
        UserDefaults.standard.set("web_conv_legacy", forKey: "lastConversationId")
        UserDefaults.standard.removeObject(forKey: "lastConversationActiveAt")

        let model = makeViewModel(conversationID: nil)

        XCTAssertNil(
            model.conversationSelection,
            "Without a recorded activity time (e.g. upgrade), launch should land on the list."
        )
    }

    func testLaunchWithNoStoredConversationLandsOnList() {
        let model = makeViewModel(conversationID: nil)

        XCTAssertNil(model.conversationSelection)
        XCTAssertTrue(model.conversationID?.hasPrefix("web_conv_") == true)
    }

    func testExplicitConversationIDIgnoresRestoreWindow() {
        storeLastConversation("web_conv_stale", activeSecondsAgo: 16 * 60)

        let model = makeViewModel(conversationID: "web_conv_deeplink")

        XCTAssertEqual(model.conversationID, "web_conv_deeplink")
        XCTAssertEqual(
            model.conversationSelection,
            "web_conv_deeplink",
            "An explicit route/deep-link selection always opens regardless of the restore window."
        )
    }

    /// `groupedMessages` regroups the whole held thread, so an agentic turn whose
    /// tool-calling steps landed in the thread as separate `ChatMessage`s — e.g.
    /// because they arrived across two incremental `mergeNewMessages` fetches —
    /// still collapses into a single tool bubble for display, while the raw
    /// `messages` array stays one-to-one with the persisted backend messages.
    func testGroupedMessagesCollapsesToolBubblesSplitAcrossMerges() {
        let model = makeViewModel(conversationID: "web_conv_split")
        func toolStep(id: String, callID: String, minute: Int) -> ChatMessage {
            ChatMessage(
                id: id,
                role: .assistant,
                text: "",
                createdAt: Date(timeIntervalSince1970: TimeInterval(minute * 60)),
                toolCalls: [
                    ChatToolCall(
                        id: callID,
                        name: "search_notes",
                        argumentsText: "{}",
                        resultText: "{}",
                        attachments: [],
                        status: .complete
                    ),
                ],
                attachments: [],
                isLoading: false,
                status: .complete,
                processingProfileID: nil,
                errorTraceback: nil
            )
        }

        // Two tool-calling steps of one turn that arrived as separate bubbles.
        model.messages = [
            ChatMessage(
                id: "msg_1",
                role: .user,
                text: "Find everything",
                createdAt: Date(timeIntervalSince1970: 0),
                toolCalls: [],
                attachments: [],
                isLoading: false,
                status: .complete,
                processingProfileID: nil,
                errorTraceback: nil
            ),
            toolStep(id: "msg_10", callID: "a", minute: 1),
            toolStep(id: "msg_12", callID: "b", minute: 2),
        ]

        // Raw thread is untouched (cursor/identity preserved); display collapses.
        XCTAssertEqual(model.messages.count, 3)
        let grouped = model.groupedMessages
        XCTAssertEqual(grouped.map(\.role), [.user, .assistant])
        XCTAssertEqual(grouped.last?.toolCalls.map(\.id), ["a", "b"])
    }

    private func plainMessage(index: Int) -> ChatMessage {
        ChatMessage(
            id: "msg_\(index)",
            role: index.isMultiple(of: 2) ? .user : .assistant,
            text: "Message \(index)",
            createdAt: Date(timeIntervalSince1970: TimeInterval(index * 60)),
            toolCalls: [],
            attachments: [],
            isLoading: false,
            status: .complete,
            processingProfileID: nil,
            errorTraceback: nil
        )
    }

    func testVisibleMessagesCapToRecentWindow() {
        let model = makeViewModel(conversationID: "web_conv_window")
        let total = ChatViewModel.initialDisplayedMessageCount + 15
        model.messages = (0..<total).map(plainMessage(index:))

        let visible = model.visibleGroupedMessages
        XCTAssertEqual(visible.count, ChatViewModel.initialDisplayedMessageCount)
        XCTAssertTrue(model.hasEarlierMessages)
        // The window is the most-recent suffix: newest is shown, oldest is hidden.
        XCTAssertEqual(visible.last?.id, "msg_\(total - 1)")
        XCTAssertEqual(visible.first?.id, "msg_\(total - ChatViewModel.initialDisplayedMessageCount)")
    }

    func testShowEarlierMessagesWidensWindowToWholeThread() {
        let model = makeViewModel(conversationID: "web_conv_window")
        let total = ChatViewModel.initialDisplayedMessageCount + 15
        model.messages = (0..<total).map(plainMessage(index:))

        let initialVisible = model.visibleGroupedMessages.count
        model.showEarlierMessages()
        XCTAssertGreaterThan(model.visibleGroupedMessages.count, initialVisible)

        // Widening enough reveals the whole thread and clears the earlier flag.
        while model.hasEarlierMessages {
            model.showEarlierMessages()
        }
        XCTAssertEqual(model.visibleGroupedMessages.count, total)
        XCTAssertEqual(model.visibleGroupedMessages.first?.id, "msg_0")
    }

    func testShortThreadShowsEverythingWithoutEarlierControl() {
        let model = makeViewModel(conversationID: "web_conv_short")
        model.messages = (0..<3).map(plainMessage(index:))

        XCTAssertEqual(model.visibleGroupedMessages.count, 3)
        XCTAssertFalse(model.hasEarlierMessages)
    }

    func testStartingNewConversationResetsTheWindow() {
        let model = makeViewModel(conversationID: "web_conv_window")
        model.messages = (0..<(ChatViewModel.initialDisplayedMessageCount + 15)).map(plainMessage(index:))
        model.showEarlierMessages()
        XCTAssertGreaterThan(model.displayedMessageLimit, ChatViewModel.initialDisplayedMessageCount)

        model.startNewConversation()

        XCTAssertEqual(model.displayedMessageLimit, ChatViewModel.initialDisplayedMessageCount)
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
        var streamedTurnID = "turn-send"
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["interface_type"] as? String, "web")
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_send")
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_send","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_send/stream"):
                return .text(
                    """
                    event: turn_started
                    data: {"turn_id":"\(streamedTurnID)","seq":0}

                    event: text
                    data: {"content":"Streamed"}

                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"complete"}

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
        // error bubble. The client resumes the stream a few times; when every
        // resume keeps dropping at the same seq (no forward progress) it gives up
        // and reloads persisted history to surface the durably saved reply.
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

        let model = makeViewModel(
            conversationID: "web_conv_drop",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 2,
            streamResumeLivenessSeconds: 60
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // The durable reply is surfaced from persisted history, not a fabricated
        // "Done." and not a hard error bubble.
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Durable reply"])
        XCTAssertFalse(model.messages.contains { $0.text.contains("Sorry, I encountered an error") })
        XCTAssertFalse(model.messages.contains { $0.text == "Done." })
        // Recovery is silent: a recovered disconnect must not pop a modal
        // "Chat Error" alert for a reply that actually succeeded.
        XCTAssertNil(model.errorMessage)
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
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_redrop/stream" {
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
        // The resubscribe resumes from just after the last applied seq (the
        // partial text was seq 1). The server replays seq >= from_seq, so
        // resuming from seq 1 would re-apply the partial text.
        XCTAssertEqual(resumeFromSeq, "2")
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Resumed reply"])
        XCTAssertNil(model.errorMessage)
    }

    func testSendResumesTokenStreamAfterMidTurnInterruption() async throws {
        // The proxy severs the send-and-watch stream mid-turn — a CLEAN EOF with
        // no turn_ended (e.g. an Envoy 15s request timeout) right after the first
        // tool round. The turn keeps running durably, so the client must
        // resubscribe from the last applied seq and RESUME live streaming to
        // completion, not strand on the disconnected indicator until turn_ended.
        let streamRequests = AtomicCounter()
        var resumeFromSeq: String?
        var streamedTurnID = "turn-resume"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_resume","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_resume/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_resume",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Looking all good","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_resume/stream" {
                    if streamRequests.increment() == 1 {
                        // First leg: the first tool round, then a clean EOF with no
                        // turn_ended — the severed connection.
                        return .text(
                            """
                            event: text
                            data: {"turn_id":"\(streamedTurnID)","content":"Looking","seq":1}

                            event: tool_call
                            data: {"turn_id":"\(streamedTurnID)","tool_call":{"id":"call-1","type":"function","function":{"name":"query_database","arguments":"{}"}},"seq":2}

                            event: tool_result
                            data: {"turn_id":"\(streamedTurnID)","tool_call_id":"call-1","result":"ok","seq":3}

                            """
                        )
                    }
                    resumeFromSeq = Self.queryItems(from: request)["from_seq"]
                    // Resume leg: the rest of the reply plus turn_ended.
                    return .text(
                        """
                        event: text
                        data: {"turn_id":"\(streamedTurnID)","content":" all good","seq":4}

                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","status":"complete","seq":5}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_resume")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // The clean mid-turn EOF is resumed from just after the last applied seq
        // (tool_result was seq 3), and streaming continues to turn_ended instead
        // of stranding — the completed reply surfaces rather than the turn hanging.
        XCTAssertEqual(streamRequests.value, 2)
        XCTAssertEqual(resumeFromSeq, "4")
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Looking all good"])
        let assistant = try XCTUnwrap(model.messages.last)
        XCTAssertEqual(assistant.status, .complete)
        XCTAssertNil(model.errorMessage)
    }

    func testSendQuietRunningTurnKeepsResumingUntilItCompletes() async throws {
        // A healthy but quiet turn — a long tool call running through several proxy
        // timeouts with no streamed frames — must NOT be abandoned. Each resume
        // ends with no new seq, but the server holds `follow=false` open only while
        // a turn is still running, so a held-open resume resets the give-up streak
        // rather than counting toward it. Here every resume returns an empty
        // (held-open) stream until the turn finally completes; with a tiny liveness
        // threshold the real round-trips read as held-open, so the client keeps
        // resuming well past `maxConsecutiveStreamResumes` and finishes the turn.
        let streamRequests = AtomicCounter()
        var streamedTurnID = "turn-quiet"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_quiet","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_quiet/messages"):
                return .json(
                    """
                    {"conversation_id":"web_conv_quiet","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Done thinking","timestamp":"2026-06-08T12:00:01Z"}],"count":2,"total_messages":2,"has_more_before":false,"has_more_after":false}
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_quiet/stream" {
                    // The first four subscribes return an empty, held-open stream (a
                    // quiet running turn the proxy keeps cutting); the fifth finally
                    // streams turn_ended.
                    if streamRequests.increment() < 5 {
                        return .text("")
                    }
                    return .text(
                        """
                        event: text
                        data: {"turn_id":"\(streamedTurnID)","content":"Done thinking","seq":1}

                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","status":"complete","seq":2}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_quiet",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 1,
            streamResumeLivenessSeconds: 0.00001
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // Despite `maxConsecutiveStreamResumes: 1`, the held-open empty resumes did
        // not trip the give-up bound — the client kept resuming through all five
        // subscribes and completed the turn rather than reloading history early.
        XCTAssertEqual(streamRequests.value, 5)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Done thinking"])
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
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_5xx/stream" {
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
        // Every resume keeps dropping at the same seq, so the client gives up
        // after a bounded streak and the placeholder is dropped on recovery.
        var sender: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_reopen",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 2,
            streamResumeLivenessSeconds: 60
        )
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

    func testConcurrentOtherTurnSeqIsNotAckedOnResubscribe() async throws {
        // Another device can start a concurrent turn whose events interleave on
        // this stream. The send-and-watch flow ignores those events, so it must
        // not advance its ack cursor past them: acking a skipped turn's seq would
        // let the hub treat that other turn as delivered and suppress its
        // disconnect push. The resume cursor and ack_seq must reflect only our
        // own turn's highest applied seq.
        // Our turn's events carry the client-generated turn id (echoed by the
        // mock). Two non-ours, higher-seq events interleave: a no-turn_id
        // `message` nudge (seq 5) and a concurrent other-device turn_ended
        // (seq 6). Neither may advance our ack/resume cursor.
        let streamRequests = AtomicCounter()
        var streamedTurnID = "turn-mine"
        var resumeFromSeq: String?
        var resumeAckSeq: String?
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_concurrent","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_concurrent/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_concurrent",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"My reply","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                // Scope to this conversation's stream so a leaked follow-stream
                // request from a prior test (app-hosted unit bundle) can't bump
                // the counter.
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_concurrent/stream" {
                    if streamRequests.increment() == 1 {
                        // Our text (seq 1, our turn id → applied + acked), then a
                        // no-turn_id message nudge (seq 5) and a concurrent OTHER
                        // device's turn_ended (seq 6) — both must be ignored for
                        // ack purposes — then a drop.
                        return .text(
                            """
                            event: text
                            data: {"turn_id":"\(streamedTurnID)","content":"Partial","seq":1}

                            event: message
                            data: {"seq":5}

                            event: turn_ended
                            data: {"turn_id":"turn-other-device","status":"complete","seq":6}

                            event: stream_dropped
                            data: {}

                            """
                        )
                    }
                    let items = Self.queryItems(from: request)
                    resumeFromSeq = items["from_seq"]
                    resumeAckSeq = items["ack_seq"]
                    return .text(
                        """
                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","status":"complete","seq":7}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_concurrent")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { streamRequests.value >= 2 }
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(streamRequests.value, 2)
        // Resume/ack reflect only our turn (seq 1) — not the no-turn_id nudge
        // (seq 5) nor the other-device turn (seq 6): resume from 2 (our seq 1 + 1),
        // ack 1.
        XCTAssertEqual(resumeAckSeq, "1")
        XCTAssertEqual(resumeFromSeq, "2")
    }

    func testSendStartTurnFailureSurfacesError() async throws {
        // If the turn never starts (POST /turns fails), there is no durable turn
        // to recover, so the error must surface rather than silently reload an
        // empty history. The user's prompt is preserved in the thread.
        var sawStream = false
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(#"{"detail":"server is down"}"#, statusCode: 500)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_nostart/stream" {
                    sawStream = true
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_nostart")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertFalse(sawStream)
        XCTAssertEqual(model.errorMessage, "server is down")
        XCTAssertEqual(model.messages.first?.text, "Hi")
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed)
    }

    func testSendRepeatedNoProgressDropsGiveUpAndReloadHistory() async throws {
        // A stream that keeps dropping at the same seq (no new events on resume)
        // must not reopen forever: after `maxConsecutiveStreamResumes` resumes
        // with no forward progress the client gives up and reloads history.
        let streamRequests = AtomicCounter()
        var streamedTurnID = "turn-twice"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_twice","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_twice/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_twice",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Reloaded reply","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_twice/stream" {
                    streamRequests.increment()
                    return .text(
                        """
                        event: text
                        data: {"turn_id":"\(streamedTurnID)","content":"Partial","seq":1}

                        event: stream_dropped
                        data: {}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_twice",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 2,
            streamResumeLivenessSeconds: 60
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // The first subscribe plus exactly `maxConsecutiveStreamResumes` (2)
        // no-progress resumes, then it stops — no further attempts.
        XCTAssertEqual(streamRequests.value, 3)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Reloaded reply"])
        // Recovery is silent — no spurious modal error for a recovered turn.
        XCTAssertNil(model.errorMessage)
    }

    func testSendFailedTurnEndedShowsErrorNotDone() async throws {
        // A turn that ends in failure must show the error, not a fabricated
        // "Done." reply. The persisted error row surfaces on the reload.
        var streamedTurnID = "turn-failed"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_failed","first_seq":0}"#
                )
            case ("POST", "/api/v1/chat/ack"):
                return .json("{}")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_failed/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_failed",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"error","content":"Boom","timestamp":"2026-06-08T12:00:01Z","error_traceback":"trace"}
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
                    return .text(
                        """
                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","status":"failed","error":"Boom","seq":1}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_failed")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertFalse(model.messages.contains { $0.text == "Done." })
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed)
        XCTAssertTrue(assistant.text.contains("Boom"))
    }

    func testInterruptedSendDoesNotAcknowledge() async throws {
        // The disconnect push is suppressed only by an explicit ack of the
        // turn_ended seq. An interrupted send never saw turn_ended, so it must
        // NOT ack — otherwise the offline user would silently lose the push.
        let ackRequests = AtomicCounter()
        var streamedTurnID = "turn-noack"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_noack","first_seq":0}"#
                )
            case ("POST", "/api/v1/chat/ack"):
                // Scope the count to this conversation so a fire-and-forget ack
                // from a previous test can't bleed in.
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   body["conversation_id"] as? String == "web_conv_noack" {
                    ackRequests.increment()
                }
                return .json("{}")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_noack/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_noack",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"}
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

        let model = makeViewModel(
            conversationID: "web_conv_noack",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 2,
            streamResumeLivenessSeconds: 60
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(ackRequests.value, 0)
    }

    func testLiveFollowSurfacesTurnCompletedElsewhere() async throws {
        // The follow stream's whole purpose: a turn that completes elsewhere (or
        // while we were briefly disconnected) surfaces in the open conversation
        // with no active send, by reloading persisted history.
        let ackRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                // The initial full load (no `after`) shows only the earlier
                // message; the incremental reload (with `after`) returns the
                // newly persisted reply.
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_elsewhere",
                          "messages":[
                            {"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}
                          ],
                          "count":1,
                          "total_messages":1,
                          "has_more_before":false,
                          "has_more_after":false
                        }
                        """
                    )
                }
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_elsewhere",
                      "messages":[
                        {"internal_id":2,"role":"assistant","content":"Reply elsewhere","timestamp":"2026-06-08T12:05:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "POST", path == "/api/v1/chat/ack" {
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   body["conversation_id"] as? String == "web_conv_elsewhere" {
                    ackRequests.increment()
                }
                return .json("{}")
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                return .text(
                    """
                    event: turn_ended
                    data: {"turn_id":"turn-elsewhere","status":"complete","seq":7}

                    """
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        // Default reconnect delays: the merge happens on the first connect, so
        // there's no need for a fast loop that could leak into later tests.
        var model: ChatViewModel? = makeViewModel(conversationID: "web_conv_elsewhere")
        await model?.selectConversation("web_conv_elsewhere")

        try await waitUntil { model?.messages.map(\.text) == ["Earlier", "Reply elsewhere"] }
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "Reply elsewhere"])
        // The turn_ended seen on the follow stream is acked so the server
        // suppresses the disconnect push for a reply we've now surfaced.
        try await waitUntil { ackRequests.value >= 1 }

        // Release so deinit cancels the fast reconnect loop before teardown.
        model = nil
    }

    func testLiveFollowCatchesUpHistoryWhenStreamConnectKeepsFailing() async throws {
        // Gap A: when the production front door makes SSE unusable, the follow
        // stream's `connectEvents` keeps throwing. The only history catch-up
        // (`mergeNewMessages`) used to be gated behind a *successful* connect, so
        // a turn that finished while the stream was down never surfaced until a
        // manual list refresh + reopen (both plain, short HTTP requests the proxy
        // handles fine). The loop must catch up over plain HTTP on every failed
        // cycle, so content recovers without the SSE stream ever succeeding.
        let streamConnects = AtomicCounter()
        let incrementalMerges = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_catchup",
                          "messages":[
                            {"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}
                          ],
                          "count":1,
                          "total_messages":1,
                          "has_more_before":false,
                          "has_more_after":false
                        }
                        """
                    )
                }
                incrementalMerges.increment()
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_catchup",
                      "messages":[
                        {"internal_id":2,"role":"assistant","content":"Reply after drop","timestamp":"2026-06-08T12:05:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                // The front door mangles SSE framing: every connect attempt fails
                // at the call site (connectEvents validates before yielding).
                streamConnects.increment()
                throw URLError(.cannotParseResponse)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_catchup",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_catchup")

        // The reply surfaces even though no SSE connect ever succeeds.
        try await waitUntil { model?.messages.map(\.text) == ["Earlier", "Reply after drop"] }
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "Reply after drop"])
        XCTAssertGreaterThanOrEqual(streamConnects.value, 1)
        XCTAssertGreaterThanOrEqual(incrementalMerges.value, 1)
        // Content recovery is decoupled from connection health: the disconnected
        // indicator is still surfaced, but the thread is no longer stranded.
        XCTAssertEqual(model?.liveUpdatesConnected, false)

        // Release so deinit cancels the fast reconnect loop before teardown.
        model = nil
    }

    func testReconnectLiveUpdatesCatchesUpHistoryAfterForegrounding() async throws {
        // The foreground hook (scenePhase -> .active) calls reconnectLiveUpdates().
        // While the app is backgrounded the follow stream is held open but the
        // app is suspended, so the passive loop — blocked on a healthy hanging
        // stream — never re-merges. A turn that finishes during that window must
        // surface on foreground via the explicit reconnect's catch-up, not only
        // when the stream happens to drop.
        let turnFinished = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_fg",
                          "messages":[
                            {"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}
                          ],
                          "count":1,
                          "total_messages":1,
                          "has_more_before":false,
                          "has_more_after":false
                        }
                        """
                    )
                }
                if turnFinished.value == 0 {
                    return .json(
                        #"{"conversation_id":"web_conv_fg","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_fg",
                      "messages":[
                        {"internal_id":2,"role":"assistant","content":"Reply while backgrounded","timestamp":"2026-06-08T12:05:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                // A healthy stream held open with no events: the passive follow
                // loop blocks here and cannot re-merge on its own. Each request
                // gets its own controller; teardown/cancel releases it.
                return .hangingStream("", controller: HangingStream())
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(conversationID: "web_conv_fg")
        await model?.selectConversation("web_conv_fg")
        try await waitUntil { model?.messages.map(\.text) == ["Earlier"] }

        // The turn finishes while we're backgrounded; the held stream delivered
        // nothing. The passive loop is still blocked on its hanging connection.
        turnFinished.increment()
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier"])

        // Foregrounding kicks the explicit reconnect, which catches up history.
        await model?.reconnectLiveUpdates()
        try await waitUntil {
            model?.messages.map(\.text) == ["Earlier", "Reply while backgrounded"]
        }
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "Reply while backgrounded"])

        // Release so deinit cancels the held stream before teardown.
        model = nil
    }

    func testLiveFollowStreamsTokensForTurnStartedElsewhere() async throws {
        // M2: the always-on follow stream now carries token frames, so a turn
        // started elsewhere (another device, or after our own send gave up)
        // streams live into a freshly created assistant bubble instead of only
        // appearing as a history reload when it ends. On `turn_ended` the live
        // `local_` bubble reconciles to the persisted reply.
        let turnFinished = AtomicCounter()
        let acks = AtomicCounter()
        let controller = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_remote",
                          "messages":[
                            {"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}
                          ],
                          "count":1,
                          "total_messages":1,
                          "has_more_before":false,
                          "has_more_after":false
                        }
                        """
                    )
                }
                if turnFinished.value == 0 {
                    return .json(
                        #"{"conversation_id":"web_conv_remote","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_remote",
                      "messages":[
                        {"internal_id":2,"role":"assistant","content":"Hello world","timestamp":"2026-06-08T12:05:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "POST", path == "/api/v1/chat/ack" {
                acks.increment()
                return .json("{}")
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                // A turn already running elsewhere: stream its text tokens, then
                // hold open until the test finishes it with turn_ended. Each frame
                // is terminated with an explicit blank line so a held partial
                // chunk can't merge with the later turn_ended frame.
                return .hangingStream(
                    "event: text\ndata: {\"turn_id\":\"turn-remote\",\"content\":\"Hello\",\"seq\":5}\n\n"
                        + "event: text\ndata: {\"turn_id\":\"turn-remote\",\"content\":\" world\",\"seq\":6}\n\n",
                    controller: controller
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(conversationID: "web_conv_remote")
        await model?.selectConversation("web_conv_remote")

        // Tokens render live into a new assistant bubble before the turn ends —
        // not via a history reload (the persisted reply doesn't exist yet).
        try await waitUntil { model?.messages.map(\.text) == ["Earlier", "Hello world"] }
        XCTAssertEqual(model?.messages.last?.id, "local_follow_turn-remote")
        XCTAssertEqual(model?.messages.last?.status, .running)

        // The turn ends: history reload reconciles the live bubble to persisted.
        turnFinished.increment()
        controller.finish(
            appending: """
            event: turn_ended
            data: {"turn_id":"turn-remote","status":"complete","seq":7}

            """
        )
        try await waitUntil { model?.messages.last?.id.hasPrefix("msg_") == true }
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "Hello world"])
        XCTAssertEqual(model?.messages.last?.status, .complete)
        // A turn_ended we surfaced live is acked so its disconnect push is
        // suppressed.
        try await waitUntil { acks.value >= 1 }

        // Release so deinit cancels the follow loop before teardown.
        model = nil
    }

    func testResendDuringStreamSupersedesWithoutClobbering() async throws {
        // Sending again while the first turn is still streaming must supersede it
        // cleanly: the first (cancelled) task must not flip the new turn's
        // streaming state off, reload history over it, or leave a stale bubble.
        let streamRequests = AtomicCounter()
        let firstStream = HangingStream()
        let secondStream = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-supersede","conversation_id":"web_conv_supersede","first_seq":0}"#
                )
            case ("POST", "/api/v1/chat/ack"):
                return .json("{}")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_supersede/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_supersede",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"First","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"First reply","timestamp":"2026-06-08T12:00:01Z"},
                        {"internal_id":3,"role":"user","content":"Second","timestamp":"2026-06-08T12:00:02Z"},
                        {"internal_id":4,"role":"assistant","content":"Second reply","timestamp":"2026-06-08T12:00:03Z"}
                      ],
                      "count":4,
                      "total_messages":4,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_supersede/stream" {
                    let isFirst = streamRequests.increment() == 1
                    let controller = isFirst ? firstStream : secondStream
                    let token = isFirst ? "First partial" : "Second partial"
                    return .hangingStream(
                        "event: text\ndata: {\"content\":\"\(token)\"}\n\n",
                        controller: controller
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_supersede")

        // Phase 1: send "First"; its stream hangs after the first token.
        model.draftText = "First"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && model.messages.last?.text == "First partial" }

        // Phase 2: send "Second" while the first turn is still streaming.
        model.draftText = "Second"
        await model.sendDraft()
        try await waitUntil { model.messages.contains { $0.text == "Second partial" } }

        // The superseded first turn must not flip streaming off or reload over the
        // in-flight second turn. Hold the assertion across a window long enough
        // for the cancelled task's tail to (wrongly) run.
        for _ in 0 ..< 10 {
            XCTAssertTrue(model.isStreaming)
            XCTAssertNil(model.errorMessage)
            try await Task.sleep(for: .milliseconds(20))
        }

        // Phase 3: finish the second turn; the thread reloads to a coherent final
        // history with no leftover optimistic placeholders.
        secondStream.finish(appending: "event: turn_ended\ndata: {\"status\":\"complete\",\"seq\":1}\n\n")
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.messages.map(\.text), ["First", "First reply", "Second", "Second reply"])
        XCTAssertFalse(model.messages.contains { $0.text == "First partial" || $0.text == "Second partial" })

        // Release the still-held first stream so its background waiter exits.
        firstStream.finish()
    }

    func testStreamingTextDeltasCoalesceInOrder() async throws {
        // Streamed deltas are buffered and flushed together rather than applied
        // per token, but the assembled bubble text must preserve arrival order.
        let stream = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-coalesce","conversation_id":"web_conv_coalesce","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_coalesce/stream" {
                    return .hangingStream(
                        "event: text\ndata: {\"content\":\"Hello\"}\n\n"
                            + "event: text\ndata: {\"content\":\", \"}\n\n"
                            + "event: text\ndata: {\"content\":\"world\"}\n\n",
                        controller: stream
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_coalesce")
        model.draftText = "Hi"
        await model.sendDraft()

        try await waitUntil { model.messages.last?.text == "Hello, world" }
        XCTAssertTrue(model.isStreaming)

        stream.finish()
    }

    func testBufferedStreamTextIsFlushedSynchronouslyOnCancel() async throws {
        // With a flush interval long enough that the timer never fires during the
        // test, a delta stays buffered (the bubble is still empty) until a turn
        // finalizes. Cancelling must flush it synchronously so the partial reply
        // isn't lost and isn't overwritten by the "Response stopped." placeholder.
        let stream = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-flush","conversation_id":"web_conv_flush","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_flush/stream" {
                    return .hangingStream(
                        "event: text\ndata: {\"content\":\"Partial answer\"}\n\n",
                        controller: stream
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_flush", streamTextFlushInterval: .seconds(60))
        model.draftText = "Hi"
        await model.sendDraft()

        // Wait deterministically for the delta to be received and buffered rather
        // than racing on a fixed delay.
        try await waitUntil { model.bufferedStreamTextForTesting == "Partial answer" }
        // The 60s flush timer has not fired, so the delta is buffered, not applied.
        XCTAssertEqual(model.messages.last?.text, "")

        model.cancelStream()

        XCTAssertFalse(model.isStreaming)
        XCTAssertEqual(model.messages.last?.text, "Partial answer")

        stream.finish()
    }

    func testConversationSwitchDuringStreamCancelsSendAndLoadsTarget() async throws {
        // Switching conversations mid-stream cancels the in-flight send and loads
        // the target thread; the cancelled send must not bleed its optimistic
        // bubbles or an error into the newly opened conversation.
        let hanging = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-switch","conversation_id":"web_conv_switch_a","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_switch_b/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_switch_b",
                      "messages":[
                        {"internal_id":9,"role":"user","content":"Other thread","timestamp":"2026-06-08T12:00:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path.hasSuffix("web_conv_switch_a/stream") {
                    return .hangingStream(
                        "event: text\ndata: {\"content\":\"Working\"}\n\n",
                        controller: hanging
                    )
                }
                if request.httpMethod == "GET", path.hasSuffix("web_conv_switch_b/stream") {
                    return .text("")
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        var model: ChatViewModel? = makeViewModel(conversationID: "web_conv_switch_a")
        model?.draftText = "Hi"
        await model?.sendDraft()
        try await waitUntil { model?.isStreaming == true && model?.messages.last?.text == "Working" }

        // Switch to another conversation while the first is still streaming.
        await model?.selectConversation("web_conv_switch_b")

        XCTAssertEqual(model?.conversationID, "web_conv_switch_b")
        try await waitUntil { model?.messages.map(\.text) == ["Other thread"] }
        XCTAssertEqual(model?.isStreaming, false)
        XCTAssertNil(model?.errorMessage)
        XCTAssertFalse(model?.messages.contains { $0.text == "Working" } ?? true)

        // Release the held stream and the model so the live loop tears down.
        hanging.finish()
        model = nil
    }

    func testFollowLoopSkipsAckForConcurrentTurnWhileStreaming() async throws {
        // While a send is in flight (isStreaming), the live-follow loop must not
        // acknowledge a concurrent turn it doesn't surface (the merge is skipped
        // while streaming) — acking it would let the hub mark that turn delivered
        // and suppress its disconnect push. Counterpart to the send-path guard.
        let acks = AtomicCounter()
        let followConnects = AtomicCounter()
        let followStream = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/ack"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   body["conversation_id"] as? String == "web_conv_followack" {
                    acks.increment()
                }
                return .json("{}")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_followack/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_followack",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_followack/stream" {
                    followConnects.increment()
                    return .hangingStream("", controller: followStream)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        var model: ChatViewModel? = makeViewModel(conversationID: "web_conv_followack")
        await model?.selectConversation("web_conv_followack")
        try await waitUntil { followConnects.value >= 1 }

        // Simulate an in-flight send, then let a concurrent turn end on the follow
        // stream while we're "streaming".
        model?.isStreaming = true
        followStream.finish(
            appending: "event: turn_ended\ndata: {\"turn_id\":\"turn-other\",\"status\":\"complete\",\"seq\":9}\n\n"
        )

        // The ack (if it wrongly fired) is scheduled synchronously when the event
        // is processed, so a short settle window reliably surfaces it.
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(acks.value, 0)

        // Release so deinit cancels the follow loop before teardown.
        model = nil
    }

    func testApplyRouteProcessesEachNewInitialPrompt() async throws {
        var sentPrompts: [String] = []
        var conversationIDs: [String] = []
        var streamedTurnID = "turn-route"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                sentPrompts.append(try XCTUnwrap(payload["prompt"] as? String))
                let conversationID = try XCTUnwrap(payload["conversation_id"] as? String)
                conversationIDs.append(conversationID)
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    """
                    {"turn_id":"\(streamedTurnID)","conversation_id":"\(conversationID)","first_seq":0}
                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    return .text(
                        """
                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","status":"complete"}

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

    func testSendToolCallMidStreamDropSelfHealsViaLiveFollow() async throws {
        // Reproduces the reported flow with the conversation OPEN (live-follow loop
        // running): a send streams its first tool call, then the subscribe
        // connection drops before turn_ended while the turn keeps running durably
        // server-side. Nothing is persisted yet at the drop, so the recovery reload
        // surfaces no reply — the "streaming stopped after the first tool call"
        // symptom. When the turn finishes, the live-follow loop must surface the
        // durable reply on its own, with NO manual reopen.
        let turnComplete = AtomicCounter()
        var streamedTurnID = "turn-tooldrop"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let query = Self.queryItems(from: request)
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_tooldrop","first_seq":0}"#
                )
            case ("POST", "/api/v1/chat/ack"):
                return .json("{}")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_tooldrop/messages"):
                // Nothing persists until the turn completes server-side.
                let rows = turnComplete.value > 0
                    ? #"{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Tool reply","timestamp":"2026-06-08T12:00:01Z"}"#
                    : ""
                return .json(
                    """
                    {"conversation_id":"web_conv_tooldrop","messages":[\(rows)],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_tooldrop/stream" {
                    if query["follow"] == "true" {
                        // Live-follow connection: deliver turn_ended once the turn
                        // completes, otherwise close immediately so the reconnect
                        // loop keeps polling history via handleLiveReconnect.
                        if turnComplete.value > 0 {
                            return .text(
                                "event: turn_ended\ndata: {\"turn_id\":\"\(streamedTurnID)\",\"status\":\"complete\",\"seq\":2}\n\n"
                            )
                        }
                        return .text("")
                    }
                    // Send-and-watch subscribe: stream the first tool call, then drop
                    // before turn_ended.
                    return .droppedStream(
                        """
                        event: turn_started
                        data: {"turn_id":"\(streamedTurnID)","seq":0}

                        event: tool_call
                        data: {"turn_id":"\(streamedTurnID)","seq":1,"tool_call":{"id":"call-1","name":"search_notes","arguments":"{}"}}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_tooldrop",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_tooldrop")
        model?.draftText = "Hi"
        await model?.sendDraft()

        // The subscribe stream dropped after the tool call. The send path recovers
        // by reloading history, which does not yet contain the durable reply.
        try await waitUntil { model?.isStreaming == false }
        XCTAssertFalse(
            model?.messages.contains { $0.text == "Tool reply" } ?? true,
            "The reply is not persisted at the drop, so it cannot have surfaced yet."
        )

        // The turn finishes server-side. The live-follow loop must surface the reply
        // without a manual reopen.
        turnComplete.increment()
        try await waitUntil(timeout: 8) { model?.messages.contains { $0.text == "Tool reply" } ?? false }
        XCTAssertTrue(model?.messages.contains { $0.text == "Tool reply" } ?? false)

        // Release so deinit cancels the fast reconnect loop before teardown.
        model = nil
    }

    func testSendStreamsPastFirstToolCallToCompletion() async throws {
        // Directly probes the reported "streaming stops after the first tool call":
        // a single turn that streams text, a tool call, a tool result, MORE text,
        // and then turn_ended must render through to completion. Every event shares
        // one turn_id (the server keeps the whole user message as one turn), so the
        // client's per-turn filter must not drop the post-tool-call continuation.
        var streamedTurnID = "turn-tools"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_tools","first_seq":0}"#
                )
            case ("POST", "/api/v1/chat/ack"):
                return .json("{}")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_tools/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_tools",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Let me check. Here is what I found.","timestamp":"2026-06-08T12:00:02Z"}
                      ],
                      "count":2,
                      "total_messages":2,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_tools/stream" {
                    return .text(
                        """
                        event: turn_started
                        data: {"turn_id":"\(streamedTurnID)","seq":0}

                        event: text
                        data: {"turn_id":"\(streamedTurnID)","seq":1,"content":"Let me check. "}

                        event: tool_call
                        data: {"turn_id":"\(streamedTurnID)","seq":2,"tool_call":{"id":"call-1","name":"search_notes","arguments":"{}"}}

                        event: tool_result
                        data: {"turn_id":"\(streamedTurnID)","seq":3,"tool_call_id":"call-1","result":"found 3 notes"}

                        event: text
                        data: {"turn_id":"\(streamedTurnID)","seq":4,"content":"Here is what I found."}

                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","seq":5,"status":"complete"}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_tools")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // Streaming must have continued past the tool call: the follow-up text after
        // the tool result is present, the turn completed, and nothing errored.
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertTrue(
            assistant.text.contains("Here is what I found."),
            "Streaming stopped after the tool call: post-tool-call text never rendered. Got: \(assistant.text)"
        )
        XCTAssertEqual(assistant.status, .complete)
        XCTAssertNil(model.errorMessage)
    }

    func testSendToolCallMidStreamDropSelfHealsViaResume() async throws {
        // A mid-stream tool-call drop with NO live-follow loop active (sendDraft
        // never starts one; only selectConversation / startNewConversation do).
        // The send-and-watch path must resume the stream itself and surface the
        // completed reply WITHOUT the user reopening the thread — the fix for the
        // reported "I only see the full conversation if I reopen the app".
        let streamRequests = AtomicCounter()
        var streamedTurnID = "turn-selfheal"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_selfheal","first_seq":0}"#
                )
            case ("POST", "/api/v1/chat/ack"):
                return .json("{}")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_selfheal/messages"):
                return .json(
                    """
                    {"conversation_id":"web_conv_selfheal","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Tool reply","timestamp":"2026-06-08T12:00:01Z"}],"count":2,"total_messages":2,"has_more_before":false,"has_more_after":false}
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_selfheal/stream" {
                    if streamRequests.increment() == 1 {
                        // First leg: the tool call, then the socket dies before
                        // turn_ended.
                        return .droppedStream(
                            """
                            event: turn_started
                            data: {"turn_id":"\(streamedTurnID)","seq":0}

                            event: tool_call
                            data: {"turn_id":"\(streamedTurnID)","seq":1,"tool_call":{"id":"call-1","type":"function","function":{"name":"search_notes","arguments":"{}"}}}

                            """
                        )
                    }
                    // Resume leg: the rest of the turn through turn_ended.
                    return .text(
                        """
                        event: tool_result
                        data: {"turn_id":"\(streamedTurnID)","tool_call_id":"call-1","result":"ok","seq":2}

                        event: text
                        data: {"turn_id":"\(streamedTurnID)","content":"Tool reply","seq":3}

                        event: turn_ended
                        data: {"turn_id":"\(streamedTurnID)","status":"complete","seq":4}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_selfheal",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // The drop self-healed via the send-path resume — a second subscribe
        // completed the turn and surfaced the reply with no reopen required.
        XCTAssertEqual(streamRequests.value, 2)
        XCTAssertTrue(model.messages.contains { $0.text == "Tool reply" })
        XCTAssertNil(model.errorMessage)
    }

    func testStreamDropEmitsDiagnosticBreadcrumb() async throws {
        // A mid-turn drop must emit a structured breadcrumb to the error log so a
        // real drop can be diagnosed from /api/diagnostics without a tethered
        // device. The decisive field is `url_error_code` — it distinguishes an
        // idle timeout (proxy/buffering) from a connection loss (background /
        // suspend) from an app-side cancel. With no base URL configured the
        // reporter persists the payload to its spool, which we decode directly —
        // deterministic and free of network timing.
        var streamedTurnID = "turn-breadcrumb"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_breadcrumb","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_breadcrumb/messages"):
                return .json(
                    """
                    {"conversation_id":"web_conv_breadcrumb","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}
                    """
                )
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    return .droppedStream(
                        """
                        event: turn_started
                        data: {"turn_id":"\(streamedTurnID)","seq":0}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-breadcrumb-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        // No baseURLProvider configured → deliver() persists to the spool.
        let reporter = ErrorReporter(spoolDirectory: spoolDirectory, dedupeWindow: 0)
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        let model = ChatViewModel(
            authManager: authManager,
            conversationID: "web_conv_breadcrumb",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 1,
            streamResumeLivenessSeconds: 60,
            errorReporter: reporter
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        let payload = try await waitForSpooledReport(in: spoolDirectory)
        let extra = try XCTUnwrap(payload.extraData)
        XCTAssertEqual(payload.componentName, "Chat.streamDrop")
        XCTAssertEqual(extra["phase"], "send-subscribe")
        XCTAssertEqual(extra["outcome"], "interrupted")
        // The decisive diagnostic field: a connection-loss drop, not a timeout or
        // an app-side cancel.
        XCTAssertEqual(extra["url_error_code"], "networkConnectionLost")
    }

    /// Poll a reporter spool directory until a report lands, then decode it.
    private func waitForSpooledReport(
        in directory: URL,
        timeout: TimeInterval = 4
    ) async throws -> ErrorReportPayload {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let files = (try? FileManager.default.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: nil
            )) ?? []
            if let file = files.first(where: { $0.pathExtension == "json" }),
               let data = try? Data(contentsOf: file),
               let payload = try? JSONDecoder().decode(ErrorReportPayload.self, from: data) {
                return payload
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        let listing = (try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil
        ))?.map(\.lastPathComponent) ?? ["<dir missing>"]
        XCTFail("Timed out waiting for a spooled error report. Dir contents: \(listing)")
        throw ChatAPIError.validation("no spooled report")
    }

    private func makeViewModel(
        conversationID: String?,
        liveReconnectInitialDelaySeconds: Double = 2,
        liveReconnectMaxDelaySeconds: Double = 30,
        maxConsecutiveStreamResumes: Int = 5,
        streamResumeLivenessSeconds: Double = 2,
        streamTextFlushInterval: Duration = .milliseconds(50)
    ) -> ChatViewModel {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return ChatViewModel(
            authManager: authManager,
            conversationID: conversationID,
            initialPrompt: nil,
            liveReconnectInitialDelaySeconds: liveReconnectInitialDelaySeconds,
            liveReconnectMaxDelaySeconds: liveReconnectMaxDelaySeconds,
            maxConsecutiveStreamResumes: maxConsecutiveStreamResumes,
            streamResumeLivenessSeconds: streamResumeLivenessSeconds,
            streamTextFlushInterval: streamTextFlushInterval
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
        UserDefaults.standard.removeObject(forKey: "lastConversationActiveAt")
        UserDefaults.standard.removeObject(forKey: "selectedProfileId")
    }

    private func storeLastConversation(_ id: String, activeSecondsAgo: TimeInterval) {
        UserDefaults.standard.set(id, forKey: "lastConversationId")
        UserDefaults.standard.set(
            Date().addingTimeInterval(-activeSecondsAgo).timeIntervalSinceReferenceDate,
            forKey: "lastConversationActiveAt"
        )
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
