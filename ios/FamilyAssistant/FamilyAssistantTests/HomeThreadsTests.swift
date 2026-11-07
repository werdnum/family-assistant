import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class HomeThreadsTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private var models: [ChatViewModel] = []

    override func setUp() {
        super.setUp()
        resetStoredState()
        KeychainHelper.save(key: "fa_api_token", string: "home-threads-token")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
        URLProtocol.registerClass(ChatMockBackendURLProtocol.self)
        ChatMockBackendURLProtocol.reset()
    }

    override func tearDown() {
        for model in models {
            model.scenePhaseChanged(old: .active, new: .background)
            model.cancelStream()
        }
        models.removeAll()
        ChatMockBackendURLProtocol.reset()
        URLProtocol.unregisterClass(ChatMockBackendURLProtocol.self)
        resetStoredState()
        super.tearDown()
    }

    // MARK: - Decoding

    func testThreadListDecodesEveryStateAndToleratesNewOnes() throws {
        let json = """
        {
          "home_conversation_id": "web_conv_home",
          "threads": [
            \(threadJSON(id: "t1", state: "working")),
            \(threadJSON(id: "t2", state: "waiting", sourceToolCallID: "call-2")),
            \(threadJSON(id: "t3", state: "failed")),
            \(threadJSON(id: "t4", state: "archived_someday"))
          ]
        }
        """
        let response = try JSONDecoder.chatDecoder.decode(ChatHomeThreadsResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.homeConversationID, "web_conv_home")
        XCTAssertEqual(response.threads.map(\.state), [.working, .waiting, .failed, .unknown("archived_someday")])
        XCTAssertEqual(response.threads[1].sourceToolCallID, "call-2")
        XCTAssertNil(response.threads[0].sourceToolCallID)
    }

    func testMessagesResponseWithoutThreadFieldsStillDecodes() throws {
        let json = """
        {"conversation_id":"web_conv_a","messages":[],"count":0,"total_messages":0}
        """
        let response = try JSONDecoder.chatDecoder.decode(ChatConversationMessagesResponse.self, from: Data(json.utf8))

        XCTAssertNil(response.latestUserProfileID)
        XCTAssertNil(response.conversationKind)
        XCTAssertNil(response.thread)
    }

    func testMessagesResponseDecodesThreadMetadata() throws {
        let json = """
        {
          "conversation_id":"t1","messages":[],"count":0,"total_messages":0,
          "latest_user_profile_id":"browser_profile",
          "conversation_kind":"thread",
          "thread": \(threadJSON(id: "t1", state: "waiting", profileID: "browser_profile"))
        }
        """
        let response = try JSONDecoder.chatDecoder.decode(ChatConversationMessagesResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.latestUserProfileID, "browser_profile")
        XCTAssertEqual(response.conversationKind, .thread)
        XCTAssertEqual(response.thread?.profileID, "browser_profile")
    }

    func testConversationSummaryKindIsOptional() throws {
        let json = """
        {"conversations":[
          {"conversation_id":"a","last_message":"x","last_timestamp":"2026-06-08T12:00:00Z","message_count":1},
          {"conversation_id":"b","last_message":"y","last_timestamp":"2026-06-08T12:00:00Z","message_count":1,"kind":"thread"}
        ],"count":2}
        """
        let response = try JSONDecoder.chatDecoder.decode(ChatConversationListResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.conversations.map(\.kind), [nil, "thread"])
    }

    // MARK: - Switcher grouping

    func testSwitcherGroupsThreadsByStateInServerOrder() {
        let threads = [
            thread("w1", .working),
            thread("q1", .waiting),
            thread("f1", .failed),
            thread("q2", .waiting),
            thread("u1", .unknown("later")),
        ]

        let sections = ThreadSwitcherSections(threads: threads, conversations: [], homeConversationID: "home")

        XCTAssertEqual(sections.groups.map(\.title), ["Working", "Waiting on you", "Failed"])
        XCTAssertEqual(sections.groups[0].threads.map(\.conversationID), ["w1"])
        XCTAssertEqual(sections.groups[1].threads.map(\.conversationID), ["q1", "q2", "u1"])
        XCTAssertEqual(sections.groups[2].threads.map(\.conversationID), ["f1"])
        XCTAssertEqual(ThreadSwitcherSections.waitingCount(in: threads), 3)
    }

    func testUnknownStateIsWaitingOnCardSectionAndBadge() {
        let unknown = thread("u1", .unknown("needs_review"))

        XCTAssertEqual(unknown.presentationState, .waiting)
        XCTAssertEqual(ThreadPresentationState.label(for: unknown.presentationState), "Waiting on you")
        let sections = ThreadSwitcherSections(threads: [unknown], conversations: [], homeConversationID: "home")
        XCTAssertEqual(sections.groups.map(\.title), ["Waiting on you"])
        XCTAssertEqual(ThreadSwitcherSections.waitingCount(in: [unknown]), 1)
        XCTAssertEqual(
            ThreadPresentationState.label(for: nil),
            "Starting",
            "Only a card whose thread has not loaded yet reads as starting."
        )
    }

    func testSwitcherOmitsEmptyGroups() {
        let sections = ThreadSwitcherSections(
            threads: [thread("w1", .working)],
            conversations: [],
            homeConversationID: "home"
        )

        XCTAssertEqual(sections.groups.map(\.title), ["Working"])
    }

    func testEarlierListsOnlyOrdinaryConversations() {
        let conversations = [
            conversation("home", kind: "home"),
            conversation("t1", kind: nil),
            conversation("t2", kind: "thread"),
            conversation("old", kind: nil),
            conversation("new", kind: "conversation"),
        ]

        let sections = ThreadSwitcherSections(
            threads: [thread("t1", .waiting)],
            conversations: conversations,
            homeConversationID: "home"
        )

        XCTAssertEqual(sections.earlier.map(\.conversationID), ["old", "new"])
    }

    // MARK: - Thread cards

    func testStartThreadCallBecomesACardAndLeavesTheGroup() {
        let calls = [
            toolCall("call-search", name: "search_notes", status: .succeeded),
            toolCall("call-start", name: "start_thread", status: .succeeded),
        ]

        let split = ThreadCardSplit(toolCalls: calls, showsCards: true)

        XCTAssertEqual(split.threadCalls.map(\.id), ["call-start"])
        XCTAssertEqual(split.otherCalls.map(\.id), ["call-search"])
    }

    func testCardsAreOnlyDrawnInHome() {
        let calls = [toolCall("call-start", name: "start_thread", status: .succeeded)]

        let split = ThreadCardSplit(toolCalls: calls, showsCards: false)

        XCTAssertTrue(split.threadCalls.isEmpty)
        XCTAssertEqual(split.otherCalls.map(\.id), ["call-start"])
    }

    func testRefusedStartThreadStaysAToolRow() {
        let calls = [
            toolCall("call-failed", name: "start_thread", status: .failed),
            toolCall("call-refused", name: "start_thread", status: .succeeded, result: "Error: Threads are not enabled."),
            toolCall("call-running", name: "start_thread", status: .running),
        ]

        let split = ThreadCardSplit(toolCalls: calls, showsCards: true)

        XCTAssertEqual(split.threadCalls.map(\.id), ["call-running"])
        XCTAssertEqual(split.otherCalls.map(\.id), ["call-failed", "call-refused"])
    }

    func testKnownThreadIsACardWhateverTheCallRecorded() {
        let calls = [toolCall("call-start", name: "start_thread", status: .unknown)]

        let split = ThreadCardSplit(toolCalls: calls, showsCards: true, hasThread: { $0 == "call-start" })

        XCTAssertEqual(split.threadCalls.map(\.id), ["call-start"])
    }

    func testCardTitleFallsBackToTheRequestedTitle() {
        XCTAssertEqual(
            ChatThreadTool.requestedTitle(from: #"{"title":"  Hot water quotes ","request":"x"}"#),
            "Hot water quotes"
        )
        XCTAssertNil(ChatThreadTool.requestedTitle(from: #"{"request":"x"}"#))
        XCTAssertNil(ChatThreadTool.requestedTitle(from: "not json"))
    }

    func testPreviewLineFallsBackToTheSeedRequest() {
        XCTAssertEqual(thread("a", .working, preview: "Latest", seed: "Seed").previewLine, "Latest")
        XCTAssertEqual(thread("a", .working, preview: "  ", seed: "Seed").previewLine, "Seed")
        XCTAssertNil(thread("a", .working, preview: nil, seed: nil).previewLine)
    }

    func testStoreMatchesCardsBySourceToolCallID() async {
        ChatMockBackendURLProtocol.respond { request in
            guard request.url?.path == "/api/v1/chat/home/threads" else {
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            return .json("""
            {"home_conversation_id":"home","threads":[
              \(self.threadJSON(id: "t1", state: "waiting", sourceToolCallID: "call-1")),
              \(self.threadJSON(id: "t2", state: "working"))
            ]}
            """)
        }
        let store = HomeThreadsStore(homeConversationID: "home", apiClient: ChatAPIClient(authManager: makeAuthManager()))

        await store.refreshThreads()

        XCTAssertEqual(store.thread(forSourceToolCallID: "call-1")?.conversationID, "t1")
        XCTAssertNil(store.thread(forSourceToolCallID: "call-2"))
        XCTAssertEqual(store.thread(conversationID: "t2")?.state, .working)
        XCTAssertEqual(store.waitingCount, 1)
        XCTAssertFalse(store.threadsRefreshFailed)
    }

    func testStoreKeepsItsThreadsWhenARefreshFails() async {
        let failures = LockedStrings()
        ChatMockBackendURLProtocol.respond { _ in
            if !failures.values.isEmpty {
                return .json(#"{"detail":"boom"}"#, statusCode: 500)
            }
            return .json("""
            {"home_conversation_id":"home","threads":[\(self.threadJSON(id: "t1", state: "waiting"))]}
            """)
        }
        let store = HomeThreadsStore(
            homeConversationID: "home",
            apiClient: ChatAPIClient(authManager: makeAuthManager()),
            errorReporter: ErrorReporter(spoolDirectory: nil)
        )
        await store.refreshThreads()

        failures.append("fail")
        await store.refreshThreads()

        XCTAssertEqual(store.threads.map(\.conversationID), ["t1"])
        XCTAssertTrue(store.threadsRefreshFailed)
    }

    // MARK: - Thread state, unread and done

    func testSummaryDecodesCloseAndUnreadAndDefaultsWithoutThem() throws {
        let closed = try JSONDecoder.chatDecoder.decode(ChatThreadSummary.self, from: Data(threadJSON(
            id: "t1",
            state: "done",
            extra: #","closed_by":"assistant","closed_at":"2026-06-08T12:06:00Z","echo_message_id":7,"unread_count":3"#
        ).utf8))
        let plain = try JSONDecoder.chatDecoder.decode(
            ChatThreadSummary.self,
            from: Data(threadJSON(id: "t2", state: "waiting").utf8)
        )

        XCTAssertEqual(closed.closedBy, .assistant)
        XCTAssertNotNil(closed.closedAt)
        XCTAssertEqual(closed.echoMessageID, 7)
        XCTAssertEqual(closed.unreadCount, 3)
        XCTAssertNil(plain.closedBy)
        XCTAssertNil(plain.echoMessageID)
        XCTAssertEqual(plain.unreadCount, 0)
    }

    /// Every state and closer: the label, the line under the title, whether
    /// "Mark done" is offered, and the switcher group.
    func testCardAndSwitcherShowEveryStateAndCloser() {
        let states: [(ChatThreadState, String, String)] = [
            (.working, "Working", ThreadSwitcherSections.workingTitle),
            (.waiting, "Waiting on you", ThreadSwitcherSections.waitingTitle),
            (.failed, "Failed", ThreadSwitcherSections.failedTitle),
            (.done, "Done", ThreadSwitcherSections.doneTitle),
        ]
        for (state, label, group) in states {
            for closer in [nil, ChatThreadCloser.assistant, .user] {
                let summary = thread("t", state, preview: "Booked for Saturday", closedBy: closer)
                let context = "\(state) closed by \(String(describing: closer))"

                XCTAssertEqual(ThreadPresentationState.label(for: summary.presentationState), label, context)
                let userClosedDone = state == .done && closer == .user
                XCTAssertEqual(
                    summary.detailLine,
                    userClosedDone ? "You marked this done" : "Booked for Saturday",
                    context
                )
                XCTAssertEqual(summary.canMarkDone, state == .waiting || state == .failed, context)
                let sections = ThreadSwitcherSections(threads: [summary], conversations: [], homeConversationID: "home")
                XCTAssertEqual(sections.groups.map(\.title), [group], context)
            }
        }
    }

    func testSwitcherOrdersWhatIsMovingFirstAndWhatHasSettledLast() {
        let sections = ThreadSwitcherSections(
            threads: [thread("d", .done), thread("f", .failed), thread("w", .working), thread("q", .waiting)],
            conversations: [conversation("old", kind: "conversation")],
            homeConversationID: "home"
        )

        XCTAssertEqual(sections.groups.map(\.title), ["Working", "Waiting on you", "Failed", "Done"])
        XCTAssertEqual(sections.earlier.map(\.conversationID), ["old"])
    }

    func testAQuietThreadIsListedApartAndNotCountedAsWaiting() {
        var quiet = thread("old", .waiting)
        quiet.isQuiet = true
        let threads = [quiet, thread("new", .waiting), thread("d", .done)]

        let sections = ThreadSwitcherSections(threads: threads, conversations: [], homeConversationID: "home")

        XCTAssertEqual(sections.groups.map(\.title), ["Waiting on you", "Quiet", "Done"])
        XCTAssertEqual(sections.groups[1].threads.map(\.conversationID), ["old"])
        XCTAssertEqual(ThreadSwitcherSections.waitingCount(in: threads), 1)
        XCTAssertTrue(quiet.canMarkDone, "A quiet thread can still be closed.")
    }

    func testOnlyAWaitingThreadReadsAsQuiet() {
        var working = thread("w", .working)
        working.isQuiet = true

        XCTAssertEqual(working.presentationState, .working)
    }

    func testThreadDecodesQuietAndDefaultsToNotQuiet() throws {
        let json = #"{"conversation_id":"t1","home_conversation_id":"home","title":"T","profile_id":"p","state":"waiting","created_at":"2026-06-08T12:00:00Z","updated_at":"2026-06-08T12:00:00Z"QUIET}"#
        func decoded(_ quiet: String) throws -> ChatThreadSummary {
            let body = json.replacingOccurrences(of: "QUIET", with: quiet)
            return try JSONDecoder.chatDecoder.decode(ChatThreadSummary.self, from: Data(body.utf8))
        }

        XCTAssertTrue(try decoded(#","quiet":true"#).isQuiet)
        XCTAssertFalse(try decoded("").isQuiet)
    }

    func testUnreadCountIsInTheCardsSpokenSummaryOnlyWhenNonZero() {
        XCTAssertEqual(
            thread("Quotes", .waiting, preview: "Gas or electric?", unread: 2).accessibilitySummary,
            "Quotes, Waiting on you, Gas or electric?, 2 unread"
        )
        XCTAssertEqual(
            thread("Quotes", .done, preview: "Asked", closedBy: .user).accessibilitySummary,
            "Quotes, Done, You marked this done"
        )
    }

    func testStoreAppliesAReadThreadsUnreadCount() async {
        ChatMockBackendURLProtocol.respond { _ in
            .json("""
            {"home_conversation_id":"home","threads":[
              \(self.threadJSON(id: "t1", state: "waiting", extra: #","unread_count":4"#)),
              \(self.threadJSON(id: "t2", state: "working", extra: #","unread_count":1"#))
            ]}
            """)
        }
        let store = makeStore()
        await store.refreshThreads()

        store.applyUnreadCount(0, for: "t1")

        XCTAssertEqual(store.thread(conversationID: "t1")?.unreadCount, 0)
        XCTAssertEqual(store.thread(conversationID: "t2")?.unreadCount, 1)
    }

    func testMarkDoneShowsTheThreadClosedByTheUser() async {
        let doneRequests = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/home/threads/t1/done" {
                doneRequests.append(path)
                return .json(self.threadJSON(
                    id: "t1",
                    state: "done",
                    extra: #","closed_by":"user","closed_at":"2026-06-08T12:06:00Z""#
                ))
            }
            let state = doneRequests.values.isEmpty ? "waiting" : "done"
            return .json("""
            {"home_conversation_id":"home","threads":[
              \(self.threadJSON(id: "t1", state: state, extra: doneRequests.values.isEmpty ? "" : #","closed_by":"user""#))
            ]}
            """)
        }
        let store = makeStore()
        await store.refreshThreads()
        XCTAssertEqual(store.thread(conversationID: "t1")?.presentationState, .waiting)

        await store.markDone("t1")

        let closed = store.thread(conversationID: "t1")
        XCTAssertEqual(doneRequests.values.count, 1)
        XCTAssertEqual(closed?.presentationState, .done)
        XCTAssertEqual(closed?.closedBy, .user)
        XCTAssertEqual(closed?.detailLine, "You marked this done")
        XCTAssertNil(store.actionFailure)
        XCTAssertTrue(store.markingDone.isEmpty)
    }

    func testMarkDoneFailureIsReportedAndLeavesTheThreadAsItWas() async {
        ChatMockBackendURLProtocol.respond { request in
            if request.httpMethod == "POST" {
                return .json(#"{"detail":"boom"}"#, statusCode: 500)
            }
            return .json("""
            {"home_conversation_id":"home","threads":[\(self.threadJSON(id: "t1", state: "waiting"))]}
            """)
        }
        let store = makeStore()
        await store.refreshThreads()

        await store.markDone("t1")

        XCTAssertNotNil(store.actionFailure, "A failed Mark done is reported, never dropped.")
        XCTAssertEqual(store.thread(conversationID: "t1")?.presentationState, .waiting)
        XCTAssertTrue(store.markingDone.isEmpty)
    }

    // MARK: - Echo rows

    func testHomeRowEchoingAThreadCarriesTheThread() async {
        respondWithConversation(
            id: "home",
            messagesJSON: """
            [{"internal_id":1,"role":"assistant","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
             {"internal_id":2,"role":"assistant","content":"Booked for Saturday.","timestamp":"2026-06-08T12:01:00Z",
              "echoed_from_thread":"t1"}]
            """,
            extraJSON: #","conversation_kind":"home""#
        )
        let model = makeViewModel(conversationID: "home", role: .home)

        await model.selectConversation("home")

        XCTAssertEqual(model.messages.map(\.echoedFromThread), [nil, "t1"])
        XCTAssertEqual(model.groupedMessages.last?.echoedFromThread, "t1")
    }

    // MARK: - Read marker

    func testReadMarkerMovesOnlyWhileTheNewestMessageIsInView() {
        let reading = ReadMarkerGate.Inputs(
            kind: .thread,
            isOnScreen: true,
            isSceneActive: true,
            isAtLatest: true,
            latestPersistedMessageID: 9,
            markedThrough: nil
        )
        XCTAssertEqual(ReadMarkerGate.messageToMark(reading), 9)

        var home = reading
        home.kind = .home
        XCTAssertEqual(ReadMarkerGate.messageToMark(home), 9)

        for (change, name) in [
            ({ (inputs: inout ReadMarkerGate.Inputs) in inputs.kind = .conversation }, "ordinary conversation"),
            ({ (inputs: inout ReadMarkerGate.Inputs) in inputs.kind = nil }, "unknown kind"),
            ({ (inputs: inout ReadMarkerGate.Inputs) in inputs.isOnScreen = false }, "off screen"),
            ({ (inputs: inout ReadMarkerGate.Inputs) in inputs.isSceneActive = false }, "backgrounded"),
            ({ (inputs: inout ReadMarkerGate.Inputs) in inputs.isAtLatest = false }, "scrolled up"),
            ({ (inputs: inout ReadMarkerGate.Inputs) in inputs.latestPersistedMessageID = nil }, "nothing persisted"),
            ({ (inputs: inout ReadMarkerGate.Inputs) in inputs.markedThrough = 9 }, "already marked"),
            ({ (inputs: inout ReadMarkerGate.Inputs) in inputs.markedThrough = 12 }, "marked further"),
        ] {
            var inputs = reading
            change(&inputs)
            XCTAssertNil(ReadMarkerGate.messageToMark(inputs), name)
        }
        XCTAssertEqual(ReadMarkerGate.persistedID(of: "msg_42"), 42)
        XCTAssertNil(ReadMarkerGate.persistedID(of: "local_assistant_1"))
    }

    func testReadingHomeMarksItReadThroughTheNewestMessageOnce() async throws {
        let reads = respondWithReadableConversation(id: "home", kind: "home")
        let model = makeViewModel(conversationID: "home", role: .home)
        model.markReadDelay = .milliseconds(20)
        var reported: [(String, Int)] = []
        model.onMarkedRead = { reported.append(($0, $1)) }
        await model.selectConversation("home")

        model.setOnScreen(true)
        model.setScrolledToLatest(true)
        await model.settleReadMarker()

        XCTAssertEqual(reads.values, [#"{"through_message_id":3}"#])
        XCTAssertEqual(reported.map(\.0), ["home"])
        XCTAssertEqual(reported.map(\.1), [0])

        // A streamed token changes no persisted message: nothing more to mark.
        model.messages[model.messages.count - 1].text += " more"
        await model.settleReadMarker()
        XCTAssertEqual(reads.values.count, 1, "Marking read never follows streamed tokens.")

        model.messages.append(persistedMessage(id: 7))
        await model.settleReadMarker()
        XCTAssertEqual(reads.values.last, #"{"through_message_id":7}"#)
    }

    func testThreadIsNotMarkedReadScrolledUpOffScreenOrBackgrounded() async throws {
        let reads = respondWithReadableConversation(id: "t1", kind: "thread")
        let model = makeViewModel(conversationID: "t1", role: .pushed)
        model.markReadDelay = .milliseconds(20)
        await model.selectConversation("t1")

        model.setOnScreen(true)
        model.setScrolledToLatest(false)
        XCTAssertFalse(model.hasPendingReadCheck, "Scrolled up: the newest message is not in view.")

        model.setOnScreen(false)
        model.setScrolledToLatest(true)
        XCTAssertFalse(model.hasPendingReadCheck, "Covered by another view.")

        model.setOnScreen(true)
        model.scenePhaseChanged(old: .active, new: .background)
        XCTAssertFalse(model.hasPendingReadCheck, "Backgrounded: a check already scheduled is dropped.")
        XCTAssertTrue(reads.values.isEmpty)

        model.scenePhaseChanged(old: .background, new: .active)
        await model.settleReadMarker()
        XCTAssertEqual(reads.values.count, 1)
    }

    func testHomeIsNotMarkedReadUnderTheChatSwitcher() async throws {
        let reads = respondWithReadableConversation(id: "home", kind: "home")
        let model = makeViewModel(conversationID: "home", role: .home)
        model.markReadDelay = .milliseconds(20)
        await model.selectConversation("home")

        model.setCoveredBySheet(true)
        model.setOnScreen(true)
        model.setScrolledToLatest(true)
        XCTAssertFalse(model.hasPendingReadCheck, "Home behind the switcher sheet is not being read.")

        model.setCoveredBySheet(false)
        await model.settleReadMarker()
        XCTAssertEqual(reads.values.count, 1)
    }

    func testReturningToTheNewestPageMarksWhatItShows() async throws {
        let reads = respondWithReadableConversation(id: "home", kind: "home")
        let model = makeViewModel(conversationID: "home", role: .home)
        model.markReadDelay = .milliseconds(20)
        await model.selectConversation("home")
        model.setOnScreen(true)
        model.messages.append(contentsOf: (10..<50).map(persistedMessage(id:)))
        model.displayedMessageNewerOffset = ChatViewModel.displayedMessageWindowCount
        XCTAssertTrue(model.hasNewerMessages)

        model.setScrolledToLatest(true)
        await model.settleReadMarker()
        XCTAssertTrue(reads.values.isEmpty, "The bottom of an older page is not the newest message.")

        model.showNewerMessages()
        await model.settleReadMarker()
        XCTAssertEqual(reads.values, [#"{"through_message_id":49}"#])
    }

    func testOrdinaryConversationIsNeverMarkedRead() async throws {
        let reads = respondWithReadableConversation(id: "c1", kind: "conversation")
        let model = makeViewModel(conversationID: "c1", role: .pushed)
        model.markReadDelay = .milliseconds(20)
        await model.selectConversation("c1")

        model.setOnScreen(true)
        model.setScrolledToLatest(true)

        XCTAssertFalse(model.hasPendingReadCheck)
        XCTAssertTrue(reads.values.isEmpty)
    }

    func testFailedReadIsNotRetriedUntilSomethingNewArrives() async throws {
        let reads = respondWithReadableConversation(id: "t1", kind: "thread", readStatus: 500)
        let model = makeViewModel(conversationID: "t1", role: .pushed)
        model.markReadDelay = .milliseconds(20)
        await model.selectConversation("t1")

        model.setOnScreen(true)
        model.setScrolledToLatest(true)
        await model.settleReadMarker()
        XCTAssertEqual(reads.values.count, 1)

        model.messages[model.messages.count - 1].text += " more"
        await model.settleReadMarker()
        XCTAssertEqual(reads.values.count, 1, "The same position is not retried on every change.")

        model.messages.append(persistedMessage(id: 8))
        await model.settleReadMarker()
        XCTAssertEqual(reads.values.count, 2)
    }

    func testMessageArrivingWhileAReadFailsIsMarkedAfterIt() async throws {
        let pending = HangingStream()
        let reads = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/conversations/t1/read" {
                reads.append(String(decoding: Self.body(of: request), as: UTF8.self))
                if reads.values.count == 1 {
                    // Not a read response: the first request fails once released.
                    return .hangingStream(#"{"detail":"boom"}"#, controller: pending)
                }
                return .json(#"{"conversation_id":"t1","unread_count":0}"#)
            }
            return .json("""
            {"conversation_id":"t1","messages":[
              {"internal_id":3,"role":"assistant","content":"Anything else?","timestamp":"2026-06-08T12:00:02Z"}
            ],"count":1,"total_messages":1,"latest_user_profile_id":"research",
             "conversation_kind":"thread","thread":\(self.threadJSON(id: "t1", state: "waiting"))}
            """)
        }
        let model = makeViewModel(conversationID: "t1", role: .pushed)
        model.markReadDelay = .milliseconds(20)
        await model.selectConversation("t1")
        model.setOnScreen(true)
        model.setScrolledToLatest(true)
        try await waitUntil { reads.values.count == 1 }

        model.messages.append(persistedMessage(id: 9))
        pending.finish()
        await model.settleReadMarker()

        XCTAssertEqual(reads.values, [#"{"through_message_id":3}"#, #"{"through_message_id":9}"#])
    }

    /// A conversation of `kind` holding messages 1–3, with a read endpoint that
    /// records each request body.
    private func respondWithReadableConversation(
        id: String,
        kind: String,
        readStatus: Int = 200
    ) -> LockedStrings {
        let reads = LockedStrings()
        let threadField = kind == "thread" ? #","thread":\#(threadJSON(id: id, state: "waiting"))"# : ""
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/conversations/\(id)/read" {
                reads.append(String(decoding: Self.body(of: request), as: UTF8.self))
                if readStatus != 200 {
                    return .json(#"{"detail":"boom"}"#, statusCode: readStatus)
                }
                return .json(#"{"conversation_id":"\#(id)","unread_count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json("""
                {"conversation_id":"\(id)","messages":[
                  {"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                  {"internal_id":2,"role":"assistant","content":"Hello","timestamp":"2026-06-08T12:00:01Z"},
                  {"internal_id":3,"role":"assistant","content":"Anything else?","timestamp":"2026-06-08T12:00:02Z"}
                ],"count":3,"total_messages":3,"latest_user_profile_id":"research",
                 "conversation_kind":"\(kind)"\(threadField)}
                """)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }
        return reads
    }

    private func persistedMessage(id: Int) -> ChatMessage {
        ChatMessage(
            id: "msg_\(id)",
            role: .assistant,
            text: "Later",
            createdAt: Date(),
            toolCalls: [],
            attachments: [],
            isLoading: false,
            status: .complete,
            processingProfileID: nil,
            errorTraceback: nil
        )
    }

    private func makeStore(errorReporter: ErrorReporter = ErrorReporter(spoolDirectory: nil)) -> HomeThreadsStore {
        HomeThreadsStore(
            homeConversationID: "home",
            apiClient: ChatAPIClient(authManager: makeAuthManager()),
            errorReporter: errorReporter
        )
    }

    // MARK: - Routes

    func testRoutesIntoHome() {
        let home = "web_conv_home"

        XCTAssertEqual(HomeRouteAction.resolve(ChatRoute(), homeConversationID: home), .showHome)
        XCTAssertEqual(HomeRouteAction.resolve(ChatRoute(conversationID: home), homeConversationID: home), .showHome)
        XCTAssertEqual(
            HomeRouteAction.resolve(ChatRoute(conversationID: "t1"), homeConversationID: home),
            .open(conversationID: "t1")
        )
        XCTAssertEqual(
            HomeRouteAction.resolve(ChatRoute(initialPrompt: "Buy milk"), homeConversationID: home),
            .sendInHome(prompt: "Buy milk")
        )
        XCTAssertEqual(
            HomeRouteAction.resolve(
                ChatRoute(initialPrompt: "Plan", newConversationRequestID: "r1"),
                homeConversationID: home
            ),
            .sendInHome(prompt: "Plan"),
            "A new chat that comes with a prompt is Home's next message."
        )
        XCTAssertEqual(
            HomeRouteAction.resolve(ChatRoute(newConversationRequestID: "r1"), homeConversationID: home),
            .composeInHome,
            "A new chat without one is Home's composer, never a conversation beside it."
        )
        XCTAssertEqual(
            HomeRouteAction.resolve(
                ChatRoute(newConversationRequestID: "b1", sharedAttachmentBatchID: "b1"),
                homeConversationID: home
            ),
            .showHome
        )
    }

    // MARK: - Home availability

    func testHomeAvailabilityIsRememberedPerServer() throws {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "HomeThreadsTests.availability"))
        defaults.removePersistentDomain(forName: "HomeThreadsTests.availability")

        XCTAssertEqual(HomeAvailability.cached(serverURL: serverURL, defaults: defaults), .unknown)

        HomeAvailability.store(.enabled(homeConversationID: "home"), serverURL: serverURL, defaults: defaults)
        XCTAssertEqual(
            HomeAvailability.cached(serverURL: serverURL, defaults: defaults),
            .enabled(homeConversationID: "home")
        )
        XCTAssertEqual(HomeAvailability.cached(serverURL: "https://other.example.test", defaults: defaults), .unknown)

        HomeAvailability.store(.disabled, serverURL: serverURL, defaults: defaults)
        XCTAssertEqual(HomeAvailability.cached(serverURL: serverURL, defaults: defaults), .disabled)

        HomeAvailability.clear(defaults: defaults)
        XCTAssertEqual(HomeAvailability.cached(serverURL: serverURL, defaults: defaults), .unknown)
    }

    func testFreshAnswerReplacesAGoneOrMovedHomeButNotTheConversationList() {
        let home = HomeAvailability.State.enabled(homeConversationID: "home")
        let moved = HomeAvailability.State.enabled(homeConversationID: "moved")

        XCTAssertEqual(HomeAvailability.resolved(current: .unknown, fetched: home), home)
        XCTAssertEqual(HomeAvailability.resolved(current: .unknown, fetched: .disabled), .disabled)
        XCTAssertEqual(HomeAvailability.resolved(current: home, fetched: .disabled), .disabled)
        XCTAssertEqual(HomeAvailability.resolved(current: home, fetched: moved), moved)
        XCTAssertEqual(HomeAvailability.resolved(current: .disabled, fetched: home), .disabled)
        XCTAssertEqual(
            HomeAvailability.state(for: ChatHomeResponse(enabled: true, conversationID: nil)),
            .disabled
        )
    }

    func testFailedHomeLookupIsNotThreadsOffAndRetryRecovers() async throws {
        let suite = "HomeThreadsTests.discovery"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        var fails = true
        var lookups = 0
        let discovery = HomeDiscovery(
            serverURL: serverURL,
            defaults: defaults,
            lookUp: {
                lookups += 1
                if fails {
                    throw URLError(.notConnectedToInternet)
                }
                return ChatHomeResponse(enabled: true, conversationID: "web_conv_home")
            },
            reportFailure: { _ in }
        )
        XCTAssertEqual(discovery.state, .unknown)

        await discovery.refresh()

        XCTAssertEqual(discovery.state, .failed, "A failed lookup says nothing about whether threads are on.")
        XCTAssertEqual(
            HomeAvailability.cached(serverURL: serverURL, defaults: defaults),
            .unknown,
            "A failure is never remembered as threads off."
        )

        fails = false
        await discovery.retryIfFailed()

        XCTAssertEqual(discovery.state, .enabled(homeConversationID: "web_conv_home"))
        XCTAssertEqual(
            HomeAvailability.cached(serverURL: serverURL, defaults: defaults),
            .enabled(homeConversationID: "web_conv_home")
        )

        await discovery.retryIfFailed()
        XCTAssertEqual(lookups, 2, "Retry looks again only after a failure.")
    }

    func testRouteArrivingWhileLookupFailedReachesHomeAfterRetry() async throws {
        let suite = "HomeThreadsTests.heldRoute"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        let failures = LockedStrings()
        failures.append("fail")
        let discovery = HomeDiscovery(
            serverURL: serverURL,
            defaults: defaults,
            lookUp: {
                if !failures.values.isEmpty {
                    throw URLError(.notConnectedToInternet)
                }
                return ChatHomeResponse(enabled: true, conversationID: "web_conv_home")
            },
            reportFailure: { _ in }
        )
        await discovery.refresh()
        XCTAssertEqual(discovery.state, .failed)

        let prompt = ChatRoute(initialPrompt: "Buy milk")
        let share = ChatRoute(newConversationRequestID: "b1", sharedAttachmentBatchID: "b1")
        XCTAssertEqual(
            HomeDiscovery.deliverableRoute(prompt, state: discovery.state),
            ChatRoute(),
            "The provisional conversation list must not consume a routed prompt."
        )
        XCTAssertEqual(HomeDiscovery.deliverableRoute(share, state: discovery.state), ChatRoute())
        XCTAssertEqual(HomeDiscovery.deliverableRoute(prompt, state: .unknown), ChatRoute())

        failures.reset()
        await discovery.retryIfFailed()

        let delivered = HomeDiscovery.deliverableRoute(prompt, state: discovery.state)
        XCTAssertEqual(delivered, prompt)
        XCTAssertEqual(
            HomeRouteAction.resolve(delivered, homeConversationID: "web_conv_home"),
            .sendInHome(prompt: "Buy milk")
        )
        XCTAssertEqual(HomeDiscovery.deliverableRoute(share, state: .disabled), share)
    }

    func testSigningInAgainLooksHomeUpAfresh() async throws {
        let suite = "HomeThreadsTests.restart"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        HomeAvailability.store(.enabled(homeConversationID: "home-a"), serverURL: serverURL, defaults: defaults)
        let responses = LockedStrings()
        let discovery = HomeDiscovery(
            serverURL: serverURL,
            defaults: defaults,
            lookUp: {
                guard let next = responses.values.last else {
                    throw URLError(.notConnectedToInternet)
                }
                return ChatHomeResponse(enabled: true, conversationID: next)
            },
            reportFailure: { _ in }
        )
        XCTAssertEqual(discovery.state, .enabled(homeConversationID: "home-a"))

        // Signed in again as someone else: their Home replaces the previous one.
        responses.append("home-b")
        await discovery.restart()
        XCTAssertEqual(discovery.state, .enabled(homeConversationID: "home-b"))

        // A failed lookup after signing in again never falls back to the
        // previous account's Home.
        responses.reset()
        await discovery.restart()
        XCTAssertEqual(discovery.state, .failed)
    }

    func testHomeTakesOverTheFallbackListsDraft() {
        let fallback = makeViewModel(conversationID: "web_conv_old", role: .standalone)
        let attachment = ChatAttachment(
            id: "draft-file",
            attachmentID: "66666666-6666-6666-6666-666666666666",
            type: .document,
            name: "quote.pdf",
            contentURL: "/api/attachments/66666666-6666-6666-6666-666666666666",
            mimeType: "application/pdf",
            size: 10,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
        fallback.draftText = "Typed before Retry"
        fallback.draftAttachments = [attachment]

        let home = makeViewModel(conversationID: "home", role: .home)
        home.inheritDraft(fallback.currentDraft)

        XCTAssertEqual(home.currentDraft, ChatDraft(text: "Typed before Retry", attachments: [attachment]))

        home.draftText = "Already here"
        home.inheritDraft(ChatDraft(text: "Other", attachments: []))
        XCTAssertEqual(home.draftText, "Already here", "A draft being written is not overwritten.")
    }

    func testFailedLookupKeepsARememberedHome() async throws {
        let suite = "HomeThreadsTests.discoveryRemembered"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        HomeAvailability.store(.enabled(homeConversationID: "home"), serverURL: serverURL, defaults: defaults)
        let discovery = HomeDiscovery(
            serverURL: serverURL,
            defaults: defaults,
            lookUp: { throw URLError(.timedOut) },
            reportFailure: { _ in }
        )

        await discovery.refresh()

        XCTAssertEqual(discovery.state, .enabled(homeConversationID: "home"))
    }

    // MARK: - API client

    func testGetHomeTreatsAMissingRouteAsThreadsOff() async throws {
        ChatMockBackendURLProtocol.respond { _ in .json(#"{"detail":"Not Found"}"#, statusCode: 404) }

        let home = try await ChatAPIClient(authManager: makeAuthManager()).getHome()

        XCTAssertEqual(home, ChatHomeResponse(enabled: false, conversationID: nil))
    }

    func testGetHomeDecodesTheHome() async throws {
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/v1/chat/home")
            return .json(#"{"enabled":true,"conversation_id":"web_conv_home"}"#)
        }

        let home = try await ChatAPIClient(authManager: makeAuthManager()).getHome()

        XCTAssertEqual(home, ChatHomeResponse(enabled: true, conversationID: "web_conv_home"))
    }

    func testOrdinaryConversationsAskForOrdinaryKind() async throws {
        let queries = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            queries.append(request.url?.query ?? "")
            return .json(#"{"conversations":[],"count":0}"#)
        }

        _ = try await ChatAPIClient(authManager: makeAuthManager()).listOrdinaryConversations()

        XCTAssertEqual(queries.values.count, 1)
        XCTAssertTrue(queries.values[0].contains("kind=ordinary"))
    }

    // MARK: - Send feedback

    func testSendShowsSendingAtOnceAndTakesOneTapInHome() async throws {
        try await assertSendShowsSendingWhileItsPreflightRuns(
            role: .home,
            extraJSON: #","conversation_kind":"home""#
        )
    }

    func testSendShowsSendingAtOnceAndTakesOneTapInAPushedThread() async throws {
        try await assertSendShowsSendingWhileItsPreflightRuns(
            role: .pushed,
            extraJSON: """
            ,"latest_user_profile_id":"research","conversation_kind":"thread",
            "thread": \(threadJSON(id: "conv", state: "waiting"))
            """
        )
    }

    func testSendShowsSendingAtOnceAndTakesOneTapStandalone() async throws {
        try await assertSendShowsSendingWhileItsPreflightRuns(role: .standalone, extraJSON: "")
    }

    func testASendStillStartingDoesNotHoldAConversationOpenedMeanwhile() async throws {
        let preflight = HangingStream()
        let preflights = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            guard request.httpMethod == "GET", path.hasSuffix("/messages") else {
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            let id = path.split(separator: "/").dropLast().last.map(String.init) ?? ""
            let page = #"{"conversation_id":"\#(id)","messages":[],"count":0,"total_messages":0}"#
            guard (request.url?.query ?? "").split(separator: "&").contains("limit=1") else {
                return .json(page)
            }
            preflights.append(path)
            return .hangingStream(page, controller: preflight)
        }
        let model = makeViewModel(conversationID: "a", role: .standalone)
        await model.selectConversation("a")
        model.draftText = "Hello"
        let send = Task { await model.sendDraft() }
        try await waitUntil { preflights.values.count == 1 }
        XCTAssertTrue(model.isSubmittingDraft)

        await model.selectConversation("b")
        model.draftText = "Other"

        XCTAssertFalse(model.isSubmittingDraft, "The send in progress belongs to the conversation left behind.")
        // A background catch-up of the newly opened conversation may briefly
        // hold the composer while it loads; only the other send must not.
        try await waitUntil { model.canSendDraft }
        XCTAssertTrue(preflights.values.count == 1 && !model.isSubmittingDraft)
        preflight.finish()
        await send.value
        XCTAssertFalse(model.isSubmittingDraft)
        XCTAssertTrue(model.messages.isEmpty, "The abandoned send is not shown in the conversation opened since.")
    }

    /// The send path asks the server whether a turn is already running before
    /// it shows the message. Over a slow network that round trip is the gap in
    /// which the button looked dead, so hold it open and check what the user
    /// sees meanwhile, then let the turn fail to start and check it says so.
    private func assertSendShowsSendingWhileItsPreflightRuns(
        role: ChatViewModel.Role,
        extraJSON: String
    ) async throws {
        let preflight = HangingStream()
        let preflights = LockedStrings()
        let turnPosts = LockedStrings()
        let page = """
        {"conversation_id":"conv","messages":[],"count":0,"total_messages":0\(extraJSON)}
        """
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/turns" {
                turnPosts.append(path)
                return .json(#"{"detail":"server is down"}"#, statusCode: 500)
            }
            guard request.httpMethod == "GET", path.hasSuffix("/messages") else {
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            let query = request.url?.query ?? ""
            guard query.split(separator: "&").contains("limit=1") else {
                return .json(page)
            }
            preflights.append(path)
            return .hangingStream(page, controller: preflight)
        }
        let model = makeViewModel(conversationID: "conv", role: role)
        await model.selectConversation("conv")
        model.draftText = "Hello"
        XCTAssertTrue(model.canSendDraft)

        let send = Task { await model.sendDraft() }
        try await waitUntil { preflights.values.count == 1 }

        XCTAssertTrue(model.isSubmittingDraft, "The composer shows the send in progress.")
        XCTAssertFalse(model.canSendDraft, "A send in progress takes no second send.")
        await model.sendDraft()
        XCTAssertEqual(preflights.values.count, 1, "A second tap starts no second send.")

        preflight.finish()
        await send.value

        XCTAssertFalse(model.isSubmittingDraft)
        XCTAssertEqual(model.messages.filter { $0.role == .user }.map(\.text), ["Hello"])
        XCTAssertEqual(model.draftText, "")
        try await waitUntil { !model.isStreaming }
        XCTAssertFalse(turnPosts.values.isEmpty)
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed, "A turn that fails to start says so.")
        XCTAssertNotNil(model.failedSend(for: assistant))
    }

    // MARK: - View model roles

    func testThreadAdoptsItsOwnProfileWithoutAUserMessage() async {
        respondWithConversation(
            id: "t1",
            messagesJSON: #"[{"internal_id":1,"role":"assistant","content":"Gas or electric?","timestamp":"2026-06-08T12:00:01Z","processing_profile_id":"browser_profile"}]"#,
            extraJSON: """
            ,"latest_user_profile_id":"browser_profile","conversation_kind":"thread",
            "thread": \(threadJSON(id: "t1", state: "waiting", profileID: "browser_profile"))
            """
        )
        let model = makeViewModel(conversationID: "t1", role: .pushed)

        await model.selectConversation("t1")

        XCTAssertEqual(model.selectedProfileID, "browser_profile")
        XCTAssertEqual(model.conversationKind, .thread)
        XCTAssertEqual(model.threadSummary?.title, "Thread t1")
        XCTAssertFalse(model.isConfirmedOrdinaryConversation)
    }

    func testPushedThreadStaysUnsendableUntilItsLoadSucceeds() async {
        PreferredProfile.store("research")
        let loads = LockedStrings()
        let turnPosts = LockedStrings()
        let threadJSON = threadJSON(id: "t1", state: "waiting", profileID: "browser_profile")
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/turns" {
                turnPosts.append(path)
                return .json(#"{"detail":"unexpected"}"#, statusCode: 500)
            }
            guard request.httpMethod == "GET", path.hasSuffix("/messages") else {
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            loads.append(path)
            if loads.values.count == 1 {
                return .json(#"{"detail":"upstream unavailable"}"#, statusCode: 503)
            }
            return .json("""
            {"conversation_id":"t1","messages":[],"count":0,"total_messages":0,
             "latest_user_profile_id":"browser_profile","conversation_kind":"thread","thread":\(threadJSON)}
            """)
        }
        let model = makeViewModel(conversationID: "t1", role: .pushed)

        await model.selectConversation("t1")
        model.draftText = "Either, but flag the rebate"

        XCTAssertTrue(model.conversationMetadataLoadFailed)
        XCTAssertFalse(model.canSendDraft, "A thread whose description failed to load must not send.")
        await model.sendDraft()
        XCTAssertTrue(turnPosts.values.isEmpty, "Nothing is posted under a guessed profile.")

        await model.retryConversationLoad()

        XCTAssertFalse(model.conversationMetadataLoadFailed)
        XCTAssertEqual(model.selectedProfileID, "browser_profile")
        XCTAssertTrue(model.canSendDraft)
    }

    func testHomeRunsOnTheDefaultProfileWhateverItsLatestRowRanOn() async {
        respondWithConversation(
            id: "home",
            messagesJSON: #"[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z","processing_profile_id":"research"}]"#,
            extraJSON: #","latest_user_profile_id":"research","conversation_kind":"home""#
        )
        let model = makeViewModel(conversationID: "home", role: .home)

        await model.selectConversation("home")

        XCTAssertEqual(model.selectedProfileID, "default_assistant")
    }

    func testIntelligenceControlWaitsUntilTheConversationIsDescribed() async {
        let loads = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            guard request.url?.path.hasSuffix("/messages") == true else {
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            loads.append("load")
            if loads.values.count == 1 {
                return .json(#"{"detail":"upstream unavailable"}"#, statusCode: 503)
            }
            return .json(
                #"{"conversation_id":"t1","messages":[],"count":0,"total_messages":0,"conversation_kind":"conversation"}"#
            )
        }
        let linked = makeViewModel(conversationID: "t1", role: .pushed)
        XCTAssertFalse(linked.isConversationDescribed, "Not described before its history loads.")

        await linked.selectConversation("t1")
        XCTAssertFalse(linked.isConversationDescribed, "Not described after a failed load.")

        await linked.retryConversationLoad()
        XCTAssertTrue(linked.isConversationDescribed)
    }

    func testStartupAppliesTheRouteWithoutWaitingForTheThreadList() async {
        var events: [String] = []
        var releaseThreads: CheckedContinuation<Void, Never>?
        let threadsStarted = expectation(description: "thread list requested")

        let startup = Task { @MainActor in
            await HomeStartup.run(
                bootstrap: { events.append("bootstrap") },
                refreshThreads: {
                    await withCheckedContinuation { continuation in
                        releaseThreads = continuation
                        threadsStarted.fulfill()
                    }
                    events.append("threads")
                },
                applyRoute: { events.append("route") }
            )
        }
        await fulfillment(of: [threadsStarted], timeout: 5)
        // The thread list is still outstanding; the route must not have waited.
        while !events.contains("route") {
            await Task.yield()
        }
        XCTAssertEqual(events, ["bootstrap", "route"])

        releaseThreads?.resume()
        await startup.value
        XCTAssertEqual(events, ["bootstrap", "route", "threads"])
    }

    func testHomeWithoutHistoryRunsOnTheDefaultProfile() async {
        PreferredProfile.store("research")
        respondWithConversation(
            id: "home",
            messagesJSON: "[]",
            extraJSON: #","conversation_kind":"home""#
        )
        let model = makeViewModel(conversationID: "home", role: .home)

        await model.selectConversation("home")

        XCTAssertEqual(model.selectedProfileID, "default_assistant")
        XCTAssertEqual(model.conversationKind, .home)
        XCTAssertFalse(model.isConfirmedOrdinaryConversation)
    }

    func testOrdinaryConversationOverHomeIsConfirmedByItsHistory() async {
        respondWithConversation(
            id: "old",
            messagesJSON: #"[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z","processing_profile_id":"research"}]"#,
            extraJSON: #","latest_user_profile_id":"research","conversation_kind":"conversation""#
        )
        let model = makeViewModel(conversationID: "old", role: .pushed)

        await model.selectConversation("old")

        XCTAssertEqual(model.selectedProfileID, "research")
        XCTAssertTrue(model.isConfirmedOrdinaryConversation)
    }

    func testOnlyHomeAndPushedLoadsAskForConversationProfile() async {
        let queries = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if path.hasSuffix("/messages") {
                queries.append(request.url?.query ?? "")
                return .json(#"{"conversation_id":"c","messages":[],"count":0,"total_messages":0}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        await makeViewModel(conversationID: "c", role: .standalone).selectConversation("c")
        await makeViewModel(conversationID: "c", role: .pushed).selectConversation("c")

        XCTAssertEqual(queries.values.count, 2)
        XCTAssertFalse(queries.values[0].contains("include_conversation_profile"))
        XCTAssertTrue(queries.values[1].contains("include_conversation_profile=true"))
    }

    func testHomeKeepsNoConversationListAndForwardsActivityAndResync() async {
        let listRequests = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/v1/chat/conversations" {
                listRequests.append(request.url?.query ?? "")
            }
            return .json(#"{"conversations":[],"count":0}"#)
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        var activitySignals = 0
        model.onListRefresh = { activitySignals += 1 }

        let refreshed = await model.refreshConversations()
        await model.activityStreamDidSignal(generation: model.syncCoordinator.activityGeneration)
        XCTAssertEqual(activitySignals, 1)

        // A resync catches up on pings the activity stream missed while it was down.
        let snapshotApplied = await model.applyListSnapshot()
        await model.applyRecentListSnapshot()

        XCTAssertTrue(refreshed)
        XCTAssertTrue(snapshotApplied)
        XCTAssertTrue(listRequests.values.isEmpty)
        XCTAssertEqual(activitySignals, 3)
    }

    func testPromptRoutedMidBootstrapSendsOnceOnHomesProfile() async throws {
        PreferredProfile.store("research")
        let messagesRequested = expectation(description: "Home's history requested")
        let releaseHistory = DispatchSemaphore(value: 0)
        let historyLoads = LockedStrings()
        let turnProfiles = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/profiles"):
                return .json("""
                {"profiles":[
                  {"id":"default_assistant","description":"d","available_tools":[],"enabled_mcp_servers":[],"delegation_only":false},
                  {"id":"research","description":"r","available_tools":[],"enabled_mcp_servers":[],"delegation_only":false}
                ],"default_profile_id":"default_assistant"}
                """)
            case ("GET", "/api/v1/chat/conversations/home/messages"):
                historyLoads.append(request.url?.query ?? "")
                if historyLoads.values.count == 1 {
                    // Hold the bootstrap load open until the route has arrived.
                    messagesRequested.fulfill()
                    releaseHistory.wait()
                }
                return .json(
                    #"{"conversation_id":"home","messages":[],"count":0,"total_messages":0,"conversation_kind":"home"}"#
                )
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Self.body(of: request)) as? [String: Any]
                )
                turnProfiles.append(payload["profile_id"] as? String ?? "<nil>")
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"home","first_seq":0}"#)
            case ("GET", "/api/v1/chat/conversations/home/stream"):
                return .droppedStream("")
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let model = makeViewModel(conversationID: "home", role: .home)

        let bootstrap = Task { await model.bootstrap() }
        await fulfillment(of: [messagesRequested], timeout: 10)

        // The route lands (twice, as a re-applied route does) while Home is
        // still loading: nothing may be sent yet.
        await model.sendPromptInPlace("Buy milk")
        await model.sendPromptInPlace("Buy milk")
        XCTAssertTrue(turnProfiles.values.isEmpty)

        releaseHistory.signal()
        await bootstrap.value
        try await waitUntil { !turnProfiles.values.isEmpty }

        XCTAssertEqual(turnProfiles.values, ["default_assistant"])
        await model.sendPromptInPlace("Buy milk")
        XCTAssertEqual(turnProfiles.values.count, 1, "A prompt already sent is not sent again.")
    }

    func testRoutedPromptLeavesTheDraftInPlace() async throws {
        let turnPayloads = LockedPayloads()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/conversations/home/messages"):
                return .json(
                    #"{"conversation_id":"home","messages":[],"count":0,"total_messages":0,"conversation_kind":"home"}"#
                )
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Self.body(of: request)) as? [String: Any]
                )
                turnPayloads.append(payload)
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"home","first_seq":0}"#)
            case ("GET", "/api/v1/chat/conversations/home/stream"):
                return .droppedStream("")
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        await model.bootstrap()
        let draftAttachment = ChatAttachment(
            id: "draft-photo",
            attachmentID: "55555555-5555-5555-5555-555555555555",
            type: .image,
            name: "photo.jpg",
            contentURL: "/api/attachments/55555555-5555-5555-5555-555555555555",
            mimeType: "image/jpeg",
            size: 10,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
        model.draftText = "Half typed"
        model.draftAttachments = [draftAttachment]

        await model.sendPromptInPlace("Buy milk")
        try await waitUntil { !turnPayloads.values.isEmpty }

        let payload = try XCTUnwrap(turnPayloads.values.first)
        XCTAssertEqual(payload["prompt"] as? String, "Buy milk")
        XCTAssertTrue(
            (payload["attachments"] as? [Any])?.isEmpty ?? true,
            "The routed prompt goes alone, without the draft's attachments."
        )
        XCTAssertEqual(model.draftText, "Half typed")
        XCTAssertEqual(model.draftAttachments, [draftAttachment])
    }

    func testPromptHeldByAForeignTurnSendsWhenThatTurnEnds() async throws {
        let historyLoads = LockedStrings()
        let turnPrompts = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/conversations/home/messages"):
                historyLoads.append(request.url?.query ?? "")
                // Home's first load finds a turn running that another device
                // started; by any later load it has finished.
                let activeTurns = historyLoads.values.count == 1
                    ? #"[{"turn_id":"turn-remote","started_at":"2026-06-08T12:00:00Z","latest_seq":3,"status":"running"}]"#
                    : "[]"
                return .json("""
                {"conversation_id":"home","messages":[],"count":0,"total_messages":0,
                 "conversation_kind":"home","active_turns":\(activeTurns)}
                """)
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Self.body(of: request)) as? [String: Any]
                )
                turnPrompts.append(payload["prompt"] as? String ?? "<nil>")
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"home","first_seq":0}"#)
            case ("GET", "/api/v1/chat/conversations/home/stream"):
                return .droppedStream("")
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        await model.bootstrap()

        await model.sendPromptInPlace("Buy milk")

        XCTAssertFalse(model.isReadyForRoutedPrompt, "A turn running on another device holds the prompt.")
        XCTAssertEqual(model.heldRoutedPrompts, ["Buy milk"], "A held prompt is visible, not silent.")
        XCTAssertTrue(turnPrompts.values.isEmpty)

        // The other device's turn ends, seen on the follow stream.
        let turnEnded = ChatStreamEvent(
            type: .turnEnded,
            turnID: "turn-remote",
            seq: 9,
            text: nil,
            toolCall: nil,
            toolCallID: nil,
            toolResult: nil,
            attachments: [],
            attachmentSource: .response,
            confirmation: nil,
            confirmationResult: nil,
            errorMessage: nil,
            status: "complete"
        )
        _ = await model.handleFollowEvent(
            turnEnded,
            conversationID: "home",
            generation: model.syncCoordinator.followGeneration
        )
        try await waitUntil { !turnPrompts.values.isEmpty }

        XCTAssertEqual(turnPrompts.values, ["Buy milk"])
        XCTAssertTrue(model.heldRoutedPrompts.isEmpty)
    }

    func testOrdinaryControlsWaitUntilTheConversationIsKnownToBeOrdinary() async {
        respondWithConversation(id: "t1", messagesJSON: "[]", extraJSON: #","conversation_kind":"thread""#)
        let linked = makeViewModel(conversationID: "t1", role: .pushed)
        XCTAssertFalse(
            linked.isConfirmedOrdinaryConversation,
            "A conversation opened by a link is not known to be ordinary before its history loads."
        )
        await linked.selectConversation("t1")
        XCTAssertFalse(linked.isConfirmedOrdinaryConversation)
    }

    func testHomeDoesNotBecomeTheRestoredConversation() {
        _ = makeViewModel(conversationID: "home", role: .home)
        _ = makeViewModel(conversationID: "t1", role: .pushed)

        XCTAssertNil(UserDefaults.standard.string(forKey: "lastConversationId"))
    }

    // MARK: - Helpers

    // MARK: - User overrides: starting threads

    func testStartThreadSendsEveryChoiceAndListsTheThread() async throws {
        let bodies = LockedPayloads()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/home/threads" {
                bodies.append(Self.jsonObject(of: request))
                return .json("""
                {"thread":\(self.threadJSON(id: "t-new", state: "working", extra: #","anchor_message_id":12"#)),
                 "turn":{"turn_id":"turn-1","conversation_id":"t-new","first_seq":4},"copied_message_ids":[]}
                """)
            }
            return .json(#"{"home_conversation_id":"home","threads":[]}"#)
        }
        let store = makeStore()
        let start = HomeThreadStart(
            turnID: "turn-1",
            profileID: "research",
            prompt: "Compare the quotes",
            anchorMessageID: 12,
            includeContext: false,
            modelTier: "deep",
            attachments: [uploadedAttachment()]
        )

        let result = await store.startThread(start)

        let thread = try XCTUnwrap(try? result?.get())
        XCTAssertEqual(thread.conversationID, "t-new")
        XCTAssertEqual(thread.anchorMessageID, 12)
        let body = try XCTUnwrap(bodies.values.first)
        XCTAssertEqual(body["profile_id"] as? String, "research")
        XCTAssertEqual(body["prompt"] as? String, "Compare the quotes")
        XCTAssertEqual(body["turn_id"] as? String, "turn-1")
        XCTAssertEqual(body["anchor_message_id"] as? Int, 12)
        XCTAssertEqual(body["include_context"] as? Bool, false)
        XCTAssertEqual(body["model_tier"] as? String, "deep")
        let attachments = try XCTUnwrap(body["attachments"] as? [[String: Any]])
        XCTAssertEqual(attachments.first?["id"] as? String, "local-1")
        XCTAssertEqual(attachments.first?["content"] as? String, "/api/attachments/att-1")
    }

    func testNewThreadLeavesUnchosenFieldsOut() async throws {
        let bodies = LockedPayloads()
        ChatMockBackendURLProtocol.respond { request in
            if request.httpMethod == "POST" {
                bodies.append(Self.jsonObject(of: request))
                return .json(#"{"thread":\#(self.threadJSON(id: "t-new", state: "working")),"turn":null}"#)
            }
            return .json(#"{"home_conversation_id":"home","threads":[]}"#)
        }
        let store = makeStore()

        _ = await store.startThread(HomeThreadStart(turnID: "turn-2", profileID: "research", prompt: "Plan the trip"))

        let body = try XCTUnwrap(bodies.values.first)
        XCTAssertEqual(Set(body.keys), ["profile_id", "prompt", "turn_id", "include_context"])
        XCTAssertEqual(body["include_context"] as? Bool, true)
    }

    func testRefusedStartIsReportedWithTheServersReason() async throws {
        let detail = "Profile 'browser_profile' does not receive household context, so Home conversation can't be carried into it."
        ChatMockBackendURLProtocol.respond { request in
            if request.httpMethod == "POST" {
                return .json(#"{"detail":"\#(detail)"}"#, statusCode: 409)
            }
            return .json(#"{"home_conversation_id":"home","threads":[]}"#)
        }
        let spool = FileManager.default.temporaryDirectory
            .appendingPathComponent("thread-start-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spool) }
        let store = makeStore(errorReporter: ErrorReporter(spoolDirectory: spool, dedupeWindow: 0))

        let result = await store.startThread(HomeThreadStart(
            turnID: "turn-4",
            profileID: "browser_profile",
            prompt: nil,
            anchorMessageID: 3
        ))

        guard case let .failure(failure) = result else {
            return XCTFail("A refused start is a failure, not \(String(describing: result))")
        }
        XCTAssertEqual(failure.message, detail)
        XCTAssertTrue(store.threads.isEmpty)
        try await waitUntil { Self.spooledComponents(in: spool).contains("Chat.startThread") }
    }

    func testSendAsThreadFailureReachesHomesAlert() async {
        ChatMockBackendURLProtocol.respond { request in
            if request.httpMethod == "POST" {
                return .json(#"{"detail":"A thread needs an opening message."}"#, statusCode: 422)
            }
            return .json(#"{"home_conversation_id":"home","threads":[]}"#)
        }
        let store = makeStore()
        var opened: [String] = []
        store.openConversation = { opened.append($0) }

        let started = await store.startThreadAndOpen(HomeThreadStart(turnID: "turn-5", profileID: "research", prompt: nil))

        XCTAssertFalse(started)
        XCTAssertEqual(store.actionFailure?.message, "A thread needs an opening message.")
        XCTAssertTrue(opened.isEmpty)
    }

    func testStartedSendAsThreadOpensTheThread() async {
        ChatMockBackendURLProtocol.respond { request in
            if request.httpMethod == "POST" {
                return .json(#"{"thread":\#(self.threadJSON(id: "t-new", state: "working")),"turn":null}"#)
            }
            return .json(#"{"home_conversation_id":"home","threads":[]}"#)
        }
        let store = makeStore()
        var opened: [String] = []
        store.openConversation = { opened.append($0) }

        let started = await store.startThreadAndOpen(HomeThreadStart(turnID: "turn-6", profileID: "research", prompt: "Hi"))

        XCTAssertTrue(started)
        XCTAssertEqual(opened, ["t-new"])
        XCTAssertNil(store.actionFailure)
    }

    func testContextIsOfferedOnlyWhereTheProfileTakesHomeConversations() {
        let takesContext: Set<String> = ["default_assistant", "research"]
        let research = ThreadStartOptions(anchorMessageID: 7, profileID: "research", householdContextProfileIDs: takesContext)
        let browser = ThreadStartOptions(anchorMessageID: 7, profileID: "browser_profile", householdContextProfileIDs: takesContext)
        let unknown = ThreadStartOptions(anchorMessageID: 7, profileID: "research", householdContextProfileIDs: nil)
        let newThread = ThreadStartOptions(anchorMessageID: nil, profileID: "research", householdContextProfileIDs: takesContext)

        XCTAssertTrue(research.offersContext)
        XCTAssertFalse(browser.offersContext, "A profile without household context is offered a request-only start.")
        XCTAssertFalse(unknown.offersContext, "Nothing is offered on a guess before the profiles load.")
        XCTAssertFalse(newThread.offersContext, "A new thread has no Home exchange to bring.")

        XCTAssertTrue(research.canStart(prompt: "", includeContext: true), "Context can stand on its own.")
        XCTAssertFalse(research.canStart(prompt: " ", includeContext: false))
        XCTAssertFalse(browser.canStart(prompt: "", includeContext: true), "A request-only start needs its request.")
        XCTAssertTrue(browser.canStart(prompt: "Book it", includeContext: true))

        let refusedContext = browser.start(turnID: "t", prompt: " Book it ", includeContext: true)
        XCTAssertFalse(refusedContext.includeContext, "The toggle never sends context the server would refuse.")
        XCTAssertEqual(refusedContext.prompt, "Book it")
        XCTAssertEqual(refusedContext.anchorMessageID, 7)
        XCTAssertTrue(research.start(turnID: "t", prompt: "", includeContext: true).includeContext)
        XCTAssertNil(research.start(turnID: "t", prompt: "", includeContext: true).prompt)
        XCTAssertTrue(newThread.start(turnID: "t", prompt: "Hi", includeContext: false).includeContext)
    }

    func testARetriedStartKeepsItsTurnIDOnlyWhileNothingChanged() {
        let first = HomeThreadStart(turnID: "first", profileID: "research", prompt: "Plan it", anchorMessageID: 4)
        let unchanged = HomeThreadStart(turnID: "second", profileID: "research", prompt: "Plan it", anchorMessageID: 4)
        let edited = HomeThreadStart(turnID: "third", profileID: "research", prompt: "Plan it all", anchorMessageID: 4)
        var otherProfile = unchanged
        otherProfile.profileID = "default_assistant"

        XCTAssertEqual(unchanged.retrying(first).turnID, "first", "A retry is the same start to the server.")
        XCTAssertEqual(edited.retrying(first).turnID, "third", "An edited start is a new one.")
        XCTAssertEqual(otherProfile.retrying(first).turnID, "second")
        XCTAssertEqual(unchanged.retrying(nil).turnID, "second")
    }

    // MARK: - User overrides: the profile list

    func testAProfileListMissedAtLaunchLoadsWhenAskedForAgain() async {
        var loads = 0
        ChatMockBackendURLProtocol.respond { _ in
            loads += 1
            if loads == 1 {
                return .json(#"{"detail":"unavailable"}"#, statusCode: 500)
            }
            return .json(Self.oneProfileJSON)
        }
        let model = makeViewModel(conversationID: "home", role: .home)

        await model.loadProfiles()
        XCTAssertEqual(model.profileListState, .unavailable)

        await model.ensureProfilesLoaded()
        XCTAssertEqual(model.profileListState, .ready)
        XCTAssertEqual(model.profiles.map(\.id), ["default_assistant"])

        await model.ensureProfilesLoaded()
        XCTAssertEqual(loads, 2, "A list it already holds is not fetched again.")
    }

    func testAnEmptyProfileListFromAServerStillStartingIsNotReady() async {
        var loads = 0
        ChatMockBackendURLProtocol.respond { _ in
            loads += 1
            return .json(loads == 1 ? #"{"profiles":[],"default_profile_id":"default_assistant"}"# : Self.oneProfileJSON)
        }
        let model = makeViewModel(conversationID: "home", role: .home)

        await model.loadProfiles()
        XCTAssertEqual(model.profileListState, .unavailable)

        await model.ensureProfilesLoaded()
        XCTAssertEqual(model.profileListState, .ready)
    }

    func testReturningToTheForegroundLoadsAMissingProfileList() async throws {
        var loads = 0
        ChatMockBackendURLProtocol.respond { request in
            guard request.url?.path == "/api/v1/profiles" else {
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            loads += 1
            return .json(loads == 1 ? #"{"profiles":[],"default_profile_id":"default_assistant"}"# : Self.oneProfileJSON)
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        await model.loadProfiles()

        model.scenePhaseChanged(old: .background, new: .active)

        try await waitUntil { model.profileListState == .ready }
    }

    func testAProfileLoadInProgressIsNotDuplicated() async {
        var loads = 0
        ChatMockBackendURLProtocol.respond { _ in
            loads += 1
            return .json(Self.oneProfileJSON)
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        model.isLoadingProfiles = true
        XCTAssertEqual(model.profileListState, .loading)

        await model.ensureProfilesLoaded()

        XCTAssertEqual(loads, 0)
    }

    private static let oneProfileJSON = """
    {"profiles":[{"id":"default_assistant","description":"","available_tools":[],"enabled_mcp_servers":[],
     "delegation_only":false}],"default_profile_id":"default_assistant"}
    """

    // MARK: - User overrides: send-as-thread

    func testSendAsThreadCarriesTheDraftsAttachmentsAndTierAndClearsTheComposer() async throws {
        let model = try await makeHomeModelWithProfiles()
        model.draftText = "  Research heat pumps  "
        model.draftAttachments = [uploadedAttachment()]
        model.selectModelTier("deep")
        var starts: [HomeThreadStart] = []
        var wasShownSending = false

        await model.sendDraftAsThread(profileID: "research") { start in
            wasShownSending = model.isSubmittingDraft && !model.canSendDraft
            starts.append(start)
            return true
        }

        let start = try XCTUnwrap(starts.first)
        XCTAssertTrue(wasShownSending, "The send button shows the send in progress, as an ordinary send does.")
        XCTAssertEqual(start.profileID, "research")
        XCTAssertEqual(start.prompt, "Research heat pumps")
        XCTAssertEqual(start.attachments.map(\.id), ["local-1"])
        XCTAssertEqual(start.modelTier, "deep")
        XCTAssertNil(start.anchorMessageID)
        XCTAssertEqual(model.draftText, "")
        XCTAssertTrue(model.draftAttachments.isEmpty)
        XCTAssertNil(model.modelTierChoice, "Home's tier choice was for that one message.")
        XCTAssertFalse(model.isSubmittingDraft)
    }

    func testSendAsThreadRetryReusesItsTurnIDUntilItSucceeds() async throws {
        let model = try await makeHomeModelWithProfiles()
        model.draftText = "Research heat pumps"
        var starts: [HomeThreadStart] = []

        await model.sendDraftAsThread(profileID: "research") { starts.append($0); return false }
        XCTAssertEqual(model.draftText, "Research heat pumps", "A failed start keeps the message.")
        await model.sendDraftAsThread(profileID: "research") { starts.append($0); return true }
        model.draftText = "Research heat pumps"
        await model.sendDraftAsThread(profileID: "research") { starts.append($0); return true }

        XCTAssertEqual(starts.count, 3)
        XCTAssertEqual(starts[0].turnID, starts[1].turnID, "A retry is the same start to the server.")
        XCTAssertNotEqual(starts[1].turnID, starts[2].turnID, "A new send after success is a new thread.")
    }

    func testSendAsThreadKeepsATierChosenMeanwhileForTheNextMessage() async throws {
        let model = try await makeHomeModelWithProfiles()
        model.draftText = "Research heat pumps"

        await model.sendDraftAsThread(profileID: "research") { _ in
            model.selectModelTier("deep")
            return true
        }

        XCTAssertEqual(model.modelTierChoice, "deep", "A choice made during the send is for the next message.")

        model.draftText = "Another"
        await model.sendDraftAsThread(profileID: "research") { _ in true }
        XCTAssertNil(model.modelTierChoice, "The choice sent with a message is used up by it.")
    }

    func testSendAsThreadRetryWithAChangedTierIsANewStart() async throws {
        let model = try await makeHomeModelWithProfiles()
        model.draftText = "Research heat pumps"
        var starts: [HomeThreadStart] = []

        await model.sendDraftAsThread(profileID: "research") { starts.append($0); return false }
        model.selectModelTier("deep")
        await model.sendDraftAsThread(profileID: "research") { starts.append($0); return false }

        XCTAssertEqual(starts.count, 2)
        XCTAssertNil(starts[0].modelTier)
        XCTAssertEqual(starts[1].modelTier, "deep", "The retry sends the tier now chosen.")
        XCTAssertNotEqual(starts[0].turnID, starts[1].turnID, "A changed tier is a new start.")
    }

    func testSendAsThreadSendsTheTierChosenBesideTheProfile() async throws {
        let model = try await makeHomeModelWithProfiles()
        model.draftText = "Research heat pumps"
        model.selectModelTier("deep")
        var starts: [HomeThreadStart] = []

        await model.sendDraftAsThread(profileID: "research", modelTier: nil) { starts.append($0); return true }

        XCTAssertNil(starts.first?.modelTier, "The sheet's pick overrides Home's choice.")
        XCTAssertNil(model.modelTierChoice, "Home's choice was still for that message.")
    }

    func testThreadStartSendsATierOnlyWithAnOpeningMessage() {
        let options = ThreadStartOptions(anchorMessageID: 7, profileID: "research", householdContextProfileIDs: ["research"])

        XCTAssertEqual(
            options.start(turnID: "t", prompt: " Go ", includeContext: true, modelTier: "deep").modelTier,
            "deep"
        )
        XCTAssertNil(
            options.start(turnID: "t", prompt: "  ", includeContext: true, modelTier: "deep").modelTier,
            "With no message there is no turn for the tier to run on."
        )
    }

    func testSendAsThreadDropsATierTheThreadsProfileDoesNotOffer() async throws {
        let model = try await makeHomeModelWithProfiles()
        model.draftText = "Quick one"
        model.selectModelTier("deep")
        var starts: [HomeThreadStart] = []

        await model.sendDraftAsThread(profileID: "plain") { starts.append($0); return true }

        XCTAssertNil(starts.first?.modelTier)
    }

    func testSendingInHomeStartsAThreadOnTheDefaultProfile() async throws {
        let model = try await makeHomeModelWithProfiles()
        var starts: [HomeThreadStart] = []
        model.startHomeThread = { starts.append($0); return true }
        model.draftText = "Is the bin out?"

        await model.sendDraft()

        XCTAssertEqual(starts.map(\.profileID), [model.defaultProfileID])
        XCTAssertEqual(starts.first?.prompt, "Is the bin out?")
        XCTAssertEqual(model.draftText, "")
    }

    func testAnAttachmentSentAloneInHomeAsksForAMessageAndStaysInTheComposer() async throws {
        let model = try await makeHomeModelWithProfiles()
        var starts: [HomeThreadStart] = []
        model.startHomeThread = { starts.append($0); return true }
        model.draftAttachments = [uploadedAttachment()]

        await model.sendDraft()

        XCTAssertTrue(starts.isEmpty)
        XCTAssertEqual(model.draftAttachments.count, 1)
    }

    func testARoutedPromptRetriesItsThreadStartUnderTheSameTurnID() async throws {
        let model = try await makeHomeModelWithProfiles()
        var starts: [HomeThreadStart] = []
        var accepts = false
        model.startHomeThread = { starts.append($0); return accepts }
        await model.bootstrap()

        await model.sendPromptInPlace("Book the plumber")
        accepts = true
        await model.reloadMessages()
        try await waitUntil { starts.count == 2 }
        guard starts.count == 2 else {
            return
        }

        XCTAssertEqual(starts[0].turnID, starts[1].turnID)
        XCTAssertTrue(model.heldRoutedPrompts.isEmpty)
    }

    func testAStartedThreadMarkerDecodesItsThread() throws {
        let json = """
        {"internal_id": 5, "role": "assistant", "content": "(marker)",
         "timestamp": "2026-10-18T09:30:00Z", "started_thread": "t1"}
        """

        let message = try JSONDecoder.chatDecoder.decode(ChatBackendMessage.self, from: Data(json.utf8))

        XCTAssertEqual(message.startedThread, "t1")
    }

    func testSendAsThreadNeedsTextAndIsHomeOnly() async throws {
        let model = try await makeHomeModelWithProfiles()
        model.draftAttachments = [uploadedAttachment()]
        XCTAssertTrue(model.canSendDraft)
        XCTAssertFalse(model.canSendDraftAsThread, "Attachments go only with an opening message.")

        let pushed = makeViewModel(conversationID: "t1", role: .pushed)
        pushed.draftText = "Hi"
        XCTAssertFalse(pushed.canSendDraftAsThread)
    }

    // MARK: - User overrides: moving a turn

    func testMoveDestinationsAreThreadsThatTakeHomeConversationsAndAreAtRest() {
        let browser = ChatThreadSummary(
            conversationID: "browser", homeConversationID: "home", title: "browser", profileID: "browser_profile",
            state: .waiting, sourceToolCallID: nil, seedRequest: nil, preview: nil, createdAt: Date(), updatedAt: Date()
        )
        let threads = [thread("working", .working), browser, thread("waiting", .waiting), thread("done", .done)]

        let destinations = ThreadMoveDestination.list(threads: threads, householdContextProfileIDs: ["research"])

        XCTAssertEqual(destinations.map(\.id), ["waiting", "done", "working", "browser"], "Offerable threads come first.")
        XCTAssertEqual(destinations.filter(\.isAvailable).map(\.id), ["waiting", "done"])
        XCTAssertEqual(
            destinations.first { $0.id == "browser" }?.unavailableReason,
            "Runs on browser_profile, which doesn’t take Home conversations."
        )
        XCTAssertNotNil(destinations.first { $0.id == "working" }?.unavailableReason)
    }

    func testMoveOffersNoDestinationBeforeTheProfilesLoad() {
        let destinations = ThreadMoveDestination.list(threads: [thread("t1", .waiting)], householdContextProfileIDs: nil)

        XCTAssertEqual(destinations.count, 1)
        XCTAssertFalse(destinations[0].isAvailable)
    }

    func testMoveIsOfferedOnlyAtRestForAnExchangeTheUserStarted() {
        let user = homeMessage(id: 1, role: .user, turnID: "a")
        let reply = homeMessage(id: 2, role: .assistant, turnID: "a")
        let proactive = homeMessage(id: 3, role: .assistant, turnID: "b")
        var echo = homeMessage(id: 4, role: .assistant, turnID: "a")
        echo.echoedFromThread = "t1"
        var marker = homeMessage(id: 5, role: .assistant, turnID: "c")
        marker.movedToThread = "t1"
        let unsaved = ChatMessage(
            id: "local_assistant_x", role: .assistant, text: "…", createdAt: Date(), toolCalls: [], attachments: [],
            isLoading: true, status: .running, processingProfileID: nil, errorTraceback: nil, turnID: "d"
        )
        let messages = [user, reply, proactive, echo, marker, unsaved]
        let userTurns = HomeTurnThreadActions.userStartedTurnIDs(in: messages)
        func actions(_ message: ChatMessage, atRest: Bool = true, running: Set<String> = []) -> HomeTurnThreadActions? {
            HomeTurnThreadActions.resolve(
                for: message,
                userStartedTurnIDs: userTurns,
                homeIsAtRest: atRest,
                runningTurnIDs: running
            )
        }

        XCTAssertEqual(actions(reply), HomeTurnThreadActions(messageID: 2, canContinue: true, canMove: true))
        XCTAssertEqual(actions(user)?.canMove, true)
        XCTAssertEqual(actions(reply, atRest: false)?.canMove, false, "Move waits for Home to be at rest.")
        XCTAssertEqual(actions(reply, atRest: false)?.canContinue, true)
        XCTAssertNil(actions(reply, running: ["a"]), "Nothing is offered on a turn still running.")
        XCTAssertEqual(actions(proactive)?.canMove, false, "A message nobody asked for stays in Home.")
        XCTAssertEqual(actions(proactive)?.canContinue, true)
        XCTAssertEqual(actions(echo)?.canMove, false, "A thread's echoed outcome stays in Home.")
        XCTAssertNil(actions(marker))
        XCTAssertNil(actions(unsaved), "An unsaved bubble has no id the server knows.")
    }

    func testAFinishedHomeMessageCanBeSharedAlone() {
        let home = makeViewModel(conversationID: "home", role: .home)

        XCTAssertEqual(
            home.shareTarget(for: homeMessage(id: 2, role: .assistant, turnID: "a")),
            .message(conversationID: "home", messageID: 2)
        )
    }

    func testAReplyThatIsOnlyAnAttachmentCanBeShared() {
        let home = makeViewModel(conversationID: "home", role: .home)
        var picture = homeMessage(id: 2, role: .assistant, turnID: "a")
        picture.text = ""
        picture.attachments = [
            ChatAttachment(
                id: "picture",
                attachmentID: "picture",
                type: .image,
                name: "picture.png",
                contentURL: "/api/attachments/picture",
                mimeType: "image/png",
                size: nil,
                localFileURL: nil,
                uploadState: .uploaded,
                errorMessage: nil
            )
        ]

        XCTAssertEqual(home.shareTarget(for: picture), .message(conversationID: "home", messageID: 2))
    }

    func testAMessageWithNothingOfItsOwnCannotBeShared() {
        let home = makeViewModel(conversationID: "home", role: .home)
        var moved = homeMessage(id: 3, role: .assistant, turnID: "a")
        moved.movedToThread = "t1"
        var started = homeMessage(id: 4, role: .assistant, turnID: "b")
        started.startedThread = "t1"
        let unsaved = ChatMessage(
            id: "local_assistant_x", role: .assistant, text: "…", createdAt: Date(), toolCalls: [], attachments: [],
            isLoading: true, status: .running, processingProfileID: nil, errorTraceback: nil, turnID: "c"
        )

        XCTAssertNil(home.shareTarget(for: moved), "A marker is a pointer, not a message.")
        XCTAssertNil(home.shareTarget(for: started), "A marker is a pointer, not a message.")
        XCTAssertNil(home.shareTarget(for: unsaved), "An unsaved bubble has no id the server knows.")
    }

    func testOnlyHomeOffersMessageSharing() {
        let pushed = makeViewModel(conversationID: "t1", role: .pushed)

        XCTAssertNil(pushed.shareTarget(for: homeMessage(id: 1, role: .assistant, turnID: "a")))
    }

    func testOnlyHomeOffersThreadActions() {
        let pushed = makeViewModel(conversationID: "t1", role: .pushed)

        XCTAssertNil(pushed.threadActions(for: homeMessage(id: 1, role: .user, turnID: "a")))
    }

    func testMovingATurnSendsItsMessageAndShowsTheMarkerInHome() async throws {
        let moves = LockedPayloads()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/home/threads/t1/moved-turns" {
                moves.append(Self.jsonObject(of: request))
                return .json(#"{"thread":\#(self.threadJSON(id: "t1", state: "waiting")),"marker_message_id":9,"moved_message_ids":[20,21]}"#)
            }
            if path == "/api/v1/chat/home/threads" {
                return .json(#"{"home_conversation_id":"home","threads":[\#(self.threadJSON(id: "t1", state: "waiting"))]}"#)
            }
            if path.hasSuffix("/messages") {
                let rows = moves.values.isEmpty
                    ? #"""
                    [{"internal_id":1,"turn_id":"a","role":"user","content":"Bins?","timestamp":"2026-06-08T12:00:00Z"},
                     {"internal_id":2,"turn_id":"a","role":"assistant","content":"Tuesday.","timestamp":"2026-06-08T12:00:01Z"}]
                    """#
                    : #"""
                    [{"internal_id":9,"turn_id":"a","role":"assistant","content":"(moved)","timestamp":"2026-06-08T12:10:00Z",
                      "moved_to_thread":"t1"}]
                    """#
                return .json(#"{"conversation_id":"home","messages":\#(rows),"count":1,"total_messages":1,"conversation_kind":"home"}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }
        let home = makeViewModel(conversationID: "home", role: .home)
        await home.selectConversation("home")
        let store = makeStore()
        store.homeModel = home

        let result = await store.moveTurn(messageID: 2, to: "t1")

        XCTAssertEqual(try? result?.get().markerMessageID, 9)
        XCTAssertEqual(moves.values.first?["message_id"] as? Int, 2)
        XCTAssertEqual(home.messages.map(\.id), ["msg_9"], "The moved turn leaves Home; its marker stays.")
        XCTAssertEqual(home.messages.first?.movedToThread, "t1")
        XCTAssertEqual(store.thread(conversationID: "t1")?.conversationID, "t1")
    }

    func testRefusedMoveKeepsTheTurnAndSaysWhy() async {
        ChatMockBackendURLProtocol.respond { request in
            if request.httpMethod == "POST" {
                return .json(#"{"detail":"Wait for the reply in progress to finish, then move the turn."}"#, statusCode: 409)
            }
            return .json(#"{"home_conversation_id":"home","threads":[]}"#)
        }
        let store = makeStore()

        let result = await store.moveTurn(messageID: 2, to: "t1")

        guard case let .failure(failure) = result else {
            return XCTFail("A refused move is a failure, not \(String(describing: result))")
        }
        XCTAssertEqual(failure.title, "Couldn’t Move to Thread")
        XCTAssertEqual(failure.message, "Wait for the reply in progress to finish, then move the turn.")
    }

    func testHomeRowMarkingAMoveCarriesTheThread() async {
        respondWithConversation(
            id: "home",
            messagesJSON: """
            [{"internal_id":3,"turn_id":"a","role":"assistant","content":"(moved)","timestamp":"2026-06-08T12:01:00Z",
              "moved_to_thread":"t1"}]
            """,
            extraJSON: #","conversation_kind":"home""#
        )
        let model = makeViewModel(conversationID: "home", role: .home)

        await model.selectConversation("home")

        XCTAssertEqual(model.messages.map(\.movedToThread), ["t1"])
    }

    func testAMarkerArrivingRetiresTheMovedTurnsRows() {
        let held = [
            homeMessage(id: 1, role: .user, turnID: "a"),
            homeMessage(id: 2, role: .assistant, turnID: "a"),
            homeMessage(id: 3, role: .user, turnID: "b"),
        ]
        var marker = homeMessage(id: 9, role: .assistant, turnID: "a")
        marker.movedToThread = "t1"

        XCTAssertEqual(ChatViewModel.removingMovedTurns(held, markedIn: [marker]).map(\.id), ["msg_3"])
        XCTAssertEqual(
            ChatViewModel.removingMovedTurns(held, markedIn: [homeMessage(id: 4, role: .assistant, turnID: "a")]).map(\.id),
            ["msg_1", "msg_2", "msg_3"],
            "Only a marker retires anything."
        )
        XCTAssertEqual(ChatViewModel.removingMovedTurns(held + [marker], markedIn: [marker]).map(\.id), ["msg_3", "msg_9"])
    }

    func testContinuedThreadQuotesItsAnchorWhileHomeHoldsIt() async {
        respondWithConversation(
            id: "home",
            messagesJSON: """
            [{"internal_id":7,"turn_id":"a","role":"assistant","content":"Bins go out Tuesday.","timestamp":"2026-06-08T12:01:00Z"}]
            """,
            extraJSON: #","conversation_kind":"home""#
        )
        let home = makeViewModel(conversationID: "home", role: .home)
        await home.selectConversation("home")
        let store = makeStore()
        store.homeModel = home
        var anchored = thread("t1", .waiting)
        anchored.anchorMessageID = 7
        var gone = thread("t2", .waiting)
        gone.anchorMessageID = 99

        XCTAssertEqual(store.anchorQuote(for: anchored), "Bins go out Tuesday.")
        XCTAssertNil(store.anchorQuote(for: gone), "A quote whose message has left Home is simply not shown.")
        XCTAssertNil(store.anchorQuote(for: thread("t3", .waiting)))
    }

    func testProfileDecodesWhetherItTakesHomeConversations() throws {
        let json = #"""
        {"profiles":[
          {"id":"a","description":"","available_tools":[],"enabled_mcp_servers":[],"delegation_only":false,
           "receives_household_context":true},
          {"id":"b","description":"","available_tools":[],"enabled_mcp_servers":[],"delegation_only":false}
        ],"default_profile_id":"a"}
        """#
        let response = try JSONDecoder.chatDecoder.decode(ChatProfilesResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.profiles.map(\.receivesHouseholdContext), [true, false])
    }

    /// Home, described and loaded, with three profiles: the default and
    /// `research` offering a "deep" tier, and `plain` offering none.
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

    private func makeHomeModelWithProfiles() async throws -> ChatViewModel {
        let tiers = #"[{"id":"standard","label":"Standard"},{"id":"deep","label":"Deep"}]"#
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if path == "/api/v1/profiles" {
                return .json("""
                {"profiles":[
                  {"id":"default_assistant","description":"","available_tools":[],"enabled_mcp_servers":[],
                   "delegation_only":false,"model_tiers":\(tiers),"default_model_tier":"standard",
                   "receives_household_context":true},
                  {"id":"research","description":"","available_tools":[],"enabled_mcp_servers":[],
                   "delegation_only":false,"model_tiers":\(tiers),"default_model_tier":"standard",
                   "receives_household_context":true},
                  {"id":"plain","description":"","available_tools":[],"enabled_mcp_servers":[],"delegation_only":false}
                ],"default_profile_id":"default_assistant"}
                """)
            }
            if path.hasSuffix("/messages") {
                return .json(#"{"conversation_id":"home","messages":[],"count":0,"total_messages":0,"conversation_kind":"home"}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        await model.loadProfiles()
        await model.selectConversation("home")
        XCTAssertEqual(model.householdContextProfileIDs, ["default_assistant", "research"])
        XCTAssertTrue(model.offersModelTierChoice)
        return model
    }

    private func homeMessage(id: Int, role: ChatMessageRole, turnID: String) -> ChatMessage {
        ChatMessage(
            id: "msg_\(id)",
            role: role,
            text: "Message \(id)",
            createdAt: Date(),
            toolCalls: [],
            attachments: [],
            isLoading: false,
            status: .complete,
            processingProfileID: nil,
            errorTraceback: nil,
            turnID: turnID
        )
    }

    private func uploadedAttachment() -> ChatAttachment {
        ChatAttachment(
            id: "local-1",
            attachmentID: "att-1",
            type: .image,
            name: "photo.jpg",
            contentURL: "/api/attachments/att-1",
            mimeType: "image/jpeg",
            size: 10,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
    }

    private nonisolated static func jsonObject(of request: URLRequest) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: body(of: request)) as? [String: Any]) ?? [:]
    }

    private static func spooledComponents(in directory: URL) -> [String] {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.compactMap { file in
            guard file.pathExtension == "json", let data = try? Data(contentsOf: file) else {
                return nil
            }
            return (try? JSONDecoder().decode(ErrorReportPayload.self, from: data))?.componentName
        }
    }

    private nonisolated static func body(of request: URLRequest) -> Data {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return Data()
        }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }

    private func waitUntil(timeout: TimeInterval = 10, _ condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else {
                XCTFail("Timed out waiting for condition")
                return
            }
            await Task.yield()
            try await Task.sleep(for: .milliseconds(20))
        }
    }

    private func makeAuthManager() -> AuthManager {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return authManager
    }

    private func makeViewModel(conversationID: String?, role: ChatViewModel.Role) -> ChatViewModel {
        let model = ChatViewModel(
            authManager: makeAuthManager(),
            conversationID: conversationID,
            role: role,
            errorReporter: ErrorReporter(spoolDirectory: nil),
            pathMonitor: StubPathMonitor(isSatisfied: true)
        )
        models.append(model)
        return model
    }

    private func respondWithConversation(id: String, messagesJSON: String, extraJSON: String) {
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json("""
                {"conversation_id":"\(id)","messages":\(messagesJSON),"count":0,"total_messages":0\(extraJSON)}
                """)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }
    }

    private nonisolated func threadJSON(
        id: String,
        state: String,
        sourceToolCallID: String? = nil,
        profileID: String = "research",
        extra: String = ""
    ) -> String {
        let source = sourceToolCallID.map { "\"\($0)\"" } ?? "null"
        return """
        {"conversation_id":"\(id)","home_conversation_id":"web_conv_home","title":"Thread \(id)",
         "profile_id":"\(profileID)","state":"\(state)","source_tool_call_id":\(source),
         "seed_request":"Do \(id)","preview":null,
         "created_at":"2026-06-08T12:00:00Z","updated_at":"2026-06-08T12:05:00.123456+00:00"\(extra)}
        """
    }

    private func thread(
        _ id: String,
        _ state: ChatThreadState,
        preview: String? = nil,
        seed: String? = nil,
        closedBy: ChatThreadCloser? = nil,
        unread: Int = 0
    ) -> ChatThreadSummary {
        ChatThreadSummary(
            conversationID: id,
            homeConversationID: "home",
            title: id,
            profileID: "research",
            state: state,
            sourceToolCallID: nil,
            seedRequest: seed,
            preview: preview,
            createdAt: Date(),
            updatedAt: Date(),
            closedBy: closedBy,
            unreadCount: unread
        )
    }

    private func conversation(_ id: String, kind: String?) -> ChatConversationSummary {
        ChatConversationSummary(
            conversationID: id,
            lastMessage: "",
            lastTimestamp: Date(),
            messageCount: 1,
            kind: kind
        )
    }

    private func toolCall(
        _ id: String,
        name: String,
        status: ChatToolStatus,
        result: String? = nil
    ) -> ChatToolCall {
        ChatToolCall(id: id, name: name, argumentsText: "{}", resultText: result, attachments: [], status: status)
    }

    private func resetStoredState() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
        UserDefaults.standard.removeObject(forKey: "lastConversationId")
        UserDefaults.standard.removeObject(forKey: "lastConversationActiveAt")
        UserDefaults.standard.removeObject(forKey: "selectedProfileId")
        HomeAvailability.clear()
    }
}

private final class LockedPayloads: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [[String: Any]] = []

    var values: [[String: Any]] {
        lock.withLock { stored }
    }

    func append(_ value: [String: Any]) {
        lock.withLock { stored.append(value) }
    }
}

private final class LockedStrings: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [String] = []

    var values: [String] {
        lock.withLock { stored }
    }

    func append(_ value: String) {
        lock.withLock { stored.append(value) }
    }

    func reset() {
        lock.withLock { stored.removeAll() }
    }
}
