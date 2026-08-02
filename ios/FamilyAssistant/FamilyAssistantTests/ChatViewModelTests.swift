import Foundation
import UIKit
import XCTest

@testable import FamilyAssistant

@MainActor
final class ChatViewModelTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private var trackedViewModels: [WeakChatViewModelReference] = []

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
        for reference in trackedViewModels {
            guard let model = reference.model else { continue }
            model.scenePhaseChanged(old: .active, new: .background)
            model.cancelStream()
        }
        trackedViewModels.removeAll()
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

    func testLaunchWithStaleConversationOpensNewChat() {
        storeLastConversation("web_conv_stale", activeSecondsAgo: 16 * 60)

        let model = makeViewModel(conversationID: nil)

        XCTAssertEqual(model.conversationSelection, model.conversationID)
        XCTAssertNotEqual(model.conversationID, "web_conv_stale")
        XCTAssertTrue(model.conversationID?.hasPrefix("web_conv_") == true)
        XCTAssertNotNil(model.composerFocusRequestID)
        XCTAssertEqual(
            UserDefaults.standard.string(forKey: "lastConversationId"),
            "web_conv_stale",
            "Opening an empty draft must not overwrite the stored last conversation."
        )
    }

    func testBootstrapWithStaleConversationDoesNotPersistEmptyLaunchDraft() async {
        storeLastConversation("web_conv_stale", activeSecondsAgo: 16 * 60)
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: nil)
        await model.bootstrap()

        XCTAssertEqual(model.conversationSelection, model.conversationID)
        XCTAssertEqual(
            UserDefaults.standard.string(forKey: "lastConversationId"),
            "web_conv_stale",
            "Bootstrapping an empty launch draft must not make it the restored conversation."
        )
    }

    func testConstructionDoesNotStartPathMonitorUntilBootstrap() async {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let monitor = StubPathMonitor(isSatisfied: true)
        let model = makeViewModel(conversationID: nil, pathMonitor: monitor)

        XCTAssertEqual(monitor.startCount, 0)

        await model.bootstrap()

        XCTAssertGreaterThanOrEqual(monitor.startCount, 1)
    }

    func testBootstrapDoesNotAutoSendUserTypedLaunchDraft() async {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: nil)
        // Launch focuses the composer. The user starts typing their first word
        // while bootstrap's profile/conversation fetches are still in flight, so
        // the draft is non-empty by the time bootstrap reaches its send check.
        model.draftText = "hello"

        await model.bootstrap()

        XCTAssertEqual(
            model.draftText,
            "hello",
            "A word the user typed into the launch composer must not be auto-submitted by bootstrap."
        )
        XCTAssertTrue(
            model.messages.isEmpty,
            "Bootstrap must not start a turn from the user's in-progress launch draft."
        )
    }

    func testBootstrapAutoSendsUntouchedLaunchSeededDraft() async throws {
        let model = makeViewModel(conversationID: nil, initialPrompt: "Remember the milk")
        let conversationID = try XCTUnwrap(model.conversationID)
        var streamedTurnID = "turn-seeded"
        ChatMockBackendURLProtocol.respond { request in
            let method = request.httpMethod ?? "GET"
            let path = request.url?.path ?? ""
            switch (method, path) {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"\#(conversationID)","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/\(conversationID)/stream"):
                return .text(
                    """
                    event: turn_started
                    data: {"turn_id":"\(streamedTurnID)","seq":0}

                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"complete"}

                    """
                )
            case ("GET", "/api/v1/chat/conversations/\(conversationID)/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"\(conversationID)",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Remember the milk","timestamp":"2026-06-08T12:00:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        await model.bootstrap()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(
            model.draftText,
            "",
            "A launch-seeded draft the user left untouched should be auto-sent."
        )
        XCTAssertTrue(
            model.messages.contains { $0.role == .user && $0.text == "Remember the milk" },
            "The seeded prompt should have been submitted as the user's message."
        )
    }

    func testLaunchWithoutActivityTimestampOpensNewChat() {
        UserDefaults.standard.set("web_conv_legacy", forKey: "lastConversationId")
        UserDefaults.standard.removeObject(forKey: "lastConversationActiveAt")

        let model = makeViewModel(conversationID: nil)

        XCTAssertEqual(model.conversationSelection, model.conversationID)
        XCTAssertNotEqual(model.conversationID, "web_conv_legacy")
        XCTAssertNotNil(model.composerFocusRequestID)
    }

    func testLaunchWithNoStoredConversationOpensNewChat() {
        let model = makeViewModel(conversationID: nil)

        XCTAssertEqual(model.conversationSelection, model.conversationID)
        XCTAssertTrue(model.conversationID?.hasPrefix("web_conv_") == true)
        XCTAssertNotNil(model.composerFocusRequestID)
    }

    func testEmptyLaunchDraftIsLiveWhenActivityStreamConnects() {
        let model = makeViewModel(conversationID: nil)

        model.syncCoordinator.apply(
            .activityConnected(generation: model.syncCoordinator.activityGeneration)
        )

        XCTAssertEqual(model.syncPresentation, .live)
    }

    func testExplicitNewConversationRequestIgnoresRestoreWindow() {
        storeLastConversation("web_conv_recent", activeSecondsAgo: 60)

        let model = makeViewModel(conversationID: nil, startsNewConversation: true)

        XCTAssertEqual(model.conversationSelection, model.conversationID)
        XCTAssertNotEqual(model.conversationID, "web_conv_recent")
        XCTAssertTrue(model.conversationID?.hasPrefix("web_conv_") == true)
        XCTAssertNotNil(model.composerFocusRequestID)
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

    // MARK: - Auto-follow: local send force-scroll (scene-update watchdog)

    func testSendDraftRequestsScrollToLatestSoALocalSendIsAlwaysShown() async {
        // A local send must always scroll into view, even if the user had
        // scrolled up to read history — but right after a send the newest bubble
        // is the assistant loading placeholder, so this intent can't be inferred
        // from the last role and is signalled explicitly via
        // `scrollToLatestRequestID` (which the chat view drives as
        // `StickyBottomScroll.forceFollowTrigger`). See
        // scratch/FamilyAssistant-2026-07-07-090155.ips.
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                return .json(#"{"turn_id":"t","conversation_id":"web_conv_scroll","first_seq":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_scroll/stream"):
                return .text(
                    """
                    event: turn_started
                    data: {"turn_id":"t","seq":0}

                    event: turn_ended
                    data: {"turn_id":"t","status":"complete"}

                    """
                )
            default:
                return .json("{}")
            }
        }

        let model = makeViewModel(conversationID: "web_conv_scroll")
        let before = model.scrollToLatestRequestID
        model.draftText = "Anything else?"
        await model.sendDraft()

        XCTAssertGreaterThan(
            model.scrollToLatestRequestID, before,
            "Sending a message must request a scroll to the newest bubble."
        )
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
        let total = ChatViewModel.displayedMessageWindowCount + 15
        model.messages = (0..<total).map(plainMessage(index:))

        let visible = model.visibleGroupedMessages
        XCTAssertEqual(visible.count, ChatViewModel.displayedMessageWindowCount)
        XCTAssertTrue(model.hasEarlierMessages)
        // The window is the most-recent suffix: newest is shown, oldest is hidden.
        XCTAssertEqual(visible.last?.id, "msg_\(total - 1)")
        XCTAssertEqual(visible.first?.id, "msg_\(total - ChatViewModel.displayedMessageWindowCount)")
    }

    func testShowEarlierMessagesSlidesFixedWindowToStartOfShortThread() {
        let model = makeViewModel(conversationID: "web_conv_window")
        let total = ChatViewModel.displayedMessageWindowCount + 15
        model.messages = (0..<total).map(plainMessage(index:))

        let initialVisible = model.visibleGroupedMessages.count
        model.showEarlierMessages()
        XCTAssertEqual(model.visibleGroupedMessages.count, initialVisible)

        // The final older page is clamped to the start and overlaps the prior
        // page rather than widening the eager stack.
        XCTAssertEqual(model.visibleGroupedMessages.count, ChatViewModel.displayedMessageWindowCount)
        XCTAssertEqual(model.visibleGroupedMessages.first?.id, "msg_0")
        XCTAssertFalse(model.hasEarlierMessages)
        XCTAssertTrue(model.hasNewerMessages)
    }

    func testPagingKeepsFixedEagerRenderWindow() {
        let model = makeViewModel(conversationID: "web_conv_window")
        let total = ChatViewModel.displayedMessageWindowCount + 75
        model.messages = (0..<total).map(plainMessage(index:))

        XCTAssertEqual(model.visibleGroupedMessages.count, ChatViewModel.displayedMessageWindowCount)
        XCTAssertEqual(
            model.visibleGroupedMessages.first?.id,
            "msg_\(total - ChatViewModel.displayedMessageWindowCount)"
        )
        XCTAssertEqual(model.visibleGroupedMessages.last?.id, "msg_\(total - 1)")
        XCTAssertTrue(model.hasEarlierMessages)

        model.showEarlierMessages()

        XCTAssertEqual(model.visibleGroupedMessages.count, ChatViewModel.displayedMessageWindowCount)
        XCTAssertEqual(
            model.visibleGroupedMessages.first?.id,
            "msg_\(total - ChatViewModel.displayedMessageWindowCount - 30)"
        )
        XCTAssertEqual(model.visibleGroupedMessages.last?.id, "msg_\(total - 31)")
        XCTAssertTrue(model.hasEarlierMessages)
        XCTAssertTrue(model.hasNewerMessages)

        while model.hasEarlierMessages {
            model.showEarlierMessages()
        }

        XCTAssertEqual(model.visibleGroupedMessages.first?.id, "msg_0")
        XCTAssertEqual(model.visibleGroupedMessages.count, ChatViewModel.displayedMessageWindowCount)
        XCTAssertTrue(model.hasNewerMessages)
        XCTAssertFalse(model.hasEarlierMessages)

        model.showNewerMessages()

        XCTAssertEqual(model.visibleGroupedMessages.first?.id, "msg_30")
        XCTAssertTrue(model.hasEarlierMessages)
        XCTAssertTrue(model.hasNewerMessages)
    }

    func testPagedBackWindowStaysAnchoredWhenNewMessagesAppend() {
        let model = makeViewModel(conversationID: "web_conv_window")
        let total = ChatViewModel.displayedMessageWindowCount + 75
        model.messages = (0..<total).map(plainMessage(index:))

        while model.hasEarlierMessages {
            model.showEarlierMessages()
        }

        XCTAssertEqual(model.visibleGroupedMessages.first?.id, "msg_0")
        XCTAssertEqual(model.visibleGroupedMessages.last?.id, "msg_29")

        model.appendMessagePreservingPagedBackWindow(plainMessage(index: total))

        XCTAssertEqual(model.visibleGroupedMessages.first?.id, "msg_0")
        XCTAssertEqual(model.visibleGroupedMessages.last?.id, "msg_29")
        XCTAssertFalse(model.hasEarlierMessages)
        XCTAssertTrue(model.hasNewerMessages)
    }

    func testShortThreadShowsEverythingWithoutEarlierControl() {
        let model = makeViewModel(conversationID: "web_conv_short")
        model.messages = (0..<3).map(plainMessage(index:))

        XCTAssertEqual(model.visibleGroupedMessages.count, 3)
        XCTAssertFalse(model.hasEarlierMessages)
    }

    func testStartingNewConversationResetsTheWindow() {
        let model = makeViewModel(conversationID: "web_conv_window")
        model.messages = (0..<(ChatViewModel.displayedMessageWindowCount + 15)).map(plainMessage(index:))
        model.showEarlierMessages()
        XCTAssertTrue(model.hasNewerMessages)

        model.startNewConversation()

        XCTAssertEqual(model.displayedMessageNewerOffset, 0)
        XCTAssertFalse(model.hasNewerMessages)
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

    func testOpeningConversationAdoptsItsProfile() async throws {
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_complex",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Plan my trip","timestamp":"2026-06-08T12:00:00Z","processing_profile_id":"complex_tasks"},
                        {"internal_id":2,"role":"assistant","content":"On it","timestamp":"2026-06-08T12:00:01Z","processing_profile_id":"engineer"}
                      ],
                      "count":2,
                      "total_messages":2,
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
        XCTAssertEqual(model.selectedProfileID, "default_assistant")

        await model.selectConversation("web_conv_complex")

        // The thread's history is partitioned by profile on the backend, so the
        // active profile must match the one its turns ran under. The last USER
        // message carries the entry profile (complex_tasks); the assistant row is
        // tagged with the delegate it ran (engineer) and must NOT be adopted.
        XCTAssertEqual(
            model.selectedProfileID,
            "complex_tasks",
            "Opening a conversation must adopt the profile its user turns were sent under."
        )
    }

    func testStartingNewConversationUsesPreferredNotAdoptedProfile() async throws {
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_complex",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Plan my trip","timestamp":"2026-06-08T12:00:00Z","processing_profile_id":"complex_tasks"}
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
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let model = makeViewModel(conversationID: nil)
        await model.selectConversation("web_conv_complex")
        XCTAssertEqual(model.selectedProfileID, "complex_tasks")

        model.startNewConversation()

        XCTAssertEqual(
            model.selectedProfileID,
            "default_assistant",
            "A new conversation runs under the preferred profile, not the previously-opened thread's profile."
        )
    }

    func testLoadProfilesDoesNotResetSelectionWhenListEmpty() async throws {
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path == "/api/v1/profiles" {
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let model = makeViewModel(conversationID: nil)
        model.selectedProfileID = "complex_tasks"

        await model.loadProfiles()

        // An empty list is what `/v1/profiles` returns during a backend cold
        // start; resetting then would permanently overwrite the user's choice.
        XCTAssertEqual(
            model.selectedProfileID,
            "complex_tasks",
            "A transiently empty profiles list must not clobber the active selection."
        )
    }

    func testSendCapturesProfileAtSendTimeNotStartTurnTime() async throws {
        // The turn must POST under the profile selected when the send was issued,
        // not a later `selectedProfileID`. `sendDraft` captures the profile into the
        // ActiveTurnSession and only then spawns the transport task, so a profile
        // switch made before that task suspends into its `startTurn` POST (here,
        // synchronously on the main actor right after `sendDraft` returns, before
        // any await lets the task run) must not retarget the in-flight turn.
        let startProfileID = AtomicString()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                startProfileID.set(payload["profile_id"] as? String ?? "<nil>")
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_profile_capture","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_profile_capture/stream"):
                return .droppedStream("")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_profile_capture/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_profile_capture","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_profile_capture")
        model.selectedProfileID = "complex_tasks"
        model.draftText = "Hi"
        await model.sendDraft()
        // No await between here and the switch: the just-spawned transport task
        // (also main-actor-isolated) cannot have reached its startTurn POST yet.
        model.selectedProfileID = "engineer"

        try await waitUntil { startProfileID.value != nil }

        XCTAssertEqual(
            startProfileID.value,
            "complex_tasks",
            "The turn POST must carry the profile captured at send time, not the later switch."
        )
    }

    func testCannotSendWhileConversationMessagesAreLoading() {
        let model = makeViewModel(conversationID: "web_conv_loading")
        model.draftText = "follow up"
        XCTAssertTrue(model.canSendDraft)

        // While a switched-to conversation's history is still loading its profile
        // has not been adopted yet, so sending must be blocked to avoid posting
        // under the previous profile (which the backend would filter history by).
        model.isLoadingMessages = true
        XCTAssertFalse(
            model.canSendDraft,
            "Sending must be blocked until the conversation's history loads and its profile is adopted."
        )
    }

    func testStartNewConversationClearsLoadingGate() {
        let model = makeViewModel(conversationID: nil)
        // Simulate an in-flight conversation load being superseded (e.g. a
        // deep-link route starting a new conversation mid-load); the orphaned
        // loadMessages early-returns and leaves isLoadingMessages set.
        model.isLoadingMessages = true

        model.startNewConversation()

        XCTAssertFalse(
            model.isLoadingMessages,
            "A new conversation has no history to load; the loading gate must clear."
        )
        // A new conversation must be immediately sendable so its deep-link /
        // share-extension auto-send isn't dropped by a stale loading flag.
        model.draftText = "Deep link"
        XCTAssertTrue(model.canSendDraft)
    }

    func testStartNewConversationDefersFollowingUntilServerBacked() {
        // A brand-new conversation has no server-side row yet. Opening a follow
        // stream on its client-generated id would 404-loop the reconnect backoff and
        // pin the connection indicator to `.degraded` (a permanent "Live updates
        // degraded" warning + reconnect spinner) even while the first send streams
        // fine over its own transport. `startNewConversation` must therefore defer
        // following — like a fresh thread opened at launch — so `currentConversationID()`
        // returns nil (liveness falls back to the account-global activity stream) until
        // the first accepted turn makes the conversation server-backed.
        let model = makeViewModel(conversationID: "existing_conv")
        XCTAssertEqual(
            model.currentConversationID(), "existing_conv",
            "an existing, server-backed conversation is followed"
        )

        model.startNewConversation()

        XCTAssertNil(
            model.currentConversationID(),
            "a brand-new conversation defers following (no 404 reconnect loop) until its first turn"
        )
        XCTAssertNil(
            model.resyncSelectedConversationID,
            "and is hidden from the foreground resync target too, so a foreground/reconnect before the first send does not 404 the resync snapshot on the client-only id"
        )
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

    func testSendDraftOptimisticallyAddsNewConversationToList() async throws {
        var streamedTurnID = "turn-optimistic"
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_optimistic","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_optimistic/stream"):
                return .text(
                    """
                    event: turn_started
                    data: {"turn_id":"\(streamedTurnID)","seq":0}

                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"complete"}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                // The server has not yet surfaced the brand-new conversation
                // (race between persistence and the post-turn refresh).
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_optimistic/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_optimistic","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_optimistic")
        model.draftText = "Plan the trip"

        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(
            model.conversations.map(\.conversationID),
            ["web_conv_optimistic"],
            "A brand-new conversation must appear in the list on send and survive a server refresh that hasn't caught up."
        )
        XCTAssertEqual(model.conversations.first?.lastMessage, "Plan the trip")
    }

    func testOptimisticConversationReconciledWithoutDuplicate() async throws {
        var streamedTurnID = "turn-reconcile"
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_reconcile","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_reconcile/stream"):
                return .text(
                    """
                    event: turn_started
                    data: {"turn_id":"\(streamedTurnID)","seq":0}

                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"complete"}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                // A future timestamp models the server having caught up (the
                // persisted reply is newer than the optimistic send), so the
                // freshness guard retires the optimistic row for the server's.
                return .json(
                    #"{"conversations":[{"conversation_id":"web_conv_reconcile","last_message":"Persisted reply","last_timestamp":"2030-01-01T00:00:00Z","message_count":2}],"count":1}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_reconcile/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_reconcile","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_reconcile")
        model.draftText = "Hi"

        await model.sendDraft()
        try await waitUntil { !model.isStreaming }
        // The authoritative summary replaces the optimistic row, not duplicates it.
        try await waitUntil { model.conversations.first?.lastMessage == "Persisted reply" }

        XCTAssertEqual(
            model.conversations.map(\.conversationID),
            ["web_conv_reconcile"],
            "The server summary must reconcile the optimistic row in place, with no duplicate."
        )
        XCTAssertEqual(model.conversations.first?.messageCount, 2)
    }

    func testFailedTurnStartRemovesOptimisticConversation() async throws {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                // The turn never starts: nothing is persisted server-side.
                return .json(#"{"detail":"temporary failure"}"#, statusCode: 500)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_failstart/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_failstart","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_failstart")
        model.draftText = "Hello"

        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertFalse(
            model.conversations.contains { $0.conversationID == "web_conv_failstart" },
            "A brand-new conversation whose turn failed to start must not linger as a phantom in the list."
        )
    }

    func testFailedTurnStartRestoresExistingConversationSummary() async throws {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                return .json(#"{"detail":"temporary failure"}"#, statusCode: 500)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(
                    #"{"conversations":[{"conversation_id":"web_conv_existfail","last_message":"Earlier message","last_timestamp":"2026-06-01T00:00:00Z","message_count":4}],"count":1}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_existfail")
        await model.refreshConversations()
        XCTAssertEqual(model.conversations.first?.lastMessage, "Earlier message")

        model.draftText = "Doomed message"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        let restored = model.conversations.first { $0.conversationID == "web_conv_existfail" }
        XCTAssertEqual(
            restored?.lastMessage,
            "Earlier message",
            "A failed start in an existing conversation must roll back the optimistic bump, not pin the never-persisted prompt."
        )
    }

    func testOptimisticRowShownDuringTurnThenReconciles() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_live","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_live/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("GET", "/api/v1/chat/conversations"):
                // The authoritative summary the server returns once the turn ends.
                return .json(
                    #"{"conversations":[{"conversation_id":"web_conv_live","last_message":"Live reply","last_timestamp":"2030-01-01T00:00:00Z","message_count":2}],"count":1}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_live/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_live","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_live")
        model.draftText = "Live message"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        // While the turn is in flight (pending), the optimistic row is shown.
        XCTAssertEqual(
            model.conversations.first { $0.conversationID == "web_conv_live" }?.lastMessage,
            "Live message"
        )

        // Once the turn settles, the optimistic mark is retired and the server
        // summary becomes authoritative.
        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\"}\n\n"
        )
        try await waitUntil { !model.isStreaming }
        try await waitUntil {
            model.conversations.first { $0.conversationID == "web_conv_live" }?.lastMessage
                == "Live reply"
        }
    }

    func testNewConversationStaysOnTopAboveExistingOnStaleRefresh() async throws {
        var streamedTurnID = "turn-newtop"
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_new2","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_new2/stream"):
                return .text(
                    """
                    event: turn_started
                    data: {"turn_id":"\(streamedTurnID)","seq":0}

                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"complete"}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                // An existing older conversation; the new one isn't persisted yet,
                // so the page omits it (stale relative to the optimistic insert).
                return .json(
                    #"{"conversations":[{"conversation_id":"web_conv_existing","last_message":"Earlier chat","last_timestamp":"2026-06-01T00:00:00Z","message_count":4}],"count":1}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_new2/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_new2","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_new2")
        // Seed the existing conversation so the account isn't empty.
        await model.refreshConversations()
        XCTAssertEqual(model.conversations.map(\.conversationID), ["web_conv_existing"])

        model.draftText = "Brand new chat"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(
            model.conversations.first?.conversationID,
            "web_conv_new2",
            "A brand-new conversation must stay most-recent-first above existing ones even when a stale refresh omits it."
        )
    }

    func testActivityStreamRefreshesConversationList() async throws {
        let conversationsRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text(
                    """
                    event: conversation_activity
                    data: {"conversation_id":"web_conv_pinged","reason":"turn_started"}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                // The first fetch (bootstrap) sees an empty list; the activity
                // stream's refresh then surfaces a conversation that changed
                // elsewhere — proving the list updates without a manual refresh.
                let count = conversationsRequests.increment()
                if count <= 1 {
                    return .json(#"{"conversations":[],"count":0}"#)
                }
                return .json(
                    #"{"conversations":[{"conversation_id":"web_conv_pinged","last_message":"Hi from elsewhere","last_timestamp":"2026-06-08T12:00:01Z","message_count":1}],"count":1}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: nil)
        await model.bootstrap()

        try await waitUntil {
            model.conversations.contains { $0.conversationID == "web_conv_pinged" }
        }
    }

    func testStopTurnCancelsServerTurnAndRendersCancelledResult() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let cancelPath = AtomicString()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stop/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stop")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                cancelPath.set(path)
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_stop")
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stop","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stop/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stop",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Response stopped.","timestamp":"2026-06-08T12:00:01Z"}
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

        let model = makeViewModel(conversationID: "web_conv_stop")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        await model.stopTurn()
        try await waitUntil { cancelRequests.value == 1 }

        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"cancelled\",\"error\":\"cancelled\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(cancelPath.value, "/api/v1/chat/turns/\(postedTurnID.value ?? "")/cancel")
        XCTAssertNil(model.errorMessage)
        XCTAssertNil(model.stopWarningMessage)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Response stopped."])
    }

    func testSuspendActiveSendCancelsTransportButPreservesTurnState() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_suspend","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_suspend/stream"):
                return .hangingStream(
                    """
                    event: turn_started
                    data: {"turn_id":"\(postedTurnID.value ?? "turn-suspend")","seq":0}

                    event: token
                    data: {"turn_id":"\(postedTurnID.value ?? "turn-suspend")","text":"partial","seq":1}

                    """,
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_suspend","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_suspend")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }
        // Wait for turn_started so the per-turn control state is populated (the
        // registered-turn id we then assert survives the suspend).
        try await waitUntil {
            model.turnControlStateForTesting.registeredTurnIDs.contains(postedTurnID.value ?? "")
        }

        let sessionBefore = try XCTUnwrap(model.activeTurnSessionForTesting)
        let controlBefore = model.turnControlStateForTesting
        let bubbleTextBefore = model.messages.last(where: { $0.role == .assistant })?.text

        model.suspendActiveSend()

        // Transport task is cancelled: streaming stops and no further stream
        // events are applied even though the hanging stream is finished below.
        XCTAssertFalse(model.isStreaming)

        let sessionAfter = try XCTUnwrap(model.activeTurnSessionForTesting)
        XCTAssertTrue(
            sessionBefore === sessionAfter,
            "suspendActiveSend must preserve the SAME ActiveTurnSession instance."
        )
        XCTAssertEqual(sessionAfter.turnID, postedTurnID.value)
        XCTAssertEqual(sessionAfter.prompt, "Hi")
        XCTAssertEqual(
            sessionAfter.lastAppliedSeq,
            sessionBefore.lastAppliedSeq,
            "The durable ack/resume cursor must survive the suspend."
        )

        XCTAssertEqual(
            model.turnControlStateForTesting,
            controlBefore,
            "The per-turn control dictionaries must be preserved, not discarded."
        )
        XCTAssertFalse(
            model.turnControlStateForTesting.registeredTurnIDs.isEmpty,
            "The registered-turn control state should be non-empty and intact."
        )

        let bubbleTextAfter = model.messages.last(where: { $0.role == .assistant })?.text
        XCTAssertEqual(
            bubbleTextAfter,
            bubbleTextBefore,
            "suspendActiveSend must not write 'Response stopped.' into the bubble."
        )
        XCTAssertNotEqual(bubbleTextAfter, "Response stopped.")

        XCTAssertEqual(
            cancelRequests.value,
            0,
            "suspendActiveSend must not fire a queued stop-cancel POST."
        )

        stream.finish()
        // Give any still-live transport tail a chance to (incorrectly) mutate
        // state before asserting the session is still intact.
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertTrue(model.activeTurnSessionForTesting === sessionAfter)
        XCTAssertEqual(cancelRequests.value, 0)
    }

    func testSteerDraftPostsToRunningTurnAndClearsAfterUserInputEcho() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let steerPrompt = AtomicString()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_steer","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_steer/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-steer")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                steerPrompt.set(payload["prompt"] as? String)
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_steer")
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_steer","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_steer",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"focus on tomorrow","timestamp":"2026-06-08T12:00:01Z"},
                        {"internal_id":3,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Final reply","timestamp":"2026-06-08T12:00:02Z"}
                      ],
                      "count":3,
                      "total_messages":3,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_steer")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "focus on tomorrow"
        await model.sendSteerDraft()
        XCTAssertEqual(steerPrompt.value, "focus on tomorrow")
        // The composer clears as soon as the steer is accepted, matching web.
        XCTAssertEqual(model.draftText, "")

        stream.finish(
            appending: """
            event: user_input
            data: {"turn_id":"\(postedTurnID.value ?? "")","content":"focus on tomorrow","seq":1}

            event: text
            data: {"turn_id":"\(postedTurnID.value ?? "")","content":"Final reply","seq":2}

            event: turn_ended
            data: {"turn_id":"\(postedTurnID.value ?? "")","status":"complete","seq":3}

            """
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.draftText, "")
        XCTAssertNil(model.steerErrorMessage)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "focus on tomorrow", "Final reply"])
    }

    func testSteerDraftIgnoresDuplicatePromptWhileSubmissionInFlight() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let steerRequests = AtomicCounter()
        let releaseSteer = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_duplicate_steer","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_duplicate_steer/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-duplicate-steer")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                _ = releaseSteer.wait(timeout: .now() + 5)
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_duplicate_steer","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_duplicate_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_duplicate_steer",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"avoid duplicate","timestamp":"2026-06-08T12:00:01Z"},
                        {"internal_id":3,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Final reply","timestamp":"2026-06-08T12:00:02Z"}
                      ],
                      "count":3,
                      "total_messages":3,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_duplicate_steer")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "avoid duplicate"
        let firstSteer = Task { await model.sendSteerDraft() }
        try await waitUntil { steerRequests.value == 1 }
        await model.sendSteerDraft()
        XCTAssertEqual(steerRequests.value, 1)

        releaseSteer.signal()
        await firstSteer.value
        stream.finish(
            appending: """
            event: user_input
            data: {"turn_id":"\(postedTurnID.value ?? "")","content":"avoid duplicate","seq":1}

            event: text
            data: {"turn_id":"\(postedTurnID.value ?? "")","content":"Final reply","seq":2}

            event: turn_ended
            data: {"turn_id":"\(postedTurnID.value ?? "")","status":"complete","seq":3}

            """
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(steerRequests.value, 1)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "avoid duplicate", "Final reply"])
    }

    func testPreRegistrationSteerDraftMovesToNormalDraftWhenStartFails() async throws {
        let turnStarts = AtomicCounter()
        let releaseStart = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                _ = releaseStart.wait(timeout: .now() + 5)
                return .json(#"{"detail":"temporary failure"}"#, statusCode: 500)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_start_failed_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_start_failed_steer",
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

        let model = makeViewModel(conversationID: "web_conv_start_failed_steer")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && turnStarts.value == 1 }

        model.draftText = "keep this steer"
        await model.sendSteerDraft()
        XCTAssertEqual(privateStringArray("inFlightSteers", in: model), ["keep this steer"])

        releaseStart.signal()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.draftText, "keep this steer")
        XCTAssertEqual(privateStringArray("inFlightSteers", in: model), [])
        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), [])
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
    }

    func testFailedSteerDraftMovesToNormalDraftWhenTurnEnds() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_failed_steer_draft","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_failed_steer_draft/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-failed-steer-draft")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(#"{"detail":"bad steer"}"#, statusCode: 400)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_failed_steer_draft/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_failed_steer_draft",
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

        let model = makeViewModel(conversationID: "web_conv_failed_steer_draft")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "retry as follow-up"
        await model.sendSteerDraft()
        XCTAssertEqual(steerRequests.value, 1)
        // The steer failed while the turn was current, so the text stays in the
        // composer (the unified composer keeps its text on error) for retry.
        XCTAssertEqual(model.draftText, "retry as follow-up")
        XCTAssertEqual(model.steerErrorMessage, "Could not steer the assistant. Please try again.")

        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }

        // The turn ended without consuming the failed steer, so it remains a
        // normal draft the user can resend.
        XCTAssertEqual(model.draftText, "retry as follow-up")
    }

    func testNormalSendRecoversServerRunningTurnAndSteersIt() async throws {
        let steerRequests = AtomicCounter()
        let turnStarts = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_lost_handle/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_lost_handle",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false,
                      "active_turns":[{
                        "turn_id":"turn-running-remotely",
                        "started_at":"2026-08-01T23:25:26Z",
                        "latest_seq":7,
                        "status":"running"
                      }]
                    }
                    """
                )
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                return .json(#"{"detail":"must not start a rival turn"}"#, statusCode: 500)
            case ("POST", "/api/v1/chat/turns/turn-running-remotely/steer"):
                steerRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["prompt"] as? String, "focus on the new request")
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_lost_handle")
                return .json(
                    #"{"turn_id":"turn-running-remotely","conversation_id":"web_conv_lost_handle","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_lost_handle/stream"):
                return .hangingStream("", controller: HangingStream())
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_lost_handle")
        model.draftText = "focus on the new request"

        await model.sendDraft()

        XCTAssertEqual(turnStarts.value, 0)
        XCTAssertEqual(steerRequests.value, 1)
        XCTAssertTrue(model.isStreaming)
        XCTAssertEqual(model.draftText, "")
    }

    func testRecoveredTurnFinishingBeforeSteerSendsPromptAsFollowUp() async throws {
        let messageLookups = AtomicCounter()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_recovered_finished/messages"):
                let lookup = messageLookups.increment()
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_recovered_finished",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false,
                      "active_turns":\(lookup == 1 ? "[{\"turn_id\":\"turn-finished-before-steer\",\"started_at\":\"2026-08-01T23:25:26Z\",\"latest_seq\":0,\"status\":\"running\"}]" : "[]")
                    }
                    """
                )
            case ("POST", "/api/v1/chat/turns/turn-finished-before-steer/steer"):
                steerRequests.increment()
                return .json(#"{"detail":"Turn is not running"}"#, statusCode: 409)
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["prompt"] as? String, "send as a follow-up")
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_recovered_finished","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_recovered_finished/stream"):
                return .hangingStream("", controller: HangingStream())
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_recovered_finished")
        model.draftText = "send as a follow-up"

        await model.sendDraft()
        try await waitUntil { turnStarts.value == 1 }

        XCTAssertEqual(steerRequests.value, 1)
        XCTAssertGreaterThanOrEqual(messageLookups.value, 2)
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
        XCTAssertEqual(model.draftText, "")
    }

    func testNormalSendPreflightPreservesDraftEditedDuringLookup() async throws {
        let lookup = HangingStream()
        let lookupRequests = AtomicCounter()
        let turnStarts = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations/web_conv_edit_during_lookup/messages"):
                lookupRequests.increment()
                return .hangingStream(
                    """
                    {
                      "conversation_id":"web_conv_edit_during_lookup",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false,
                      "active_turns":[]
                    }
                    """,
                    controller: lookup
                )
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                return .json(#"{"detail":"must not send stale text"}"#, statusCode: 500)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_edit_during_lookup")
        model.draftText = "old draft"
        let send = Task { await model.sendDraft() }
        try await waitUntil { lookupRequests.value == 1 }

        model.draftText = "newer draft"
        lookup.finish()
        await send.value

        XCTAssertEqual(turnStarts.value, 0)
        XCTAssertEqual(model.draftText, "newer draft")
        XCTAssertTrue(model.messages.isEmpty)
    }

    func testTurnStartConflictRecoversRunningTurnAndSteersIt() async throws {
        let messageLookups = AtomicCounter()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_recovery/messages"):
                let lookup = messageLookups.increment()
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_conflict_recovery",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false,
                      "active_turns":\(lookup == 1 ? "[]" : "[{\"turn_id\":\"turn-won-race\",\"started_at\":\"2026-08-01T23:25:26Z\",\"latest_seq\":0,\"status\":\"running\"}]")
                    }
                    """
                )
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                return .json(
                    """
                    {
                      "detail": {
                        "message": "This conversation already has a running turn.",
                        "active_turn_id": "turn-won-race"
                      }
                    }
                    """,
                    statusCode: 409
                )
            case ("POST", "/api/v1/chat/turns/turn-won-race/steer"):
                steerRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["prompt"] as? String, "use the newer constraints")
                return .json(
                    #"{"turn_id":"turn-won-race","conversation_id":"web_conv_conflict_recovery","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_recovery/stream"):
                return .hangingStream("", controller: HangingStream())
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_conflict_recovery")
        model.draftText = "use the newer constraints"

        await model.sendDraft()
        try await waitUntil { steerRequests.value == 1 }

        XCTAssertGreaterThanOrEqual(messageLookups.value, 1)
        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(steerRequests.value, 1)
        XCTAssertTrue(model.isStreaming)
        XCTAssertEqual(model.draftText, "")
        XCTAssertFalse(
            model.messages.contains { $0.role == .user && $0.text == "use the newer constraints" },
            "The rejected rival send must not remain as an optimistic user bubble."
        )
    }

    func testTurnStartConflictPreservesDraftTypedWhileRequestIsInFlight() async throws {
        let startResponse = HangingStream()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_edit/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_conflict_edit",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false,
                      "active_turns":[]
                    }
                    """
                )
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                return .hangingStream(
                    """
                    {
                      "detail": {
                        "message": "This conversation already has a running turn.",
                        "active_turn_id": "turn-won-edit-race"
                      }
                    }
                    """,
                    statusCode: 409,
                    controller: startResponse
                )
            case ("POST", "/api/v1/chat/turns/turn-won-edit-race/steer"):
                steerRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["prompt"] as? String, "original prompt")
                return .json(
                    #"{"turn_id":"turn-won-edit-race","conversation_id":"web_conv_conflict_edit","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_edit/stream"):
                return .hangingStream("", controller: HangingStream())
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_conflict_edit")
        model.draftText = "original prompt"
        await model.sendDraft()
        try await waitUntil { turnStarts.value == 1 }

        model.draftText = "typed while the send was pending"
        startResponse.finish()
        try await waitUntil { steerRequests.value == 1 }

        XCTAssertEqual(model.draftText, "typed while the send was pending")
    }

    func testTurnStartConflictSteerFailureRemovesOptimisticConversation() async throws {
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_steer_failure/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_conflict_steer_failure","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false,"active_turns":[]}"#
                )
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"detail":{"message":"running","active_turn_id":"turn-won-failure-race"}}"#,
                    statusCode: 409
                )
            case ("POST", "/api/v1/chat/turns/turn-won-failure-race/steer"):
                steerRequests.increment()
                return .json(#"{"detail":"steer rejected"}"#, statusCode: 400)
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_steer_failure/stream"):
                return .hangingStream("", controller: HangingStream())
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_conflict_steer_failure")
        model.draftText = "unsent prompt"

        await model.sendDraft()
        try await waitUntil {
            steerRequests.value == 1 && model.draftText == "unsent prompt"
        }

        XCTAssertEqual(model.draftText, "unsent prompt")
        XCTAssertFalse(
            model.conversations.contains { $0.conversationID == "web_conv_conflict_steer_failure" },
            "A prompt rejected as both a turn start and a steer must not leave a phantom conversation summary."
        )
    }

    func testTurnStartConflictTransfersSteerQueuedWhileRequestIsInFlight() async throws {
        let startResponse = HangingStream()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_queued_steer/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_conflict_queued_steer","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false,"active_turns":[]}"#
                )
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                return .hangingStream(
                    #"{"detail":{"message":"running","active_turn_id":"turn-won-steer-race"}}"#,
                    statusCode: 409,
                    controller: startResponse
                )
            case ("POST", "/api/v1/chat/turns/turn-won-steer-race/steer"):
                let requestNumber = steerRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(
                    payload["prompt"] as? String,
                    requestNumber == 1 ? "original prompt" : "queued steer"
                )
                return .json(
                    #"{"turn_id":"turn-won-steer-race","conversation_id":"web_conv_conflict_queued_steer","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_queued_steer/stream"):
                return .hangingStream("", controller: HangingStream())
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_conflict_queued_steer")
        model.draftText = "original prompt"
        await model.sendDraft()
        try await waitUntil { turnStarts.value == 1 }

        model.draftText = "queued steer"
        await model.sendSteerDraft()
        XCTAssertEqual(steerRequests.value, 0)

        startResponse.finish()
        try await waitUntil { steerRequests.value == 2 }

        XCTAssertEqual(model.draftText, "")
        XCTAssertEqual(privateStringArray("inFlightSteers", in: model), [])
    }

    func testTurnStartConflictTransfersQueuedStopAndDropsQueuedSteer() async throws {
        let startResponse = HangingStream()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_queued_stop/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_conflict_queued_stop","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false,"active_turns":[]}"#
                )
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                return .hangingStream(
                    #"{"detail":{"message":"running","active_turn_id":"turn-won-stop-race"}}"#,
                    statusCode: 409,
                    controller: startResponse
                )
            case ("POST", _) where path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(#"{"detail":"stop must win"}"#, statusCode: 500)
            case ("POST", "/api/v1/chat/turns/turn-won-stop-race/cancel"):
                cancelRequests.increment()
                return .json(
                    #"{"turn_id":"turn-won-stop-race","conversation_id":"web_conv_conflict_queued_stop","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_conflict_queued_stop/stream"):
                return .hangingStream("", controller: HangingStream())
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_conflict_queued_stop")
        model.draftText = "original prompt"
        await model.sendDraft()
        try await waitUntil { turnStarts.value == 1 }

        model.draftText = "queued steer"
        await model.sendSteerDraft()
        await model.stopTurn()
        startResponse.finish()
        try await waitUntil { cancelRequests.value == 1 }

        XCTAssertEqual(steerRequests.value, 0)
        XCTAssertEqual(model.draftText, "")
        XCTAssertEqual(privateStringArray("inFlightSteers", in: model), [])
        XCTAssertFalse(
            model.conversations.contains { $0.conversationID == "web_conv_conflict_queued_stop" },
            "A rejected rival send must not leave a phantom summary when Stop wins recovery."
        )
    }

    func testQueuedFollowUpPreflightPreservesNewerDraftAndRequeuesFollowUp() async throws {
        let firstStream = HangingStream()
        let queuedLookup = HangingStream()
        let postedTurnID = AtomicString()
        let messageLookups = AtomicCounter()
        let turnStarts = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_queued_followup_edit/messages"):
                let lookup = messageLookups.increment()
                let response =
                    #"{"conversation_id":"web_conv_queued_followup_edit","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false,"active_turns":[]}"#
                if lookup == 3 {
                    return .hangingStream(response, controller: queuedLookup)
                }
                return .json(response)
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_queued_followup_edit","first_seq":0}"#
                )
            case ("POST", _) where path.hasSuffix("/steer"):
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_queued_followup_edit","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_queued_followup_edit/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-queued-followup")\",\"seq\":0}\n\n",
                    controller: firstStream
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_queued_followup_edit")
        model.draftText = "initial prompt"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "queued follow-up"
        await model.sendSteerDraft()
        firstStream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\",\"seq\":1}\n\n"
        )
        try await waitUntil { messageLookups.value == 3 }

        model.draftText = "newer visible draft"
        queuedLookup.finish()
        try await waitUntil {
            self.privateStringArray("queuedFollowUpSteers", in: model).contains("queued follow-up")
        }

        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(model.draftText, "newer visible draft")
    }

    func testSteerWithNoRunningTurnSendsComposerAsNormalMessage() async throws {
        // The main composer doubles as the steer input. When no turn is running,
        // tapping the action with composer text simply sends it as a normal new
        // message — text and any attachments included — and clears the composer.
        let turnStarts = AtomicCounter()
        let sentPrompt = AtomicString()
        let sentAttachmentCount = AtomicString()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                sentPrompt.set(payload["prompt"] as? String)
                sentAttachmentCount.set(String((payload["attachments"] as? [Any])?.count ?? 0))
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_late_steer_followup","already_complete":true,"first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_late_steer_followup/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_late_steer_followup",
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

        let model = makeViewModel(conversationID: "web_conv_late_steer_followup")
        model.draftText = "late steer"
        model.draftAttachments = [makeAttachment(uploadState: .uploaded)]

        await model.sendSteerDraft()
        try await waitUntil { turnStarts.value == 1 }

        XCTAssertEqual(sentPrompt.value, "late steer")
        XCTAssertEqual(sentAttachmentCount.value, "1")
        XCTAssertEqual(model.draftText, "")
        XCTAssertEqual(model.draftAttachments, [])
    }

    func testSteerFallbackEmitsDegradedToSendBreadcrumb() async throws {
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_fallback/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_steer_fallback",
                      "messages":[],
                      "count":0,
                      "total_messages":0,
                      "has_more_before":false,
                      "has_more_after":false,
                      "active_turns":[]
                    }
                    """
                )
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_steer_fallback","already_complete":true,"first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-steer-fallback-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_steer_fallback",
            spoolDirectory: spoolDirectory
        )
        model.draftText = "send this next"

        await model.sendSteerDraft()
        let reports = try await waitForSpooledReports(
            component: "Chat.steerDegradedToSend",
            in: spoolDirectory
        )

        XCTAssertEqual(reports.first?.extraData?["reason"], "no_running_server_turn")
    }

    func testStopTurnRetriesTurnRegistrationRace() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop_retry","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_retry/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stop")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                let requestNumber = cancelRequests.increment()
                if requestNumber == 1 {
                    return .json(#"{"detail":"not registered yet"}"#, statusCode: 404)
                }
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stop_retry","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_retry/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stop_retry",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Response stopped.","timestamp":"2026-06-08T12:00:01Z"}
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

        let model = makeViewModel(conversationID: "web_conv_stop_retry")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        await model.stopTurn()
        try await waitUntil { cancelRequests.value == 2 }

        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"cancelled\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertNil(model.stopWarningMessage)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Response stopped."])
    }

    func testStopWarningDoesNotLeakAfterLeavingTurn() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let cancelRequests = AtomicCounter()
        let releaseCancel = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop_warning_scope","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_warning_scope/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stop-warning-scope")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                _ = releaseCancel.wait(timeout: .now() + 5)
                return .json(#"{"detail":"not cancellable"}"#, statusCode: 400)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_warning_scope/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stop_warning_scope",
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

        let model = makeViewModel(conversationID: "web_conv_stop_warning_scope")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        let stopTask = Task { await model.stopTurn() }
        try await waitUntil { cancelRequests.value == 1 }
        model.startNewConversation()
        releaseCancel.signal()
        await stopTask.value

        XCTAssertNil(model.stopWarningMessage)
        stream.finish()
    }

    func testFailedStopWarningSurvivesStreamRecovery() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop_warning_recovery","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_warning_recovery/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stop-warning-recovery")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                let requestNumber = cancelRequests.increment()
                if requestNumber < 5 {
                    return .json(#"{"detail":"not registered yet"}"#, statusCode: 404)
                }
                return .json(#"{"detail":"not cancellable"}"#, statusCode: 400)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_warning_recovery/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stop_warning_recovery",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Recovered reply","timestamp":"2026-06-08T12:00:01Z"}
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

        let model = makeViewModel(
            conversationID: "web_conv_stop_warning_recovery",
            maxConsecutiveStreamResumes: 0
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        let stopTask = Task { await model.stopTurn() }
        try await waitUntil { cancelRequests.value == 1 }
        stream.finish(
            appending: """
            event: stream_dropped
            data: {}

            """
        )
        try await waitUntil { !model.isStreaming }
        await stopTask.value

        XCTAssertEqual(
            model.stopWarningMessage,
            "Stop could not be confirmed. Pending approvals from this turn may still be active."
        )
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Recovered reply"])
        XCTAssertEqual(cancelRequests.value, 5)
    }

    func testStopTurnBeforeRegistrationCancelsAfterStartCompletes() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let startRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        let releaseStart = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                startRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                _ = releaseStart.wait(timeout: .now() + 5)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop_pending","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_pending/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stop-pending")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_stop_pending")
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stop_pending","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_pending/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stop_pending",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Response stopped.","timestamp":"2026-06-08T12:00:01Z"}
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

        let model = makeViewModel(conversationID: "web_conv_stop_pending")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && startRequests.value == 1 }

        await model.stopTurn()
        XCTAssertEqual(cancelRequests.value, 0)
        releaseStart.signal()
        try await waitUntil { cancelRequests.value == 1 }

        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"cancelled\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertNil(model.stopWarningMessage)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Response stopped."])
    }

    func testStopTurnBeforeRegistrationStillCancelsAfterConversationSwitch() async throws {
        let postedTurnID = AtomicString()
        let startRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        let releaseStart = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                startRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                _ = releaseStart.wait(timeout: .now() + 5)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop_pending_switch","first_seq":0}"#
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_stop_pending_switch")
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stop_pending_switch","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_pending_other/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stop_pending_other",
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
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    return .text("")
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_stop_pending_switch")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && startRequests.value == 1 }

        await model.stopTurn()
        XCTAssertEqual(cancelRequests.value, 0)

        let switchTask = Task {
            await model.selectConversation("web_conv_stop_pending_other")
        }
        try await waitUntil { model.conversationID == "web_conv_stop_pending_other" }
        releaseStart.signal()
        try await waitUntil { cancelRequests.value == 1 }
        await switchTask.value

        XCTAssertEqual(model.messages.map(\.text), ["Other thread"])
    }

    func testStopTurnBeforeRegistrationCancelsWhenStartResponseFails() async throws {
        let postedTurnID = AtomicString()
        let startRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        let releaseStart = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                startRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                postedTurnID.set(try XCTUnwrap(payload["turn_id"] as? String))
                _ = releaseStart.wait(timeout: .now() + 5)
                return .json(#"{"detail":"lost response"}"#, statusCode: 500)
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_stop_start_failure")
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stop_start_failure","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_stop_start_failure")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && startRequests.value == 1 }

        await model.stopTurn()
        releaseStart.signal()
        try await waitUntil { cancelRequests.value == 1 }
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.draftText, "")
    }

    func testStopTurnBeforeRegistrationWarnsWhenCancelCannotBeSecured() async throws {
        let postedTurnID = AtomicString()
        let startRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        let releaseStart = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                startRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                postedTurnID.set(try XCTUnwrap(payload["turn_id"] as? String))
                _ = releaseStart.wait(timeout: .now() + 5)
                return .json(#"{"detail":"lost response"}"#, statusCode: 500)
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                // A non-retryable failure: the queued stop cannot be secured.
                return .json(#"{"detail":"forbidden"}"#, statusCode: 403)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_stop_unsecured")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && startRequests.value == 1 }

        await model.stopTurn()
        releaseStart.signal()
        try await waitUntil { cancelRequests.value == 1 }
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.stopWarningMessage, "Stop could not be confirmed. Pending approvals from this turn may still be active.")
    }

    func testStopQueuedBeforeRegistrationFailureSurfacesInlineNotModal() async throws {
        // A stop queued before registration, applied after startTurn succeeds,
        // whose retried cancel throws a non-retryable transport error surfaces
        // INLINE on the stop-warning line (§4.5), never a modal: a
        // `Chat.inlineErrorPresented` breadcrumb tagged operation=stop_turn, and
        // NO `Chat.alertPresented`.
        let postedTurnID = AtomicString()
        let startRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        let releaseStart = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                startRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                _ = releaseStart.wait(timeout: .now() + 5)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop_thrown","first_seq":0}"#
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                // A non-retryable transport error propagates out of the retry
                // loop as a throw, driving the queued-stop failure path.
                throw URLError(.cannotParseResponse)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-alert-stop-thrown-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_stop_thrown",
            spoolDirectory: spoolDirectory
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && startRequests.value == 1 }

        await model.stopTurn()
        releaseStart.signal()
        try await waitUntil { cancelRequests.value >= 1 }
        try await waitUntil { !model.isStreaming }

        let reports = try await waitForSpooledReports(
            component: "Chat.inlineErrorPresented",
            in: spoolDirectory
        )
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports.first?.extraData?["operation"], "stop_turn")
        XCTAssertEqual(reports.first?.extraData?["reason"], "action_failed")
        XCTAssertNotNil(model.stopWarningMessage, "the stop failure surfaces on the stop-warning line")
        XCTAssertNil(model.errorMessage, "a stop failure must not modal")
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" }
        )
    }

    func testSteerBeforeRegistrationPostsAfterStartCompletes() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let startRequests = AtomicCounter()
        let steerRequests = AtomicCounter()
        let releaseStart = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                startRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                _ = releaseStart.wait(timeout: .now() + 5)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_steer_pending","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_pending/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-steer-pending")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["conversation_id"] as? String, "web_conv_steer_pending")
                XCTAssertEqual(payload["prompt"] as? String, "focus on tomorrow")
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_steer_pending","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_pending/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_steer_pending",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"focus on tomorrow","timestamp":"2026-06-08T12:00:01Z"},
                        {"internal_id":3,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Final reply","timestamp":"2026-06-08T12:00:02Z"}
                      ],
                      "count":3,
                      "total_messages":3,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_steer_pending")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && startRequests.value == 1 }

        model.draftText = "focus on tomorrow"
        await model.sendSteerDraft()
        XCTAssertEqual(steerRequests.value, 0)
        releaseStart.signal()
        try await waitUntil { steerRequests.value == 1 }

        stream.finish(
            appending: """
            event: user_input
            data: {"turn_id":"\(postedTurnID.value ?? "")","content":"focus on tomorrow","seq":1}

            event: text
            data: {"turn_id":"\(postedTurnID.value ?? "")","content":"Final reply","seq":2}

            event: turn_ended
            data: {"turn_id":"\(postedTurnID.value ?? "")","status":"complete","seq":3}

            """
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertNil(model.steerErrorMessage)
        XCTAssertEqual(model.draftText, "")
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "focus on tomorrow", "Final reply"])
    }

    func testStopBeforeRegistrationDropsPendingSteer() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let startRequests = AtomicCounter()
        let steerRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        let releaseStart = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                startRequests.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                _ = releaseStart.wait(timeout: .now() + 5)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop_steer_pending","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_steer_pending/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stop-steer-pending")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stop_steer_pending","status":"cancelling","already_complete":false}"#
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(#"{"detail":"unexpected steer"}"#, statusCode: 500)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_steer_pending/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stop_steer_pending",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Response stopped.","timestamp":"2026-06-08T12:00:01Z"}
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

        let model = makeViewModel(conversationID: "web_conv_stop_steer_pending")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && startRequests.value == 1 }

        model.draftText = "focus on tomorrow"
        await model.sendSteerDraft()
        await model.stopTurn()
        releaseStart.signal()
        try await waitUntil { cancelRequests.value == 1 }

        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"cancelled\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(steerRequests.value, 0)
        XCTAssertEqual(model.draftText, "")
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Response stopped."])
    }

    func testSteerDraftRetriesTurnRegistrationRace() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let steerRequests = AtomicCounter()
        let turnStarts = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_steer_retry","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_retry/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-steer")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                let requestNumber = steerRequests.increment()
                if requestNumber == 1 {
                    return .json(#"{"detail":"not registered yet"}"#, statusCode: 404)
                }
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["prompt"] as? String, "focus on tomorrow")
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_steer_retry","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_retry/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_steer_retry",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"focus on tomorrow","timestamp":"2026-06-08T12:00:01Z"},
                        {"internal_id":3,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Final reply","timestamp":"2026-06-08T12:00:02Z"}
                      ],
                      "count":3,
                      "total_messages":3,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_steer_retry")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "focus on tomorrow"
        await model.sendSteerDraft()
        try await waitUntil { steerRequests.value == 2 }

        stream.finish(
            appending: """
            event: user_input
            data: {"turn_id":"\(postedTurnID.value ?? "")","content":"focus on tomorrow","seq":1}

            event: turn_ended
            data: {"turn_id":"\(postedTurnID.value ?? "")","status":"complete","seq":2}

            """
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "focus on tomorrow", "Final reply"])
    }

    func testSteerResultAfterTurnResetDoesNotLeakIntoNextTurn() async throws {
        let firstStream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let streamConnects = AtomicCounter()
        let steerRequests = AtomicCounter()
        let releaseSteer = DispatchSemaphore(value: 0)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stale_steer","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stale_steer/stream"):
                if streamConnects.increment() == 1 {
                    return .hangingStream(
                        "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stale")\",\"seq\":0}\n\n",
                        controller: firstStream
                    )
                }
                return .text(
                    "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\",\"seq\":1}\n\n"
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                _ = releaseSteer.wait(timeout: .now() + 5)
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stale_steer","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stale_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stale_steer",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Next","timestamp":"2026-06-08T12:00:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_stale_steer")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "stale steer"
        let steerTask = Task { await model.sendSteerDraft() }
        try await waitUntil { steerRequests.value == 1 }
        model.cancelStream()
        releaseSteer.signal()
        await steerTask.value

        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), [])
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
        XCTAssertEqual(turnStarts.value, 1)
    }


    func testAcceptedSteerIsClearedWhenStreamFallsBackToHistoryRecovery() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_steer_recovery","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_recovery/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-recovery")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_steer_recovery","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_recovery/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_steer_recovery",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Recovered reply","timestamp":"2026-06-08T12:00:01Z"}
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

        let model = makeViewModel(
            conversationID: "web_conv_steer_recovery",
            maxConsecutiveStreamResumes: 0
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "do not resend"
        await model.sendSteerDraft()
        try await waitUntil { steerRequests.value == 1 }
        stream.finish()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), [])
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(model.draftText, "")
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Recovered reply"])
    }

    func testFailedInFlightSteerRecoversDraftAfterStreamRecovery() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_steer_failure_recovery","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_failure_recovery/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-steer-failure-recovery")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                let requestNumber = steerRequests.increment()
                if requestNumber < 5 {
                    return .json(#"{"detail":"transient"}"#, statusCode: 500)
                }
                return .json(#"{"detail":"bad steer"}"#, statusCode: 400)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_failure_recovery/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_steer_failure_recovery",
                      "messages":[
                        {"internal_id":1,"turn_id":"\(postedTurnID.value ?? "")","role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"\(postedTurnID.value ?? "")","role":"assistant","content":"Recovered reply","timestamp":"2026-06-08T12:00:01Z"}
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

        let model = makeViewModel(
            conversationID: "web_conv_steer_failure_recovery",
            maxConsecutiveStreamResumes: 0
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "retry after recovery"
        let steerTask = Task { await model.sendSteerDraft() }
        try await waitUntil { steerRequests.value == 1 }
        XCTAssertEqual(privateStringArray("inFlightSteers", in: model), ["retry after recovery"])

        stream.finish(
            appending: """
            event: stream_dropped
            data: {}

            """
        )
        try await waitUntil { !model.isStreaming }
        // The composer doubles as the steer input, so the in-flight steer text
        // stays put while the stream recovers.
        XCTAssertEqual(model.draftText, "retry after recovery")

        await steerTask.value

        // The steer ultimately fails after the turn ended; its text remains in
        // the composer as a normal draft the user can resend.
        XCTAssertEqual(privateStringArray("inFlightSteers", in: model), [])
        XCTAssertEqual(model.draftText, "retry after recovery")
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Recovered reply"])
        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(steerRequests.value, 5)
    }

    func testAcceptedSteerEchoDoesNotResendAsFollowUp() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_steer_echo_race","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_echo_race/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-echo-race")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_steer_echo_race","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_echo_race/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_steer_echo_race",
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

        let model = makeViewModel(conversationID: "web_conv_steer_echo_race")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "already applied"
        let steerTask = Task { await model.sendSteerDraft() }
        try await waitUntil { steerRequests.value == 1 }
        stream.finish(
            appending: """
            event: user_input
            data: {"turn_id":"\(postedTurnID.value ?? "")","content":"already applied","seq":1}

            event: turn_ended
            data: {"turn_id":"\(postedTurnID.value ?? "")","status":"complete","seq":2}

            """
        )
        try await waitUntil { !model.isStreaming }
        await steerTask.value

        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), [])
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
        XCTAssertEqual(turnStarts.value, 1)
    }

    func testFinishedSteerQueuesFollowUpUntilTurnCompletes() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_steer_finished_race","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_finished_race/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-finished-race")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(#"{"detail":"Turn is not running"}"#, statusCode: 409)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_steer_finished_race/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_steer_finished_race",
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

        let model = makeViewModel(conversationID: "web_conv_steer_finished_race")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "send after finish"
        await model.sendSteerDraft()
        try await waitUntil { steerRequests.value == 1 }

        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), [])
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), ["send after finish"])
        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(model.draftText, "")
        stream.finish()
    }

    func testAmbiguousSteerRetryThenFinishedDoesNotSendFollowUp() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_ambiguous_steer_finished","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_ambiguous_steer_finished/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-ambiguous-finished")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                let requestNumber = steerRequests.increment()
                if requestNumber == 1 {
                    return .json(#"{"detail":"try again"}"#, statusCode: 500)
                }
                return .json(#"{"detail":"Turn is not running"}"#, statusCode: 409)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_ambiguous_steer_finished/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_ambiguous_steer_finished",
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

        let model = makeViewModel(conversationID: "web_conv_ambiguous_steer_finished")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "send after finish"
        let steerTask = Task { await model.sendSteerDraft() }
        try await waitUntil { steerRequests.value == 1 }
        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }

        await steerTask.value
        XCTAssertEqual(steerRequests.value, 2)
        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(model.draftText, "send after finish")
        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), [])
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
    }

    func testAcceptedSteerAfterCancelledTurnDoesNotStartFollowUp() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let streamConnects = AtomicCounter()
        let steerRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_cancelled_late_steer","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_cancelled_late_steer/stream"):
                if streamConnects.increment() == 1 {
                    return .hangingStream(
                        "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-cancelled")\",\"seq\":0}\n\n",
                        controller: stream
                    )
                }
                return .text(
                    "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\",\"seq\":1}\n\n"
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                let requestNumber = steerRequests.increment()
                if requestNumber == 1 {
                    return .json(#"{"detail":"try again"}"#, statusCode: 500)
                }
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_cancelled_late_steer","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_cancelled_late_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_cancelled_late_steer",
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

        let model = makeViewModel(conversationID: "web_conv_cancelled_late_steer")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "do not resend"
        let steerTask = Task { await model.sendSteerDraft() }
        try await waitUntil { steerRequests.value == 1 }
        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"cancelled\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }

        await steerTask.value

        XCTAssertEqual(steerRequests.value, 2)
        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(privateStringArray("inFlightSteers", in: model), [])
        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), [])
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
        XCTAssertEqual(model.draftText, "")
    }

    func testQueuedFinishedSteerIsClearedWhenTurnIsCancelled() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_cancel_finished_steer","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_cancel_finished_steer/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-cancel-finished-steer")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(#"{"detail":"Turn is not running"}"#, statusCode: 409)
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_cancel_finished_steer","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_cancel_finished_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_cancel_finished_steer",
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

        let model = makeViewModel(conversationID: "web_conv_cancel_finished_steer")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "do not send after stop"
        await model.sendSteerDraft()
        try await waitUntil { steerRequests.value == 1 }
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), ["do not send after stop"])

        await model.stopTurn()
        try await waitUntil { cancelRequests.value == 1 }
        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"cancelled\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }
        await Task.yield()
        await Task.yield()

        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
    }

    func testAcceptedSteerWaitingForEchoIsNotPreservedAfterStop() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let steerRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stop_accepted_steer","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_accepted_steer/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stop-accepted-steer")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stop_accepted_steer","accepted":true}"#
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stop_accepted_steer","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stop_accepted_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stop_accepted_steer",
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

        let model = makeViewModel(conversationID: "web_conv_stop_accepted_steer")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "do not preserve after stop"
        await model.sendSteerDraft()
        try await waitUntil { steerRequests.value == 1 }
        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), ["do not preserve after stop"])

        await model.stopTurn()
        try await waitUntil { cancelRequests.value == 1 }
        stream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"cancelled\",\"seq\":1}\n\n"
        )
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.draftText, "")
        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), [])
    }

    func testQueuedFinishedSteerIsClearedWhenStoppedStreamDrops() async throws {
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let steerRequests = AtomicCounter()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_stopped_stream_drop","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_stopped_stream_drop/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-stopped-stream-drop")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                steerRequests.increment()
                return .json(#"{"detail":"Turn is not running"}"#, statusCode: 409)
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_stopped_stream_drop","status":"cancelling","already_complete":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_stopped_stream_drop/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_stopped_stream_drop",
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

        let model = makeViewModel(
            conversationID: "web_conv_stopped_stream_drop",
            maxConsecutiveStreamResumes: 0
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "do not send after dropped stop"
        await model.sendSteerDraft()
        try await waitUntil { steerRequests.value == 1 }
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), ["do not send after dropped stop"])

        await model.stopTurn()
        try await waitUntil { cancelRequests.value == 1 }
        stream.finish()
        try await waitUntil { !model.isStreaming }
        await Task.yield()
        await Task.yield()

        XCTAssertEqual(turnStarts.value, 1)
        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), [])
    }

    func testRecoveredFollowUpQueuePreservesRemainingSteers() async throws {
        let firstStream = HangingStream()
        let secondStream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let streamConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let requestNumber = turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                if requestNumber == 2 {
                    XCTAssertEqual(payload["prompt"] as? String, "first steer")
                }
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_multiple_recovered_steers","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_multiple_recovered_steers/stream"):
                if streamConnects.increment() == 1 {
                    return .hangingStream(
                        "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-recover-first")\",\"seq\":0}\n\n",
                        controller: firstStream
                    )
                }
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-recover-second")\",\"seq\":0}\n\n",
                    controller: secondStream
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_multiple_recovered_steers","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_multiple_recovered_steers/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_multiple_recovered_steers",
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

        let model = makeViewModel(conversationID: "web_conv_multiple_recovered_steers")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "first steer"
        await model.sendSteerDraft()
        model.draftText = "second steer"
        await model.sendSteerDraft()
        XCTAssertEqual(privateStringArray("awaitingEchoSteers", in: model), ["first steer", "second steer"])

        firstStream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\",\"seq\":1}\n\n"
        )
        try await waitUntil { turnStarts.value == 2 }

        XCTAssertEqual(privateStringArray("queuedFollowUpSteers", in: model), ["second steer"])
        model.cancelStream()
        secondStream.finish()
    }

    func testRecoveredUnconsumedSteerClearsDraftWhenSentAsFollowUp() async throws {
        let firstStream = HangingStream()
        let postedTurnID = AtomicString()
        let turnStarts = AtomicCounter()
        let streamConnects = AtomicCounter()
        let followUpPrompt = AtomicString()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let requestNumber = turnStarts.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                if requestNumber == 2 {
                    followUpPrompt.set(payload["prompt"] as? String)
                    XCTAssertEqual((payload["attachments"] as? [Any])?.count, 0)
                }
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_recover_steer","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_recover_steer/stream"):
                if streamConnects.increment() == 1 {
                    return .hangingStream(
                        "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-recover")\",\"seq\":0}\n\n",
                        controller: firstStream
                    )
                }
                return .text(
                    "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\",\"seq\":1}\n\n"
                )
            case ("POST", _)
                where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/steer"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                XCTAssertEqual(payload["prompt"] as? String, "use the newer plan")
                return .json(
                    #"{"turn_id":"\#(postedTurnID.value ?? "")","conversation_id":"web_conv_recover_steer","accepted":true}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_recover_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_recover_steer",
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

        let model = makeViewModel(conversationID: "web_conv_recover_steer")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        model.draftText = "use the newer plan"
        await model.sendSteerDraft()
        let preservedAttachment = makeAttachment(uploadState: .uploaded)
        model.draftText = "visible draft"
        model.draftAttachments = [preservedAttachment]
        firstStream.finish(
            appending:
                "event: turn_ended\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"status\":\"complete\",\"seq\":1}\n\n"
        )
        try await waitUntil { turnStarts.value == 2 }

        XCTAssertEqual(followUpPrompt.value, "use the newer plan")
        // The fresh edit typed while the steer was in flight is preserved: the
        // recovered follow-up is sent without clobbering the visible draft.
        XCTAssertEqual(model.draftText, "visible draft")
        XCTAssertEqual(model.draftAttachments, [preservedAttachment])
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

    func testActiveTurnSessionSurvivesTransportDropRetainingPayloadAndCursor() async throws {
        // The durable ActiveTurnSession must OUTLIVE its transport task: after a
        // mid-turn drop the send resumes from the SAME session instance (not a
        // reconstructed one), and that session still carries its original payload
        // and the cursor it had applied before the drop. This is the M2
        // reattachment precondition — assert it directly, not just observationally.
        let streamRequests = AtomicCounter()
        var streamedTurnID = "turn-survive"
        var resumeFromSeq: String?
        let leg1 = HangingStream()
        let leg2 = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_survive","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_survive/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_survive","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_survive/stream" {
                    if streamRequests.increment() == 1 {
                        // First leg: apply a token at seq 2, then hold the socket
                        // open until the test finishes it (a clean mid-turn EOF).
                        return .hangingStream(
                            "event: text\ndata: {\"turn_id\":\"\(streamedTurnID)\",\"content\":\"Partial\",\"seq\":2}\n\n",
                            controller: leg1
                        )
                    }
                    // Resume leg: hold open so the turn stays in flight while the
                    // test inspects the surviving session.
                    resumeFromSeq = Self.queryItems(from: request)["from_seq"]
                    return .hangingStream("", controller: leg2)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_survive",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001
        )
        defer {
            leg1.finish()
            leg2.finish()
            model.cancelStream()
        }
        model.draftText = "Ask something"
        await model.sendDraft()

        // Capture the session while the first transport leg is live.
        try await waitUntil { model.messages.last?.text == "Partial" }
        let session = try XCTUnwrap(model.activeTurnSessionForTesting)
        XCTAssertEqual(session.prompt, "Ask something")
        XCTAssertEqual(session.conversationID, "web_conv_survive")

        // Sever the first leg: the send loop resubscribes on the SAME session.
        leg1.finish()
        try await waitUntil { streamRequests.value >= 2 }

        // The session survived the transport task's drop: same instance, same
        // payload, and the cursor it applied before the drop (seq 2).
        let resumed = try XCTUnwrap(model.activeTurnSessionForTesting)
        XCTAssertTrue(resumed === session)
        XCTAssertEqual(resumed.prompt, "Ask something")
        XCTAssertEqual(resumed.lastAppliedSeq, 2)
        // The resume resumes from just past the applied seq (server replays
        // seq >= from_seq), matching the send cursor now held by the session.
        XCTAssertEqual(resumeFromSeq, "3")

        leg2.finish()
        model.cancelStream()
    }

    func testDiscoveredActiveTurnTailAttachesAndReconcilesAtTurnEnded() async throws {
        // A turn the server reports in /messages active_turns for the selected
        // conversation, for which this client holds NO local session (e.g. started
        // on another device), must render progressively from the follow-stream
        // tail — a placeholder that streams the remaining tokens — and then be
        // reconciled by the canonical history replacement at turn_ended. No
        // mid-turn prefix reconstruction: the prefix arrives only via history.
        let streamConnects = AtomicCounter()
        let turnEnded = AtomicCounter()
        var tailFromSeq: String?
        let leg1 = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/conversations/web_conv_discover/messages") {
                // While the turn runs, history holds only its earlier (persisted)
                // rows and reports the running turn in active_turns — the reply's
                // prefix is NOT included. Only once the turn has ended does the
                // canonical reply appear, supplying the missed prefix + tail.
                if turnEnded.value == 0 {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_discover",
                          "messages":[{"internal_id":1,"role":"user","content":"Kick off","timestamp":"2026-06-08T12:00:00Z"}],
                          "count":1,"total_messages":1,"has_more_before":false,"has_more_after":false,
                          "active_turns":[{"turn_id":"turn-remote","started_at":"2026-06-08T12:00:01Z","latest_seq":7,"status":"running"}]
                        }
                        """
                    )
                }
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_discover",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Kick off","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"role":"assistant","content":"Full remote reply","timestamp":"2026-06-08T12:00:05Z","turn_id":"turn-remote"}
                      ],
                      "count":2,"total_messages":2,"has_more_before":false,"has_more_after":false
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/conversations/web_conv_discover/stream") {
                let n = streamConnects.increment()
                if n == 1 {
                    // Tail-only attach: the discovered turn has no local cursor, so
                    // the follow stream subscribes from the head (-1) and streams a
                    // tail token live, then holds open (the turn is still running).
                    tailFromSeq = Self.queryItems(from: request)["from_seq"]
                    return .hangingStream(
                        "event: text\ndata: {\"turn_id\":\"turn-remote\",\"content\":\" tail\",\"seq\":8}\n\n",
                        controller: leg1
                    )
                }
                // The reconnect after the first leg is finished delivers turn_ended.
                turnEnded.increment()
                return .text(
                    """
                    event: turn_ended
                    data: {"turn_id":"turn-remote","status":"complete","seq":9}

                    """
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let model = makeViewModel(
            conversationID: nil,
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model.selectConversation("web_conv_discover")

        // The discovered turn renders progressively: its tail token appears in a
        // placeholder even though no local send is driving it.
        try await waitUntil { model.messages.contains { $0.text == " tail" } }
        XCTAssertEqual(tailFromSeq, "-1")

        // End the first leg; the reconnect delivers turn_ended, which triggers the
        // canonical history replacement supplying the full reply (prefix + tail)
        // the tail-only attach never reconstructed.
        leg1.finish()
        try await waitUntil { model.messages.contains { $0.text == "Full remote reply" } }
        XCTAssertFalse(model.messages.contains { $0.text == " tail" })
        XCTAssertEqual(model.messages.map(\.text), ["Kick off", "Full remote reply"])
        XCTAssertNil(model.errorMessage)
    }

    func testReconnectCatchUpDiscoversActiveTurnBeforeAnyToken() async throws {
        // The incremental catch-up path (mergeNewMessages / fetchMessages) — the
        // PRIMARY reconnect / 410 flow — must consume each page's active_turns and
        // attach a discovered running turn, rendering its progressive placeholder
        // before any token arrives. Previously only the full loadMessages path did
        // this, so a turn discovered during a reconnect showed nothing until a
        // token landed.
        let messagesFetches = AtomicCounter()
        let stream = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/conversations/web_conv_catchup/messages") {
                // The FULL load (no `after`) reports no active_turns, so any
                // discovered placeholder can only come from the incremental
                // catch-up page below — the path under test.
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_catchup",
                          "messages":[{"internal_id":1,"role":"user","content":"Kick off","timestamp":"2026-06-08T12:00:00Z"}],
                          "count":1,"total_messages":1,"has_more_before":false,"has_more_after":false
                        }
                        """
                    )
                }
                // The incremental page (the reconnect catch-up) reports the running
                // turn in active_turns with an empty message delta, so the only way
                // a placeholder can appear is by consuming active_turns here.
                messagesFetches.increment()
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_catchup",
                      "messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false,
                      "active_turns":[{"turn_id":"turn-remote","started_at":"2026-06-08T12:00:01Z","latest_seq":7,"status":"running"}]
                    }
                    """
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/conversations/web_conv_catchup/stream") {
                // The follow stream connects (which drives the catch-up merge) but
                // delivers NO token for the running turn — so the placeholder must
                // originate from the active_turns discovery, not a streamed token.
                return .hangingStream("", controller: stream)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: nil,
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_catchup")

        // The catch-up merge ran and surfaced the discovered turn as a progressive
        // placeholder even though no token has arrived.
        try await waitUntil {
            model?.messages.contains { $0.id == "local_follow_turn-remote" && $0.status == .running } == true
        }
        XCTAssertGreaterThanOrEqual(messagesFetches.value, 1)

        stream.finish()
        model = nil
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

    func testSendFatalSubscribeErrorSurfacesInlineNotModal() async throws {
        // Recovery must not paper over a genuinely fatal (non-auth) subscribe
        // failure: a 422 on the subscribe GET surfaces at the point of action (§4.5)
        // — the bubble is marked failed with a retry affordance — rather than
        // silently reloading history OR raising a modal. (A 401/403 is NOT fatal
        // here; it routes to the auth flow — see testSubscription401RoutesToAuthFlow.)
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-fatal","conversation_id":"web_conv_fatal","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    return .json(#"{"detail":"unprocessable"}"#, statusCode: 422)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_fatal")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // The failure surfaces inline on the bubble, never as a modal.
        XCTAssertNil(model.errorMessage, "a fatal send subscribe failure must not modal")
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed)
        XCTAssertNotNil(
            model.failedSend(for: assistant),
            "a fatal send failure records a retry affordance resolvable on the bubble"
        )
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

    func testSendStartTurnFailureSurfacesInlineNotModal() async throws {
        // If the turn never starts (POST /turns fails), there is no durable turn
        // to recover, so the error surfaces inline at the point of action (§4.5) —
        // never a modal — rather than silently reloading an empty history. The
        // user's prompt is preserved in the thread with a retry affordance.
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
        XCTAssertNil(model.errorMessage, "a failed start must not raise the modal")
        XCTAssertEqual(model.messages.first?.text, "Hi")
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed)
        XCTAssertNotNil(model.failedSend(for: assistant))
    }

    func testTurnStart401And403UseCentralSignInRequiredPath() async {
        for statusCode in [401, 403] {
            resetStoredAuth()
            KeychainHelper.save(key: "fa_api_token", string: "rejected-turn-token")
            UserDefaults.standard.set(
                ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
                forKey: "fa_token_expiry"
            )
            let conversationID = "web_conv_turn_auth_\(statusCode)"
            let postRequests = AtomicCounter()
            ChatMockBackendURLProtocol.respond { request in
                let method = request.httpMethod ?? "GET"
                let path = request.url?.path ?? ""
                if method == "POST", path == "/api/v1/chat/turns" {
                    _ = postRequests.increment()
                    return .json(#"{"detail":"sign in again"}"#, statusCode: statusCode)
                }
                if method == "GET", path == "/api/v1/profiles" {
                    return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
                }
                if method == "GET", path == "/api/v1/chat/conversations" {
                    return .json(#"{"conversations":[],"count":0}"#)
                }
                if method == "GET", path.hasSuffix("/messages") {
                    return .json(
                        #"{"conversation_id":"\#(conversationID)","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                if method == "GET", path == "/api/v1/chat/confirmations/pending" {
                    return .json(#"{"confirmations":[]}"#)
                }
                if method == "GET", path.hasSuffix("/stream") {
                    return .text("")
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            let authManager = AuthManager()
            authManager.serverURL = serverURL
            let model = track(ChatViewModel(
                authManager: authManager,
                conversationID: conversationID,
                errorReporter: ErrorReporter(spoolDirectory: nil),
                pathMonitor: StubPathMonitor(isSatisfied: true)
            ))
            await model.bootstrap()
            model.draftText = "Hello"

            await model.sendDraft()
            do {
                // Wait for the presentation to settle, not just the auth flag: the
                // coordinator adopts `.authRequired` via the auth-state observer a hop
                // after `authManager.authRequired` flips, so asserting `syncPresentation`
                // the instant the flag is set is racy.
                try await waitUntil {
                    authManager.authRequired && model.syncPresentation == .authRequired
                }
            } catch {
                XCTFail("Timed out waiting for auth-required presentation: \(error)")
            }

            XCTAssertEqual(postRequests.value, 1)
            XCTAssertTrue(authManager.authRequired)
            XCTAssertEqual(model.syncPresentation, .authRequired)
            XCTAssertNil(model.errorMessage, "turn-start \(statusCode) must not use the generic modal")
            XCTAssertNil(KeychainHelper.readString(key: "fa_api_token"))
        }
    }

    func testCancelledOperationNeverRaisesModal() {
        // A cancelled operation is benign — a request superseded or torn down (e.g.
        // the recent-list refresh aborted when `authRequired` latches and cancels the
        // streams, or a conversation switch cancelling an in-flight read). Its `-999`
        // "cancelled" must never surface as the generic error modal.
        let model = makeViewModel(conversationID: "web_conv_cancel_modal")
        model.presentErrorAlert(
            "cancelled",
            reason: .recentConversationsRefresh,
            underlyingError: URLError(.cancelled)
        )
        XCTAssertNil(model.errorMessage, "a URLError.cancelled must not raise a modal")
        model.presentErrorAlert(
            "cancelled",
            reason: .streamError,
            underlyingError: CancellationError()
        )
        XCTAssertNil(model.errorMessage, "a Swift CancellationError must not raise a modal")
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

    func testLiveFollowUserInputEchoRendersBeforeRunningAssistantBubble() async throws {
        let holdStream = HangingStream()
        let streamConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_live_steer",
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
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                switch streamConnects.increment() {
                case 1:
                    return .text(
                        """
                        event: text
                        data: {"turn_id":"turn-live-steer","content":"Working","seq":1}

                        """
                    )
                case 2:
                    return .text(
                        """
                        event: user_input
                        data: {"turn_id":"turn-live-steer","content":"focus on tomorrow","seq":2}

                        """
                    )
                default:
                    return .hangingStream("", controller: holdStream)
                }
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_live_steer",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_live_steer")

        try await waitUntil {
            model?.messages.map(\.text) == ["Earlier", "focus on tomorrow", "Working"]
        }
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "focus on tomorrow", "Working"])

        holdStream.finish()
        model = nil
    }

    func testLiveFollowUserInputEchoDoesNotDuplicatePersistedUserRow() async throws {
        let holdStream = HangingStream()
        let streamConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_live_steer_duplicate",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"turn-live-steer-dup","role":"user","content":"focus on tomorrow","timestamp":"2026-06-08T12:00:01Z"}
                      ],
                      "count":2,
                      "total_messages":2,
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
                streamConnects.increment()
                return .hangingStream(
                    """
                    event: user_input
                    data: {"turn_id":"turn-live-steer-dup","content":"focus on tomorrow","seq":2}

                    """,
                    controller: holdStream
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(conversationID: "web_conv_live_steer_duplicate")
        await model?.selectConversation("web_conv_live_steer_duplicate")
        try await waitUntil { streamConnects.value == 1 }
        await Task.yield()

        XCTAssertEqual(
            model?.messages.filter { $0.turnID == "turn-live-steer-dup" && $0.text == "focus on tomorrow" }.count,
            1
        )
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "focus on tomorrow"])

        holdStream.finish()
        model = nil
    }

    func testLiveFollowRepeatedUserInputEchoRendersDistinctSeqs() async throws {
        let holdStream = HangingStream()
        let streamConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_live_repeated_steer/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_live_repeated_steer",
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
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    switch streamConnects.increment() {
                    case 1:
                        return .text(
                            """
                            event: user_input
                            data: {"turn_id":"turn-live-steer-repeat","content":"same steer","seq":2}

                            """
                        )
                    default:
                        return .hangingStream("", controller: holdStream)
                    }
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_live_repeated_steer",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_live_repeated_steer")
        try await waitUntil { streamConnects.value >= 2 }
        holdStream.finish(
            appending: """
            event: user_input
            data: {"turn_id":"turn-live-steer-repeat","content":"same steer","seq":3}

            """
        )
        try await waitUntil {
            model?.messages.filter { $0.turnID == "turn-live-steer-repeat" && $0.text == "same steer" }.count == 2
        }

        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "same steer", "same steer"])

        model = nil
    }

    func testLiveFollowUserInputEchoNotSuppressedByAssistantRowWithSameText() async throws {
        let holdStream = HangingStream()
        let streamConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_assistant_echo/messages"):
                // The assistant reply shares the steer's turn id and text ("OK").
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_assistant_echo",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"},
                        {"internal_id":2,"turn_id":"turn-assistant-echo","role":"assistant","content":"OK","timestamp":"2026-06-08T12:00:01Z"}
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
                    switch streamConnects.increment() {
                    case 1:
                        return .text(
                            """
                            event: user_input
                            data: {"turn_id":"turn-assistant-echo","content":"OK","seq":2}

                            """
                        )
                    default:
                        return .hangingStream("", controller: holdStream)
                    }
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_assistant_echo",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_assistant_echo")
        try await waitUntil { streamConnects.value >= 2 }

        // The assistant "OK" row must NOT consume the user-input echo's slot: the
        // user's mid-turn steer bubble still renders (the role filter keeps the
        // assistant row from being counted as the persisted user-input echo).
        try await waitUntil {
            model?.messages.filter {
                $0.role == .user && $0.text == "OK" && $0.turnID == "turn-assistant-echo"
            }.count == 1
        }
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "OK", "OK"])

        holdStream.finish()
        model = nil
    }

    func testLateFollowUserInputForEndedTurnIsNotAppended() async throws {
        let holdStream = HangingStream()
        let streamConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_late_dedupe/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_late_dedupe",
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
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    switch streamConnects.increment() {
                    case 1:
                        // End the turn first, so it is recorded in endedTurnIDs.
                        return .text(
                            """
                            event: turn_ended
                            data: {"turn_id":"turn-late-dedupe","status":"complete","seq":1}

                            """
                        )
                    case 2:
                        // A lagging user_input echo for the now-ended turn.
                        return .text(
                            """
                            event: user_input
                            data: {"turn_id":"turn-late-dedupe","content":"focus next week","seq":2}

                            """
                        )
                    default:
                        return .hangingStream("", controller: holdStream)
                    }
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_late_dedupe",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_late_dedupe")
        try await waitUntil { streamConnects.value >= 3 }

        // The late echo for the ended turn must not append a duplicate user bubble.
        XCTAssertEqual(
            model?.messages.filter { $0.text == "focus next week" }.count,
            0
        )
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier"])

        holdStream.finish()
        model = nil
    }

    func testLocalUserInputEchoDoesNotLeakAcrossConversationSwitch() async throws {
        let holdStream = HangingStream()
        let streamConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_echo_a/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_echo_a",
                      "messages":[
                        {"internal_id":1,"role":"user","content":"Earlier A","timestamp":"2026-06-08T12:00:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_echo_b/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_echo_b",
                      "messages":[
                        {"internal_id":9,"role":"user","content":"Only B","timestamp":"2026-06-08T12:00:00Z"}
                      ],
                      "count":1,
                      "total_messages":1,
                      "has_more_before":false,
                      "has_more_after":false
                    }
                    """
                )
            default:
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_echo_a/stream" {
                    switch streamConnects.increment() {
                    case 1:
                        return .text(
                            """
                            event: text
                            data: {"turn_id":"turn-echo-a","content":"Working","seq":1}

                            """
                        )
                    case 2:
                        return .text(
                            """
                            event: user_input
                            data: {"turn_id":"turn-echo-a","content":"old steer","seq":2}

                            """
                        )
                    default:
                        return .hangingStream("", controller: holdStream)
                    }
                }
                if request.httpMethod == "GET", path == "/api/v1/chat/conversations/web_conv_echo_b/stream" {
                    return .text("")
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_echo_a",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_echo_a")
        try await waitUntil {
            model?.messages.map(\.text) == ["Earlier A", "old steer", "Working"]
        }

        await model?.selectConversation("web_conv_echo_b")

        try await waitUntil { model?.messages.map(\.text) == ["Only B"] }
        XCTAssertFalse(model?.messages.contains { $0.text == "old steer" } == true)

        holdStream.finish()
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
        // indicator is still surfaced (presentation is not `.live` while the follow
        // stream can't connect), but the thread is no longer stranded.
        XCTAssertNotEqual(model?.syncPresentation, .live)

        // Release so deinit cancels the fast reconnect loop before teardown.
        model = nil
    }

    func testManualReconnectBumpsBothGenerations() async throws {
        // Finding 12: the toolbar's manual reconnect has no lifecycle transition to
        // bump generations for it (unlike foreground/reachability/re-auth, which bump
        // in the reducer). Without a bump the resync's replacement streams reuse the
        // cancelled consumers' generation and a late old-consumer event would pass
        // the fence. `requestManualReconnect` must bump both generations first.
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path == "/api/v1/chat/activity/stream" {
                return .hangingStream("", controller: HangingStream())
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                return .hangingStream("", controller: HangingStream())
            }
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(#"{"conversation_id":"web_conv_manual","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#)
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let model = makeViewModel(conversationID: "web_conv_manual")
        let followGenerationBefore = model.syncCoordinator.followGeneration
        let activityGenerationBefore = model.syncCoordinator.activityGeneration

        await model.requestManualReconnect()

        XCTAssertGreaterThan(model.syncCoordinator.followGeneration, followGenerationBefore)
        XCTAssertGreaterThan(model.syncCoordinator.activityGeneration, activityGenerationBefore)
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

    func testLiveFollowDoesNotSurfaceErrorOrConfirmationForPassiveTurn() async throws {
        // Dropping the follow stream's event_types filter (M2) let it carry
        // `.error` and tool-confirmation frames. Those drive global UI (a modal
        // "Chat Error" alert, a tool-approval sheet) and must only fire for a turn
        // THIS device is driving — never for one observed passively on the
        // always-on follow stream (started elsewhere / by a schedule). They must
        // be ignored here; only visible reply content renders into a bubble.
        let controller = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_passive",
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
                    #"{"conversation_id":"web_conv_passive","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                // The error and confirmation frames precede the text frame; if the
                // text renders, both earlier frames were already processed.
                return .hangingStream(
                    "event: error\ndata: {\"turn_id\":\"turn-x\",\"error\":\"boom\",\"seq\":5}\n\n"
                        + "event: tool_confirmation_request\ndata: {\"turn_id\":\"turn-x\",\"request_id\":\"req-1\",\"tool_name\":\"delete_all\",\"seq\":6}\n\n"
                        + "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\"Working\",\"seq\":7}\n\n",
                    controller: controller
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(conversationID: "web_conv_passive")
        await model?.selectConversation("web_conv_passive")

        // The text after the error/confirmation frames renders live...
        try await waitUntil { model?.messages.map(\.text) == ["Earlier", "Working"] }
        // ...proving both earlier frames were processed — and neither popped UI.
        XCTAssertNil(model?.errorMessage)
        XCTAssertEqual(model?.pendingConfirmations.isEmpty, true)

        controller.finish()
        model = nil
    }

    func testLiveFollowBubbleSurvivesMidTurnCatchUpMerge() async throws {
        // A still-running follow turn's `local_follow_` bubble must survive a
        // mid-turn history catch-up merge (which otherwise drops every `local_`
        // row) so streaming resumes into the SAME bubble after a reconnect rather
        // than discarding tokens against a stale mapping.
        let streamConnects = AtomicCounter()
        let pinged = AtomicCounter()
        let leg1 = HangingStream()
        let leg2 = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_survive",
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
                // A new persisted row appears exactly at the mid-turn drop, so the
                // catch-up merge has a non-empty delta (the path that drops locals).
                if pinged.value == 0 {
                    return .json(
                        #"{"conversation_id":"web_conv_survive","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_survive",
                      "messages":[
                        {"internal_id":2,"role":"user","content":"ping","timestamp":"2026-06-08T12:01:00Z"}
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
                let n = streamConnects.increment()
                return .hangingStream(
                    n == 1
                        ? "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\"Hello\",\"seq\":5}\n\n"
                        : "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\" world\",\"seq\":6}\n\n",
                    controller: n == 1 ? leg1 : leg2
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_survive",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_survive")
        try await waitUntil { model?.messages.contains { $0.text == "Hello" } == true }

        // A new row persists, then the first follow leg drops without turn_ended,
        // so the catch-up merge runs with a non-empty delta and drops `local_` rows.
        pinged.increment()
        leg1.finish()

        // The running bubble survived the merge and resumed streaming on reconnect:
        // the SAME bubble now holds "Hello world", not a fresh " world".
        try await waitUntil {
            model?.messages.contains { $0.id == "local_follow_turn-x" && $0.text == "Hello world" } == true
        }
        XCTAssertEqual(
            model?.messages.first { $0.id == "local_follow_turn-x" }?.text,
            "Hello world"
        )
        XCTAssertEqual(model?.messages.contains { $0.text == "ping" }, true)

        leg2.finish()
        model = nil
    }

    func testLiveFollowBubbleCompletesWhenTurnEndsWithoutPersistedReply() async throws {
        // On turn_ended the live bubble must stop spinning and keep its streamed
        // text even when the history merge can't replace it yet — the reply isn't
        // queryable (persistence lag returns an empty delta). Otherwise the bubble
        // strands as a stuck .running spinner with no mapping left to reconcile it.
        let streamConnects = AtomicCounter()
        let controller = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_noreply",
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
                // The reply never becomes queryable: every catch-up sees an empty
                // delta, so the merge can never drop/replace the live bubble.
                return .json(
                    #"{"conversation_id":"web_conv_noreply","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "POST", path == "/api/v1/chat/ack" {
                return .json("{}")
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                // First connect streams a token then holds; later reconnects hang
                // benignly so they don't re-deliver the (now ended) turn's frames.
                if streamConnects.increment() == 1 {
                    return .hangingStream(
                        "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\"Hello\",\"seq\":5}\n\n",
                        controller: controller
                    )
                }
                return .hangingStream("", controller: HangingStream())
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_noreply",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_noreply")
        try await waitUntil { model?.messages.contains { $0.text == "Hello" } == true }

        // The turn ends, but its reply is not yet queryable (empty delta).
        controller.finish(
            appending: """
            event: turn_ended
            data: {"turn_id":"turn-x","status":"complete","seq":6}

            """
        )

        // The bubble completes in place: it stops spinning and keeps "Hello"
        // instead of stranding as a stuck running placeholder.
        try await waitUntil {
            model?.messages.first { $0.id == "local_follow_turn-x" }?.status == .complete
        }
        let bubble = model?.messages.first { $0.id == "local_follow_turn-x" }
        XCTAssertEqual(bubble?.text, "Hello")
        XCTAssertEqual(bubble?.isLoading, false)

        model = nil
    }

    func testSwitchingConversationsDoesNotLeakOldFollowStreamCursor() async throws {
        // The follow loop is cancelled at the top of selectConversation, before the
        // `await loadMessages` of the new conversation suspends. Otherwise the OLD
        // conversation's still-live follow loop would deliver an event during that
        // window and pollute the NEW conversation's reset ack cursor (and merge its
        // rows). We observe the cursor pollution via the ack_seq the new
        // conversation's follow stream subscribes with.
        let oldStream = HangingStream()
        let newHistory = HangingStream()
        let contaminatedAck = AtomicCounter()
        let newStreamConnects = AtomicCounter()
        let oldConvAcks = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let after = Self.queryItems(from: request)["after"]
            if request.httpMethod == "GET", path.hasSuffix("/conversations/conv_old/messages") {
                return .json(
                    #"{"conversation_id":"conv_old","messages":[{"internal_id":1,"role":"user","content":"OldEarlier","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path.hasSuffix("/conversations/conv_new/messages") {
                if after == nil {
                    // Hang the new conversation's history load, holding
                    // selectConversation open across its suspension point.
                    return .hangingStream("", controller: newHistory)
                }
                return .json(
                    #"{"conversation_id":"conv_new","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "POST", path == "/api/v1/chat/ack" {
                // An ack for the OLD conversation proves the old loop processed the
                // leaked turn_ended (and therefore set the shared cursor to 999).
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   body["conversation_id"] as? String == "conv_old" {
                    oldConvAcks.increment()
                }
                return .json("{}")
            }
            if request.httpMethod == "GET", path.hasSuffix("/conversations/conv_old/stream") {
                // The old conversation's follow loop; held open until finished.
                return .hangingStream("", controller: oldStream)
            }
            if request.httpMethod == "GET", path.hasSuffix("/conversations/conv_new/stream") {
                // If the old loop leaked its turn_ended seq into the shared cursor,
                // the new conversation subscribes with that polluted ack_seq.
                newStreamConnects.increment()
                if Self.queryItems(from: request)["ack_seq"] == "999" {
                    contaminatedAck.increment()
                }
                return .hangingStream("", controller: HangingStream())
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(conversationID: "conv_old")
        await model?.selectConversation("conv_old")
        try await waitUntil { model?.messages.map(\.text) == ["OldEarlier"] }

        // Begin switching to the new conversation; its history load hangs, holding
        // selectConversation suspended at the `await loadMessages`.
        let switchTask = Task { await model?.selectConversation("conv_new") }
        try await waitUntil { model?.conversationID == "conv_new" }

        // While suspended, the OLD stream delivers a turn_ended at a high seq. With
        // the follow loop cancelled up front this is dropped; otherwise it would
        // call recordAppliedSeq(999) and pollute the new conversation's cursor.
        oldStream.finish(
            appending: """
            event: turn_ended
            data: {"turn_id":"turn-old","status":"complete","seq":999}

            """
        )

        // Sequence the two finishes deterministically: give the old loop a bounded
        // window to (mis)process the leaked event. Without the fix it processes
        // turn_ended — setting the cursor to 999 and acking conv_old — within
        // milliseconds, so we proceed as soon as that ack lands; with the fix the
        // loop was cancelled before the switch suspended, so no ack ever comes and
        // we fall through after the bound. This guarantees the regression (unfixed)
        // path has actually polluted the cursor before the new stream connects.
        let settleDeadline = Date().addingTimeInterval(1.5)
        while Date() < settleDeadline, oldConvAcks.value == 0 {
            try await Task.sleep(for: .milliseconds(20))
        }

        // Now let the new conversation's history load finish, which lets
        // selectConversation complete and start the new follow stream.
        newHistory.finish(
            appending: #"{"conversation_id":"conv_new","messages":[{"internal_id":9,"role":"assistant","content":"NewReply","timestamp":"2026-06-08T12:10:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
        )
        _ = await switchTask.value

        // The new conversation loads cleanly, and its follow stream connects — that
        // connection must NOT carry the old turn's seq (999) as its ack cursor.
        try await waitUntil { model?.messages.contains { $0.text == "NewReply" } == true }
        try await waitUntil { newStreamConnects.value >= 1 }
        XCTAssertEqual(contaminatedAck.value, 0)
        XCTAssertFalse(model?.messages.contains { $0.text == "OldEarlier" } == true)

        model = nil
    }

    func testRealResumeSceneSequenceRestartsStreamsExactlyOnce() async throws {
        // A normal iOS resume delivers TWO scene-phase firings —
        // `.background -> .inactive` then `.inactive -> .active` — which the former
        // single-transition predicate never matched. The latched gate must run
        // exactly one resync across the sequence, and none for an
        // `.inactive -> .active` blip that never backgrounded.
        //
        // With the subscribe-then-buffer resync (§4.4), ONE resync makes exactly
        // two follow connects: the buffering subscription established before the
        // snapshot fetch, then the loop reconnect on handover. Two resyncs would
        // add four; a blip adds none. So the "exactly one resync" invariant is
        // pinned as `+2` follow connects, not `+1`.
        let followConnects = AtomicCounter()
        let followController = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/stream"), !path.contains("activity") {
                followConnects.increment()
                // Hang so the follow loop never spontaneously reconnects: any
                // additional connect is attributable to a resync (buffering
                // subscribe or handover restart).
                return .hangingStream("", controller: HangingStream())
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/activity/stream" {
                return .hangingStream("", controller: followController)
            }
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    #"{"conversation_id":"web_conv_scene","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_scene",
            liveReconnectInitialDelaySeconds: 10,
            liveReconnectMaxDelaySeconds: 10
        )
        await model?.selectConversation("web_conv_scene")
        try await waitUntil { followConnects.value == 1 }
        let baseline = followConnects.value

        // Realistic resume: two firings. Only the latched foreground runs a resync,
        // which makes two follow connects (buffering subscribe + handover restart).
        model?.scenePhaseChanged(old: .active, new: .background)
        model?.scenePhaseChanged(old: .background, new: .inactive)
        model?.scenePhaseChanged(old: .inactive, new: .active)
        try await waitUntil { followConnects.value == baseline + 2 }
        XCTAssertEqual(followConnects.value, baseline + 2)

        // An `.inactive -> .active` blip with no prior background triggers none.
        model?.scenePhaseChanged(old: .active, new: .inactive)
        model?.scenePhaseChanged(old: .inactive, new: .active)
        // Give any erroneous resync a chance to fire before asserting it didn't.
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(followConnects.value, baseline + 2)

        followController.finish()
        model = nil
    }

    func testBackgroundDuringTurnPostSuspendsSendAndPreservesSession() async throws {
        let postBody = HangingStream()
        let followConnects = AtomicCounter()
        let activityConnects = AtomicCounter()
        let postedTurnID = AtomicString()
        let cancelRequests = AtomicCounter()
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-bg-post-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                // Hold the POST response body open so the turn is observably
                // in-flight (start_turn awaiting) when the app backgrounds.
                return .hangingStream("", controller: postBody)
            case ("POST", _) where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(#"{"turn_id":"","conversation_id":"web_conv_bg_post","status":"cancelling","already_complete":false}"#)
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                activityConnects.increment()
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_bg_post/stream" {
                    followConnects.increment()
                    return .hangingStream("", controller: HangingStream())
                }
                if method == "GET", path.hasSuffix("/messages") {
                    return .json(#"{"conversation_id":"web_conv_bg_post","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_bg_post",
            spoolDirectory: spoolDirectory,
            liveReconnectInitialDelaySeconds: 10,
            liveReconnectMaxDelaySeconds: 10
        )
        await model.bootstrap()
        try await waitUntil { followConnects.value >= 1 && activityConnects.value >= 1 }

        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }
        let sessionBefore = try XCTUnwrap(model.activeTurnSessionForTesting)
        let followGenerationBefore = model.syncCoordinator.followGeneration
        let activityGenerationBefore = model.syncCoordinator.activityGeneration
        let followBaseline = followConnects.value
        let activityBaseline = activityConnects.value

        // Capture the transport task before the suspend nils `streamTask`, so we can
        // drive its ASYNCHRONOUS cancellation aftermath to completion (F1).
        let sendTask = model.sendTaskForTesting
        model.scenePhaseChanged(old: .active, new: .background)

        try assertBackgroundSuspendedSend(
            model: model,
            sessionBefore: sessionBefore,
            followGenerationBefore: followGenerationBefore,
            activityGenerationBefore: activityGenerationBefore,
            followConnects: followConnects,
            activityConnects: activityConnects,
            followBaseline: followBaseline,
            activityBaseline: activityBaseline,
            cancelRequests: cancelRequests,
            spoolDirectory: spoolDirectory
        )
        // Release the held POST and drive the cancelled task to completion: its
        // `startTurn` throws CancellationError (POST in flight when backgrounded),
        // so the aftermath runs the CancellationError catch. It must NOT roll back,
        // mark the bubble stopped, surface an error, or clear the session.
        postBody.finish()
        await sendTask?.value
        try assertSuspendedSendSurvivesAftermath(
            model: model,
            sessionBefore: sessionBefore,
            cancelRequests: cancelRequests,
            spoolDirectory: spoolDirectory
        )
    }

    func testBackgroundAfterAcceptBeforeFirstStreamEventSuspendsSend() async throws {
        let followStream = HangingStream()
        let followConnects = AtomicCounter()
        let activityConnects = AtomicCounter()
        let postedTurnID = AtomicString()
        let cancelRequests = AtomicCounter()
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-bg-accept-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"web_conv_bg_accept","first_seq":0}"#)
            case ("POST", _) where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(#"{"turn_id":"","conversation_id":"web_conv_bg_accept","status":"cancelling","already_complete":false}"#)
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                activityConnects.increment()
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_bg_accept/stream" {
                    let count = followConnects.increment()
                    // The send's own stream subscription hangs with no events, so
                    // background lands after accept but before the first frame.
                    // (The first connect is the follow stream from selectConversation;
                    // the second is the send subscription — both hang.)
                    _ = count
                    return .hangingStream("", controller: followStream)
                }
                if method == "GET", path.hasSuffix("/messages") {
                    return .json(#"{"conversation_id":"web_conv_bg_accept","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_bg_accept",
            spoolDirectory: spoolDirectory,
            liveReconnectInitialDelaySeconds: 10,
            liveReconnectMaxDelaySeconds: 10
        )
        await model.bootstrap()
        try await waitUntil { followConnects.value >= 1 && activityConnects.value >= 1 }

        model.draftText = "Hi"
        await model.sendDraft()
        // The turn was accepted (POST returned) and its stream subscription is
        // connected but has produced no event yet.
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }
        let sessionBefore = try XCTUnwrap(model.activeTurnSessionForTesting)
        let followGenerationBefore = model.syncCoordinator.followGeneration
        let activityGenerationBefore = model.syncCoordinator.activityGeneration
        let followBaseline = followConnects.value
        let activityBaseline = activityConnects.value

        // Capture the transport task before the suspend nils `streamTask`, so the
        // aftermath await below actually waits for the cancellation cleanup (F1).
        let sendTask = model.sendTaskForTesting
        model.scenePhaseChanged(old: .active, new: .background)

        try assertBackgroundSuspendedSend(
            model: model,
            sessionBefore: sessionBefore,
            followGenerationBefore: followGenerationBefore,
            activityGenerationBefore: activityGenerationBefore,
            followConnects: followConnects,
            activityConnects: activityConnects,
            followBaseline: followBaseline,
            activityBaseline: activityBaseline,
            cancelRequests: cancelRequests,
            spoolDirectory: spoolDirectory
        )
        // Drive the aftermath: the turn was accepted, so the cancelled subscription
        // returns `.interrupted` and the resume/tail guards `return` early — the
        // session must survive intact.
        followStream.finish()
        await sendTask?.value
        try assertSuspendedSendSurvivesAftermath(
            model: model,
            sessionBefore: sessionBefore,
            cancelRequests: cancelRequests,
            spoolDirectory: spoolDirectory
        )
    }

    func testBackgroundMidStreamSuspendsSendAndPreservesCursor() async throws {
        let followStream = HangingStream()
        let followConnects = AtomicCounter()
        let activityConnects = AtomicCounter()
        let postedTurnID = AtomicString()
        let cancelRequests = AtomicCounter()
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-bg-mid-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"web_conv_bg_mid","first_seq":0}"#)
            case ("POST", _) where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(#"{"turn_id":"","conversation_id":"web_conv_bg_mid","status":"cancelling","already_complete":false}"#)
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                activityConnects.increment()
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_bg_mid/stream" {
                    let isSend = Self.queryItems(from: request)["follow"] == "false"
                    if !isSend {
                        // The advisory follow stream (follow=true): hang with no
                        // events so it only ends on the background teardown.
                        followConnects.increment()
                        return .hangingStream("", controller: HangingStream())
                    }
                    // The send subscription (follow=false): deliver turn_started
                    // (registering the turn) then hang mid-turn with no
                    // turn_ended, so the send loop is actively consuming the
                    // stream when the app backgrounds.
                    return .hangingStream(
                        """
                        event: turn_started
                        data: {"turn_id":"\(postedTurnID.value ?? "turn-bg-mid")","seq":0}

                        """,
                        controller: followStream
                    )
                }
                if method == "GET", path.hasSuffix("/messages") {
                    return .json(#"{"conversation_id":"web_conv_bg_mid","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_bg_mid",
            spoolDirectory: spoolDirectory,
            liveReconnectInitialDelaySeconds: 10,
            liveReconnectMaxDelaySeconds: 10
        )
        await model.bootstrap()
        try await waitUntil { followConnects.value >= 1 && activityConnects.value >= 1 }

        model.draftText = "Hi"
        await model.sendDraft()
        // Wait until the turn is registered from turn_started: the send loop is
        // now actively consuming the (still-open) subscription when we background.
        try await waitUntil {
            model.isStreaming
                && model.turnControlStateForTesting.registeredTurnIDs.contains(postedTurnID.value ?? "")
        }
        let sessionBefore = try XCTUnwrap(model.activeTurnSessionForTesting)
        let cursorBefore = sessionBefore.lastAppliedSeq
        let followGenerationBefore = model.syncCoordinator.followGeneration
        let activityGenerationBefore = model.syncCoordinator.activityGeneration
        let followBaseline = followConnects.value
        let activityBaseline = activityConnects.value

        // Capture the transport task before the suspend nils `streamTask`, so the
        // aftermath await below actually waits for the cancellation cleanup (F1).
        let sendTask = model.sendTaskForTesting
        model.scenePhaseChanged(old: .active, new: .background)

        try assertBackgroundSuspendedSend(
            model: model,
            sessionBefore: sessionBefore,
            followGenerationBefore: followGenerationBefore,
            activityGenerationBefore: activityGenerationBefore,
            followConnects: followConnects,
            activityConnects: activityConnects,
            followBaseline: followBaseline,
            activityBaseline: activityBaseline,
            cancelRequests: cancelRequests,
            spoolDirectory: spoolDirectory
        )
        // The session's durable resume cursor is untouched by the suspend:
        // suspendActiveSend cancels the transport without clobbering it.
        XCTAssertEqual(
            model.activeTurnSessionForTesting?.lastAppliedSeq,
            cursorBefore,
            "The mid-stream ack/resume cursor must survive the background suspend."
        )
        XCTAssertTrue(
            model.turnControlStateForTesting.registeredTurnIDs.contains(postedTurnID.value ?? ""),
            "The registered-turn control state must survive the background suspend."
        )
        // Drive the mid-stream cancellation aftermath to completion and verify the
        // session (and its cursor / registered-turn control state) still survives.
        followStream.finish()
        await sendTask?.value
        try assertSuspendedSendSurvivesAftermath(
            model: model,
            sessionBefore: sessionBefore,
            cancelRequests: cancelRequests,
            spoolDirectory: spoolDirectory
        )
        XCTAssertEqual(
            model.activeTurnSessionForTesting?.lastAppliedSeq,
            cursorBefore,
            "The resume cursor must survive the asynchronous suspend aftermath."
        )
        XCTAssertTrue(
            model.turnControlStateForTesting.registeredTurnIDs.contains(postedTurnID.value ?? ""),
            "The registered-turn control state must survive the asynchronous suspend aftermath."
        )
    }

    func testForegroundReattachContinuesRenderingRunningTurn() async throws {
        // §4.3 scenario (a): background mid-stream, then foreground while the turn is
        // STILL running server-side. The foreground resync awaits the old consumer,
        // snapshots (active_turns still reports the turn running), and the new follow
        // stream tails its remaining tokens into the live bubble — rendering
        // continues, the session instance is preserved, and no alert fires.
        let sendSubscription = HangingStream()
        let advisoryFollow = HangingStream()
        let postedTurnID = AtomicString()
        let foregrounded = AtomicCounter()
        let followTokenAfterForeground = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"web_conv_reattach","first_seq":0}"#)
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_reattach/stream" {
                    let isSend = Self.queryItems(from: request)["follow"] == "false"
                    if isSend {
                        return .hangingStream(
                            """
                            event: turn_started
                            data: {"turn_id":"\(postedTurnID.value ?? "turn-reattach")","seq":0}

                            """,
                            controller: sendSubscription
                        )
                    }
                    // The advisory follow stream: before foreground it hangs with no
                    // events; the post-foreground reconnect tails a live token for
                    // the still-running turn.
                    if foregrounded.value == 0 {
                        return .hangingStream("", controller: advisoryFollow)
                    }
                    // Model the server's ring-buffer replay: every post-foreground
                    // follow connect resumes from the client's cursor (`from_seq`) and
                    // replays `seq >= from_seq`. The tail token (seq 8) is delivered on
                    // any connect whose cursor has not yet passed it — so whether the
                    // token lands in the resync's buffering connect or the steady-state
                    // reconnect after it, it is delivered exactly once (the client
                    // resumes from seq 9 after applying it, and this branch then hangs).
                    // Keying on `from_seq` rather than the connect ordinal removes the
                    // buffering-window race that an ordinal gate would introduce.
                    let fromSeq = Int(Self.queryItems(from: request)["from_seq"] ?? "") ?? -1
                    guard fromSeq <= 8 else {
                        return .hangingStream("", controller: HangingStream())
                    }
                    return .hangingStream(
                        "event: text\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "turn-reattach")\",\"content\":\"tail token\",\"seq\":8}\n\n",
                        controller: followTokenAfterForeground
                    )
                }
                if method == "GET", path.hasSuffix("/messages") {
                    // The turn is still running: history holds no reply yet, and
                    // active_turns reports it as running (only once the turn has been
                    // posted, so a pre-send bootstrap load doesn't report a phantom).
                    guard let turnID = postedTurnID.value else {
                        return .json(
                            #"{"conversation_id":"web_conv_reattach","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                        )
                    }
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_reattach",
                          "messages":[],"count":0,"total_messages":0,
                          "has_more_before":false,"has_more_after":false,
                          "active_turns":[{"turn_id":"\(turnID)","started_at":"2026-06-08T12:00:01Z","latest_seq":7,"status":"running"}]
                        }
                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_reattach",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model.bootstrap()

        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil {
            model.isStreaming
                && model.turnControlStateForTesting.registeredTurnIDs.contains(postedTurnID.value ?? "")
        }
        let sessionBefore = try XCTUnwrap(model.activeTurnSessionForTesting)

        model.scenePhaseChanged(old: .active, new: .background)
        XCTAssertFalse(model.isStreaming)
        // Finish only the advisory follow so the old advisory consumer terminates
        // promptly. The send subscription is left hanging so the send loop exits
        // ONLY via the suspend's cancellation (a clean `Task.isCancelled` break):
        // delivering a clean EOF instead would let the send loop's resume streak
        // exhaust and `markTurnEnded` the still-running turn, which would then
        // suppress the reattach follow token under test.
        advisoryFollow.finish()

        foregrounded.increment()
        model.scenePhaseChanged(old: .background, new: .active)

        // The still-running turn's tail token streams into a live bubble via the
        // reattach follow path — rendered exactly once for the posted turn.
        try await waitUntil(timeout: 8) {
            model.messages.contains { $0.text == "tail token" && $0.turnID == postedTurnID.value }
        }
        XCTAssertEqual(
            model.messages.filter { $0.turnID == postedTurnID.value }.count,
            1,
            "The reattached turn must render into exactly one bubble."
        )
        XCTAssertTrue(
            model.activeTurnSessionForTesting === sessionBefore,
            "The ActiveTurnSession instance must be preserved across the reattach."
        )
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(model.messages.contains { $0.text == "Response stopped." })

        // The composer keys steer/stop mode SOLELY off `isStreaming` (ChatViews:
        // `if viewModel.isStreaming { steer/stop } else { sendDraft }`). Reattaching
        // a still-running turn must restore it so the user can't fire a SECOND normal
        // send into the same conversation (overlapping turns). The preserved session
        // carries the turn identity a steer/stop targets.
        XCTAssertTrue(
            model.isStreaming,
            "A reattached, still-running turn must restore the composer's steer/stop mode."
        )
        XCTAssertEqual(
            model.activeTurnSessionForTesting?.turnID,
            postedTurnID.value,
            "The reattached session's running turn identity must drive steer/stop, not a fresh send."
        )

        followTokenAfterForeground.finish()
        sendSubscription.finish()
    }

    func testStopReachesServerForReattachedRunningTurn() async throws {
        // Finding 8: a turn reattached on foreground has NO live send task (the
        // suspend tore its transport down). Stop branches on `registeredTurnIDs`; if
        // the reattached turn is left unregistered, Stop enqueues into
        // `stopAfterRegistrationByTurnID` — but the send task that would flush that
        // queue is dead, so the cancel silently never reaches the server. The
        // reattach must mark the turn registered so Stop takes the normal request
        // path and actually issues the cancel.
        let sendSubscription = HangingStream()
        let advisoryFollow = HangingStream()
        let postedTurnID = AtomicString()
        let foregrounded = AtomicCounter()
        let cancelRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"web_conv_reattach_stop","first_seq":0}"#)
            case ("POST", _) where path.hasPrefix("/api/v1/chat/turns/") && path.hasSuffix("/cancel"):
                cancelRequests.increment()
                return .json(#"{"turn_id":"","conversation_id":"web_conv_reattach_stop","status":"cancelled","already_complete":false}"#)
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_reattach_stop/stream" {
                    let isSend = Self.queryItems(from: request)["follow"] == "false"
                    if isSend {
                        return .hangingStream(
                            """
                            event: turn_started
                            data: {"turn_id":"\(postedTurnID.value ?? "turn-reattach")","seq":0}

                            """,
                            controller: sendSubscription
                        )
                    }
                    return .hangingStream("", controller: advisoryFollow)
                }
                if method == "GET", path.hasSuffix("/messages") {
                    guard let turnID = postedTurnID.value else {
                        return .json(
                            #"{"conversation_id":"web_conv_reattach_stop","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                        )
                    }
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_reattach_stop",
                          "messages":[],"count":0,"total_messages":0,
                          "has_more_before":false,"has_more_after":false,
                          "active_turns":[{"turn_id":"\(turnID)","started_at":"2026-06-08T12:00:01Z","latest_seq":7,"status":"running"}]
                        }
                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_reattach_stop",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model.bootstrap()

        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil {
            model.isStreaming
                && model.turnControlStateForTesting.registeredTurnIDs.contains(postedTurnID.value ?? "")
        }

        model.scenePhaseChanged(old: .active, new: .background)
        XCTAssertFalse(model.isStreaming)
        advisoryFollow.finish()

        foregrounded.increment()
        model.scenePhaseChanged(old: .background, new: .active)

        // Reattach restores steer/stop mode and re-registers the turn.
        try await waitUntil(timeout: 8) {
            model.isStreaming
                && model.turnControlStateForTesting.registeredTurnIDs.contains(postedTurnID.value ?? "")
        }

        // Stop now takes the normal request path and issues the cancel to the server.
        await model.stopTurn()
        try await waitUntil(timeout: 4) { cancelRequests.value >= 1 }
        XCTAssertGreaterThanOrEqual(cancelRequests.value, 1)

        sendSubscription.finish()
    }

    func testForegroundReattachReconcilesTurnThatFinishedWhileBackgrounded() async throws {
        // §4.3 scenario (b): background mid-stream, then foreground after the turn
        // FINISHED server-side. History reconciliation surfaces the completed
        // message; no alert and no "Response stopped." (regression for prod clusters
        // C–E).
        let sendSubscription = HangingStream()
        let advisoryFollow = HangingStream()
        let postedTurnID = AtomicString()
        let foregrounded = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"web_conv_reattach_done","first_seq":0}"#)
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_reattach_done/stream" {
                    let isSend = Self.queryItems(from: request)["follow"] == "false"
                    if isSend {
                        return .hangingStream(
                            """
                            event: turn_started
                            data: {"turn_id":"\(postedTurnID.value ?? "turn-done")","seq":0}

                            """,
                            controller: sendSubscription
                        )
                    }
                    // The advisory follow before foreground hangs; the reconnect
                    // after foreground finds the turn already ended (no active_turns)
                    // and simply holds open with nothing to stream.
                    return .hangingStream("", controller: advisoryFollow)
                }
                if method == "GET", path.hasSuffix("/messages") {
                    if foregrounded.value == 0 {
                        let activeTurns = postedTurnID.value.map {
                            "[{\"turn_id\":\"\($0)\",\"started_at\":\"2026-06-08T12:00:01Z\",\"latest_seq\":7,\"status\":\"running\"}]"
                        } ?? "[]"
                        return .json(
                            """
                            {
                              "conversation_id":"web_conv_reattach_done",
                              "messages":[],"count":0,"total_messages":0,
                              "has_more_before":false,"has_more_after":false,
                              "active_turns":\(activeTurns)
                            }
                            """
                        )
                    }
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_reattach_done",
                          "messages":[
                            {"internal_id":2,"role":"assistant","content":"Completed while backgrounded","timestamp":"2026-06-08T12:05:00Z","turn_id":"\(postedTurnID.value ?? "turn-done")"}
                          ],
                          "count":1,"total_messages":1,"has_more_before":false,"has_more_after":false
                        }
                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_reattach_done",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model.bootstrap()

        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil {
            model.isStreaming
                && model.turnControlStateForTesting.registeredTurnIDs.contains(postedTurnID.value ?? "")
        }

        model.scenePhaseChanged(old: .active, new: .background)
        advisoryFollow.finish()
        sendSubscription.finish()

        foregrounded.increment()
        model.scenePhaseChanged(old: .background, new: .active)

        try await waitUntil(timeout: 8) {
            model.messages.contains { $0.text == "Completed while backgrounded" }
        }
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(
            model.messages.contains { $0.text == "Response stopped." },
            "A turn that finished while backgrounded must reconcile silently, never 'Response stopped.'."
        )
        // The turn is NOT in active_turns (finished server-side): the suspended
        // session is reconciled away and steer/stop mode is NOT restored, so the
        // composer returns to a normal send.
        try await waitUntil(timeout: 8) { model.activeTurnSessionForTesting == nil }
        XCTAssertFalse(
            model.isStreaming,
            "A turn that finished while backgrounded must leave the composer in normal-send mode."
        )
    }

    func testForegroundReconcileRemovesOrphanedPlaceholderForUnregisteredTurn() async throws {
        // §4.3: background while the initial turn POST is in flight so the turn never
        // registers server-side. On foreground the incremental merge returns an empty
        // delta and no active_turns; the empty-delta early return would otherwise skip
        // the `local_` drop, stranding the optimistic assistant bubble spinning in
        // `running` forever. The suspended-session reconcile must remove it.
        let postBody = HangingStream()
        let postedTurnID = AtomicString()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                // Hang the POST so the turn is in flight when we background; the
                // suspend cancels it before it can register server-side.
                return .hangingStream("", controller: postBody)
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path.hasSuffix("/stream") {
                    return .hangingStream("", controller: HangingStream())
                }
                if method == "GET", path.hasSuffix("/messages") {
                    let hasAfter = Self.queryItems(from: request)["after"] != nil
                    if !hasAfter {
                        // Initial full load: one persisted message so the later merge
                        // takes the incremental (empty-delta) path, not a full reload.
                        return .json(
                            """
                            {
                              "conversation_id":"web_conv_orphan",
                              "messages":[
                                {"internal_id":1,"role":"assistant","content":"Earlier reply","timestamp":"2026-06-08T12:00:00Z","turn_id":"prior"}
                              ],
                              "count":1,"total_messages":1,"has_more_before":false,"has_more_after":false
                            }
                            """
                        )
                    }
                    // Incremental fetch after foreground: empty delta, no active_turns
                    // (the turn never registered).
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_orphan",
                          "messages":[],"count":0,"total_messages":0,
                          "has_more_before":false,"has_more_after":false,"active_turns":[]
                        }
                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_orphan",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model.bootstrap()

        model.draftText = "Hi"
        await model.sendDraft()
        // The optimistic assistant placeholder is rendered and the session preserved
        // while the POST is in flight.
        try await waitUntil { model.activeTurnSessionForTesting != nil }
        XCTAssertTrue(
            model.messages.contains { $0.status == .running },
            "The send optimistically renders a running assistant placeholder."
        )

        model.scenePhaseChanged(old: .active, new: .background)
        postBody.finish()
        model.scenePhaseChanged(old: .background, new: .active)

        // The reconcile clears the never-registered session and removes the orphaned
        // placeholder — no bubble is left spinning.
        try await waitUntil(timeout: 8) { model.activeTurnSessionForTesting == nil }
        XCTAssertFalse(
            model.messages.contains { $0.status == .running },
            "A never-registered suspended turn must not strand a running assistant placeholder."
        )
        XCTAssertNil(model.errorMessage)
    }

    func testDeadSendTransportReconcilesCompletedMessageWithoutAlert() async throws {
        // §4.3 scenario (c): a send whose transport died while the turn kept running
        // (a mid-turn drop the send loop couldn't resume). The next resync's history
        // snapshot surfaces the completed reply with no alert — not a fabricated
        // "Response stopped." or a Chat Error modal.
        let sendSubscription = HangingStream()
        let advisoryFollow = HangingStream()
        let postedTurnID = AtomicString()
        let reconnected = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"web_conv_deadsend","first_seq":0}"#)
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_deadsend/stream" {
                    let isSend = Self.queryItems(from: request)["follow"] == "false"
                    if isSend {
                        // The send subscription: deliver turn_started then a clean EOF
                        // (empty stream that finishes) so the send loop's resume streak
                        // exhausts and it falls back to a history reload — the
                        // transport is dead but the turn kept running server-side.
                        return .text(
                            """
                            event: turn_started
                            data: {"turn_id":"\(postedTurnID.value ?? "turn-dead")","seq":0}

                            """
                        )
                    }
                    reconnected.increment()
                    return .hangingStream("", controller: advisoryFollow)
                }
                if method == "GET", path.hasSuffix("/messages") {
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_deadsend",
                          "messages":[
                            {"internal_id":2,"role":"assistant","content":"Reply after dead transport","timestamp":"2026-06-08T12:05:00Z","turn_id":"\(postedTurnID.value ?? "turn-dead")"}
                          ],
                          "count":1,"total_messages":1,"has_more_before":false,"has_more_after":false
                        }
                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_deadsend",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01,
            maxConsecutiveStreamResumes: 1,
            streamResumeLivenessSeconds: 100
        )
        await model.bootstrap()

        model.draftText = "Hi"
        await model.sendDraft()

        // The dead transport falls back to a history reload, which surfaces the
        // completed reply — with no alert and no "Response stopped."
        try await waitUntil(timeout: 8) {
            model.messages.contains { $0.text == "Reply after dead transport" }
        }
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(model.messages.contains { $0.text == "Response stopped." })

        advisoryFollow.finish()
    }

    func testPushHintWhileForegroundedRefreshesConversationAndList() async throws {
        // §4.6: a push arriving while foregrounded targets the referenced
        // conversation (merge new messages) and refreshes the recent list.
        let pushed = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                if pushed.value == 0 {
                    return .json(#"{"conversations":[],"count":0}"#)
                }
                return .json(
                    #"{"conversations":[{"conversation_id":"web_conv_pushrefresh","last_message":"Pushed reply","last_timestamp":"2026-06-08T12:05:00Z","message_count":2}],"count":1}"#
                )
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_pushrefresh/stream" {
                    return .hangingStream("", controller: HangingStream())
                }
                if method == "GET", path.hasSuffix("/messages") {
                    if Self.queryItems(from: request)["after"] == nil {
                        return .json(
                            """
                            {
                              "conversation_id":"web_conv_pushrefresh",
                              "messages":[{"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}],
                              "count":1,"total_messages":1,"has_more_before":false,"has_more_after":false
                            }
                            """
                        )
                    }
                    if pushed.value == 0 {
                        return .json(
                            #"{"conversation_id":"web_conv_pushrefresh","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                        )
                    }
                    return .json(
                        """
                        {
                          "conversation_id":"web_conv_pushrefresh",
                          "messages":[{"internal_id":2,"role":"assistant","content":"Pushed reply","timestamp":"2026-06-08T12:05:00Z"}],
                          "count":1,"total_messages":2,"has_more_before":false,"has_more_after":false
                        }
                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_pushrefresh")
        await model.selectConversation("web_conv_pushrefresh")
        try await waitUntil { model.messages.map(\.text) == ["Earlier"] }

        pushed.increment()
        model.pushHintReceived(conversationID: "web_conv_pushrefresh")

        try await waitUntil(timeout: 6) {
            model.messages.map(\.text) == ["Earlier", "Pushed reply"]
        }
        try await waitUntil(timeout: 6) {
            model.conversations.contains { $0.conversationID == "web_conv_pushrefresh" }
        }
    }

    func testPushHintWhileBackgroundedDoesNothing() async throws {
        // §4.6/§4.8: a push arriving while backgrounded is a no-op — no targeted
        // refresh runs (silent-push/background refresh is out of scope).
        let messageMerges = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_bgpush/stream" {
                    return .hangingStream("", controller: HangingStream())
                }
                if method == "GET", path.hasSuffix("/messages") {
                    if Self.queryItems(from: request)["after"] != nil {
                        messageMerges.increment()
                    }
                    return .json(
                        #"{"conversation_id":"web_conv_bgpush","messages":[{"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_bgpush")
        await model.selectConversation("web_conv_bgpush")
        try await waitUntil { model.messages.map(\.text) == ["Earlier"] }

        model.scenePhaseChanged(old: .active, new: .background)
        let mergesAfterBackground = messageMerges.value

        model.pushHintReceived(conversationID: "web_conv_bgpush")
        // Give any (erroneous) refresh a chance to fire before asserting it didn't.
        try await Task.sleep(for: .milliseconds(200))

        XCTAssertEqual(
            messageMerges.value,
            mergesAfterBackground,
            "A backgrounded push hint must not trigger a message merge."
        )
    }

    func testPushHintRefreshFailureIsSilent() async throws {
        // F5: a push-hint targeted refresh is advisory. When its selected-conversation
        // message merge fails, the failure must feed breadcrumbs and coordinator
        // health but NEVER raise a modal Chat Error — no errorMessage, no
        // Chat.alertPresented. The merge failure is armed only AFTER the advisory
        // follow-connect catch-up merge (a separate M3-scoped path) has completed,
        // so it is not a confound.
        let mergeArmed = AtomicCounter()
        let mergeFetches = AtomicCounter()
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-push-silent-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_pushfail/stream" {
                    return .hangingStream("", controller: HangingStream())
                }
                if method == "GET", path.hasSuffix("/messages") {
                    if Self.queryItems(from: request)["after"] == nil {
                        return .json(
                            #"{"conversation_id":"web_conv_pushfail","messages":[{"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                        )
                    }
                    mergeFetches.increment()
                    // The push-hint selected-conversation merge fetch fails once armed.
                    if mergeArmed.value > 0 {
                        return .json(#"{"detail":"boom"}"#, statusCode: 500)
                    }
                    return .json(
                        #"{"conversation_id":"web_conv_pushfail","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_pushfail",
            spoolDirectory: spoolDirectory
        )
        await model.selectConversation("web_conv_pushfail")
        try await waitUntil { model.messages.map(\.text) == ["Earlier"] }
        // Wait for the advisory follow-connect catch-up merge to complete, then arm.
        try await waitUntil { mergeFetches.value >= 1 }
        XCTAssertNil(model.errorMessage)
        mergeArmed.increment()

        model.pushHintReceived(conversationID: "web_conv_pushfail")
        // Wait for the (failing) merge breadcrumb, then assert no modal was raised.
        try await waitUntil {
            self.spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.mergeMessages" }
        }

        XCTAssertNil(
            model.errorMessage,
            "A push-hint refresh failure must not surface a modal Chat Error."
        )
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "A push-hint refresh failure must not spool a Chat.alertPresented breadcrumb."
        )
    }

    func testResyncSnapshotFailureIsSilent() async throws {
        // F5: the foreground resync's snapshot helpers must be silent — a failed
        // resume-time list/message snapshot degrades from per-channel health and
        // breadcrumbs, but never modals.
        let mergeArmed = AtomicCounter()
        let mergeFetches = AtomicCounter()
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-resync-silent-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path == "/api/v1/chat/conversations/web_conv_resyncfail/stream" {
                    return .hangingStream("", controller: HangingStream())
                }
                if method == "GET", path.hasSuffix("/messages") {
                    if Self.queryItems(from: request)["after"] == nil {
                        return .json(
                            #"{"conversation_id":"web_conv_resyncfail","messages":[{"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                        )
                    }
                    let fetch = mergeFetches.increment()
                    // Fail ONLY the resync's own message snapshot (the first fetch
                    // once armed). A later post-handover follow-reconnect catch-up (a
                    // separate steady-state path, out of F5's scope) succeeds, so it
                    // is not a confound for the resync-silence assertion.
                    if mergeArmed.value > 0, fetch == mergeArmed.value {
                        return .json(#"{"detail":"boom"}"#, statusCode: 500)
                    }
                    return .json(
                        #"{"conversation_id":"web_conv_resyncfail","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_resyncfail",
            spoolDirectory: spoolDirectory
        )
        await model.selectConversation("web_conv_resyncfail")
        try await waitUntil { model.messages.map(\.text) == ["Earlier"] }
        // Wait for the advisory follow-connect catch-up merge, then arm the NEXT
        // fetch (the resync's own message snapshot) to fail.
        try await waitUntil { mergeFetches.value >= 1 }
        model.errorMessage = nil
        mergeArmed.set(mergeFetches.value + 1)

        // Drive the coalesced foreground resync directly; its message snapshot fails.
        await model.reconnectLiveUpdates()
        try await waitUntil {
            self.spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.mergeMessages" }
        }

        XCTAssertNil(
            model.errorMessage,
            "A resync snapshot failure must not surface a modal Chat Error."
        )
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "A resync snapshot failure must not spool a Chat.alertPresented breadcrumb."
        )
    }

    func testReachabilityRecoveryRunsFullCoalescedResync() async throws {
        // §4.4: an unsatisfied→satisfied recovery runs the SAME coalesced resync as
        // foreground — a full list snapshot fetch — not just a bare loop restart.
        let monitor = StubPathMonitor(isSatisfied: true)
        let listFetchesAfterRecovery = AtomicCounter()
        let recovered = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("GET", "/api/v1/chat/conversations"):
                if recovered.value > 0 {
                    listFetchesAfterRecovery.increment()
                    return .json(
                        #"{"conversations":[{"conversation_id":"web_conv_recovered","last_message":"After recovery","last_timestamp":"2026-06-08T12:05:00Z","message_count":1}],"count":1}"#
                    )
                }
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path.hasSuffix("/stream") {
                    return .hangingStream("", controller: HangingStream())
                }
                if method == "GET", path.hasSuffix("/messages") {
                    return .json(
                        #"{"conversation_id":"web_conv_recovered","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_recovered",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01,
            pathMonitor: monitor
        )
        await model.bootstrap()

        // Drop then restore the path: the recovery must run the full coalesced
        // resync, which refetches the authoritative list (surfacing the row that
        // changed while offline), not merely restart the reconnect loops.
        monitor.setSatisfied(false)
        recovered.increment()
        monitor.setSatisfied(true)

        try await waitUntil(timeout: 6) {
            model.conversations.contains { $0.conversationID == "web_conv_recovered" }
        }
        XCTAssertGreaterThanOrEqual(
            listFetchesAfterRecovery.value,
            1,
            "Reachability recovery must fetch the authoritative list snapshot (full coalesced resync)."
        )
    }

    func testActiveSendResyncPreservesOptimisticPlaceholder() async throws {
        // A foregrounded reachability recovery must not merge a messages snapshot
        // while a send is actively streaming: `mergeNewMessages` drops every `local_`
        // row, which would delete the in-flight assistant placeholder the send task
        // is still rendering into (subsequent tokens would then target a removed
        // message and vanish until a final reload). The send owns its rendering and
        // reconciles its own history on completion, so `applyMessagesSnapshot`
        // no-ops here — completing the passive-resync guard the other steps already
        // apply (reconcileSuspendedSession / catchUpPersistedHistory / follow merge).
        let monitor = StubPathMonitor(isSatisfied: true)
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        let listFetches = AtomicCounter()
        let messagesFetches = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let method = request.httpMethod ?? "GET"
            switch (method, path) {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .hangingStream("", controller: HangingStream())
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_active","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_active/stream"):
                return .hangingStream(
                    """
                    event: turn_started
                    data: {"turn_id":"\(postedTurnID.value ?? "turn-active")","seq":0}

                    event: token
                    data: {"turn_id":"\(postedTurnID.value ?? "turn-active")","text":"partial","seq":1}

                    """,
                    controller: stream
                )
            case ("GET", "/api/v1/chat/conversations"):
                listFetches.increment()
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if method == "GET", path.hasSuffix("/messages") {
                    messagesFetches.increment()
                    return .json(
                        #"{"conversation_id":"web_conv_active","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_active",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01,
            pathMonitor: monitor
        )
        defer {
            stream.finish()
            model.cancelStream()
        }
        model.syncCoordinator.start()
        model.draftText = "Hi"
        await model.sendDraft()
        // Actively streaming: the send owns its transport (isSendActivelyStreaming).
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }
        let assistantBubbleID = try XCTUnwrap(
            model.messages.last(where: { $0.role == .assistant })?.id
        )
        XCTAssertTrue(assistantBubbleID.hasPrefix("local_"))

        let listBaseline = listFetches.value
        let messagesBaseline = messagesFetches.value
        // A reachability recovery drives the full coalesced resync while the send is
        // still streaming.
        monitor.setSatisfied(false)
        monitor.setSatisfied(true)

        // The resync ran (it refetched the authoritative list)...
        try await waitUntil(timeout: 6) { listFetches.value > listBaseline }
        // ...but no messages snapshot was merged, so the optimistic placeholder the
        // send is rendering into survives.
        XCTAssertEqual(
            messagesFetches.value,
            messagesBaseline,
            "applyMessagesSnapshot must no-op while a send is actively streaming."
        )
        XCTAssertTrue(
            model.messages.contains { $0.id == assistantBubbleID },
            "The in-flight assistant placeholder must survive an active-send resync."
        )
    }

    /// Shared assertions for the §4.3 background-suspend traces: the send's
    /// transport task is cancelled, the ActiveTurnSession (instance + cursor) is
    /// preserved, the generation bumped, the advisory follow/activity tasks were
    /// torn down (no fresh connect under a long backoff), and no user-facing
    /// error/alert/"Response stopped." leaked out.
    private func assertBackgroundSuspendedSend(
        model: ChatViewModel,
        sessionBefore: ActiveTurnSession,
        followGenerationBefore: Int,
        activityGenerationBefore: Int,
        followConnects: AtomicCounter,
        activityConnects: AtomicCounter,
        followBaseline: Int? = nil,
        activityBaseline: Int? = nil,
        cancelRequests: AtomicCounter,
        spoolDirectory: URL
    ) throws {
        let followFloor = followBaseline ?? followConnects.value
        let activityFloor = activityBaseline ?? activityConnects.value

        XCTAssertGreaterThan(
            model.syncCoordinator.followGeneration,
            followGenerationBefore,
            "A real background must bump the follow generation to fence the torn-down stream."
        )
        XCTAssertGreaterThan(
            model.syncCoordinator.activityGeneration,
            activityGenerationBefore,
            "A real background must bump the activity generation to fence the torn-down stream."
        )
        XCTAssertEqual(model.syncPresentation, .suspended)

        let sessionAfter = try XCTUnwrap(model.activeTurnSessionForTesting)
        XCTAssertTrue(
            sessionBefore === sessionAfter,
            "The ActiveTurnSession must survive the background suspend (same instance)."
        )
        XCTAssertEqual(sessionAfter.lastAppliedSeq, sessionBefore.lastAppliedSeq)

        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(
            model.messages.contains { $0.text == "Response stopped." },
            "Background suspend must not run cancelStream's 'Response stopped.' semantics."
        )
        XCTAssertTrue(
            spooledReports(in: spoolDirectory).allSatisfy { $0.componentName != "Chat.alertPresented" },
            "Background suspend must not spool a Chat.alertPresented breadcrumb."
        )

        XCTAssertEqual(
            cancelRequests.value,
            0,
            "Background suspend must not fire a queued stop-cancel POST."
        )

        // The follow/activity tasks were cancelled by cancelStreams(); with a long
        // reconnect backoff, any fresh connect would be attributable to a restart
        // that must NOT happen on background. Streaming stopped for the same reason.
        XCTAssertFalse(model.isStreaming)
        XCTAssertEqual(
            followConnects.value,
            followFloor,
            "Backgrounding must not open a new follow stream (advisory teardown)."
        )
        XCTAssertEqual(
            activityConnects.value,
            activityFloor,
            "Backgrounding must not open a new activity stream (advisory teardown)."
        )
    }

    /// F1: after the suspended `runSendTurn` task has run its ASYNCHRONOUS
    /// cancellation aftermath to completion (POST-in-flight throw, or a mid-stream
    /// interrupted return), the `ActiveTurnSession` must still be the same
    /// preserved instance, with no user-cancel side effects: no error surfaced, no
    /// "Response stopped." bubble, no queued stop-cancel POST, no alert breadcrumb.
    private func assertSuspendedSendSurvivesAftermath(
        model: ChatViewModel,
        sessionBefore: ActiveTurnSession,
        cancelRequests: AtomicCounter,
        spoolDirectory: URL
    ) throws {
        let sessionAfter = try XCTUnwrap(
            model.activeTurnSessionForTesting,
            "The ActiveTurnSession must survive the asynchronous suspend aftermath."
        )
        XCTAssertTrue(
            sessionBefore === sessionAfter,
            "The suspend aftermath must not replace the preserved ActiveTurnSession."
        )
        XCTAssertNil(
            model.errorMessage,
            "The suspend aftermath must not surface an error modal."
        )
        XCTAssertFalse(
            model.messages.contains { $0.text == "Response stopped." },
            "The suspend aftermath must not run cancelStream's 'Response stopped.' semantics."
        )
        XCTAssertFalse(
            model.messages.contains { $0.status == .failed },
            "The suspend aftermath must not mark the bubble failed."
        )
        XCTAssertEqual(
            cancelRequests.value, 0,
            "The suspend aftermath must not fire a queued stop-cancel POST."
        )
        XCTAssertTrue(
            spooledReports(in: spoolDirectory).allSatisfy { $0.componentName != "Chat.alertPresented" },
            "The suspend aftermath must not spool a Chat.alertPresented breadcrumb."
        )
    }

    func testShouldRenderThreadKeepsListMountedOnceActive() {
        // Offscreen background launch (never been active): keep the message stack out
        // of the tree so a background scene-update can't run the expensive layout.
        let model = makeViewModel(conversationID: "web_conv_render")
        XCTAssertFalse(model.shouldRenderThread(isActive: false, hasMountedBefore: false))
        // First foregrounding realizes the thread.
        XCTAssertTrue(model.shouldRenderThread(isActive: true, hasMountedBefore: false))
        // Once realized, it stays mounted across later background transitions, so
        // backgrounding does not tear it down (a suspend-watchdog hazard).
        XCTAssertTrue(model.shouldRenderThread(isActive: false, hasMountedBefore: true))
        XCTAssertTrue(model.shouldRenderThread(isActive: true, hasMountedBefore: true))
    }

    func testLiveFollowMergeKeepsRunningBubbleAfterNewlyFetchedOlderRow() async throws {
        // When a follow turn drops mid-stream and the catch-up merge fetches an
        // older row for that turn (e.g. its own user prompt persisted after our
        // last held message), the still-running live bubble must stay ordered LAST
        // — the in-progress reply can't render before the prompt it answers.
        let streamConnects = AtomicCounter()
        let promptPersisted = AtomicCounter()
        let leg1 = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        #"{"conversation_id":"web_conv_order","messages":[{"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                if promptPersisted.value == 0 {
                    return .json(
                        #"{"conversation_id":"web_conv_order","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                // The running turn's own user prompt, persisted with a timestamp
                // older than the live reply bubble's creation time.
                return .json(
                    #"{"conversation_id":"web_conv_order","messages":[{"internal_id":2,"role":"user","content":"Question","timestamp":"2026-06-08T12:05:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                if streamConnects.increment() == 1 {
                    return .hangingStream(
                        "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\"Reply\",\"seq\":5}\n\n",
                        controller: leg1
                    )
                }
                return .hangingStream("", controller: HangingStream())
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_order",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_order")
        try await waitUntil { model?.messages.contains { $0.text == "Reply" } == true }

        // The prompt persists, then the stream drops, triggering the catch-up merge.
        promptPersisted.increment()
        leg1.finish()

        // The live reply must come AFTER the freshly fetched prompt, not before it.
        try await waitUntil { model?.messages.map(\.text) == ["Earlier", "Question", "Reply"] }
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "Question", "Reply"])
        XCTAssertEqual(model?.messages.last?.id, "local_follow_turn-x")

        model = nil
    }

    func testLiveFollowBubbleSurvivesEmptyThreadCatchUp() async throws {
        // A brand-new conversation has no `msg_` rows, so a catch-up merge takes
        // the full-reload (`loadMessages`) branch. That full `messages =` replace
        // must still preserve a streaming live bubble, or a drop before the first
        // reply persists wipes the partial text and dangles the mapping.
        let streamConnects = AtomicCounter()
        let leg1 = HangingStream()
        let leg2 = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                // The reply never persists during the test: every load is empty, so
                // the merge always takes the latestPersistedTimestamp()==nil branch.
                return .json(
                    #"{"conversation_id":"web_conv_empty","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                let n = streamConnects.increment()
                return .hangingStream(
                    n == 1
                        ? "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\"Reply\",\"seq\":5}\n\n"
                        : "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\" more\",\"seq\":6}\n\n",
                    controller: n == 1 ? leg1 : leg2
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_empty",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_empty")
        try await waitUntil { model?.messages.contains { $0.text == "Reply" } == true }

        // Drop before any reply persists: the catch-up takes the full-reload branch.
        leg1.finish()

        // The bubble survives the empty-thread reload and resumes streaming into the
        // SAME bubble — "Reply more", not a fresh " more".
        try await waitUntil {
            model?.messages.first { $0.id == "local_follow_turn-x" }?.text == "Reply more"
        }
        XCTAssertEqual(
            model?.messages.first { $0.id == "local_follow_turn-x" }?.text,
            "Reply more"
        )

        leg2.finish()
        model = nil
    }

    func testLiveFollowResumesFromLastSeqAndTailsAfter410() async throws {
        // A follow reconnect during a running turn resumes from highestAppliedSeq+1
        // so frames produced during the drop aren't skipped. If that resume cursor
        // has rotated out of the hub buffer (410), the next attempt tails from the
        // head instead of re-requesting the gone seq forever.
        let streamConnects = AtomicCounter()
        let resumedFromSix = AtomicCounter()
        let tailedAfter410 = AtomicCounter()
        let leg1 = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    #"{"conversation_id":"web_conv_resume_seq","messages":[{"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                let n = streamConnects.increment()
                let fromSeq = Self.queryItems(from: request)["from_seq"]
                if n == 1 {
                    // Initial connect tails from head, then streams a token at seq 5.
                    return .hangingStream(
                        "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\"Hello\",\"seq\":5}\n\n",
                        controller: leg1
                    )
                }
                if n == 2 {
                    // Reconnect resumes from the last applied seq + 1, then 410s.
                    if fromSeq == "6" { resumedFromSix.increment() }
                    return .json(#"{"detail":"gone"}"#, statusCode: 410)
                }
                // After the 410 the next attempt tails from the head.
                if fromSeq == "-1" { tailedAfter410.increment() }
                return .hangingStream("", controller: HangingStream())
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_resume_seq",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_resume_seq")
        try await waitUntil { model?.messages.contains { $0.text == "Hello" } == true }

        // Drop the first leg; the reconnect should resume from seq 6, get a 410,
        // then tail from the head on the following attempt.
        leg1.finish()
        try await waitUntil { streamConnects.value >= 3 }
        XCTAssertGreaterThanOrEqual(resumedFromSix.value, 1)
        XCTAssertGreaterThanOrEqual(tailedAfter410.value, 1)

        model = nil
    }

    func testConcurrentConversationSwitchDoesNotClobberWithStaleLoad() async throws {
        // updateSelection spawns `Task { await selectConversation(id) }`, so two
        // quick taps run two selectConversation calls that do NOT cancel each
        // other. If the first's loadMessages is still in flight when the second
        // finishes, the first's resumed full-reload must NOT assign its (stale)
        // thread over the conversation the user actually landed on.
        let aLoadFetch = AtomicCounter()
        let aHistory = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/conversations/conv_a/messages") {
                // Hang A's initial load so it is still pending when B finishes.
                aLoadFetch.increment()
                return .hangingStream("", controller: aHistory)
            }
            if request.httpMethod == "GET", path.hasSuffix("/conversations/conv_b/messages") {
                return .json(
                    #"{"conversation_id":"conv_b","messages":[{"internal_id":9,"role":"assistant","content":"BReply","timestamp":"2026-06-08T13:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                return .hangingStream("", controller: HangingStream())
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(conversationID: nil)
        // Tap A: its load hangs.
        let selectA = Task { await model?.selectConversation("conv_a") }
        try await waitUntil { aLoadFetch.value >= 1 }
        // Tap B before A finished loading: B loads cleanly.
        let selectB = Task { await model?.selectConversation("conv_b") }
        try await waitUntil { model?.messages.contains { $0.text == "BReply" } == true }

        // A's load now resumes with its (stale) thread; the guard must drop it.
        aHistory.finish(
            appending: #"{"conversation_id":"conv_a","messages":[{"internal_id":1,"role":"user","content":"AStale","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
        )
        _ = await selectA.value
        _ = await selectB.value

        // Bounded settle: without the guard A's resumed load replaces messages with
        // ["AStale"] within a few ms; with it the load returns early.
        let deadline = Date().addingTimeInterval(1.0)
        while Date() < deadline, model?.messages.contains(where: { $0.text == "AStale" }) == false {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertFalse(model?.messages.contains { $0.text == "AStale" } == true)
        XCTAssertEqual(model?.messages.map(\.text), ["BReply"])

        model = nil
    }

    func testSendGiveUpSuppressesDuplicateFollowRender() async throws {
        // When a send exhausts its resumes and recovers via history reload (no
        // turn_ended), the turn is marked ended so the always-on follow loop does
        // NOT also render its tail into a separate local_follow_ bubble — which
        // would duplicate the reply the reload surfaced as a msg_ row.
        let postedTurnID = AtomicString()
        let liveFollow = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/turns" {
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let turnID = body["turn_id"] as? String {
                    postedTurnID.set(turnID)
                }
                return .json(
                    #"{"turn_id":"ignored","conversation_id":"web_conv_giveup","first_seq":0}"#
                )
            }
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    #"{"conversation_id":"web_conv_giveup","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Partial reply","timestamp":"2026-06-08T12:00:01Z"}],"count":2,"total_messages":2,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "POST", path == "/api/v1/chat/ack" {
                return .json("{}")
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                let follow = Self.queryItems(from: request)["follow"] == "true"
                if follow {
                    // The always-on live-follow stream: held open until the test
                    // delivers a tail token after the send has given up.
                    return .hangingStream("", controller: liveFollow)
                }
                // The send subscription keeps dropping with no turn_ended.
                return .droppedStream(
                    "event: text\ndata: {\"content\":\"Partial\",\"seq\":1}\n\n"
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_giveup",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 1,
            streamResumeLivenessSeconds: 60
        )
        await model?.selectConversation("web_conv_giveup")
        model?.draftText = "Hi"
        await model?.sendDraft()

        // The send gives up and reloads history (the partial reply is a msg_ row).
        try await waitUntil {
            model?.isStreaming == false && model?.messages.contains { $0.text == "Partial reply" } == true
        }

        // Deliver a follow-stream tail token for the SAME turn after the give-up.
        let turnID = try XCTUnwrap(postedTurnID.value)
        liveFollow.finish(
            appending: "event: text\ndata: {\"turn_id\":\"\(turnID)\",\"content\":\" tail\",\"seq\":9}\n\n"
        )

        // Bounded settle: the suppressed turn must never spawn a local_follow bubble.
        let deadline = Date().addingTimeInterval(1.0)
        while Date() < deadline,
              model?.messages.contains(where: { $0.id == "local_follow_\(turnID)" }) == false {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertFalse(model?.messages.contains { $0.id == "local_follow_\(turnID)" } == true)
        XCTAssertEqual(model?.messages.map(\.text), ["Hi", "Partial reply"])

        liveFollow.finish()
        model = nil
    }

    func testLiveFollowReconcilesByTurnIdAndHoldsUntilItsRowPersists() async throws {
        // With turn_id on persisted rows, a finished follow bubble is held until
        // ITS canonical row arrives — a merge whose delta carries some OTHER
        // turn's row must not drop it, and it reconciles precisely once its own
        // persisted row (matched by turn_id) shows up.
        let streamConnects = AtomicCounter()
        let phase = AtomicCounter() // 0: empty; >=1: unrelated row; >=2: this turn's reply
        let leg1 = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                if Self.queryItems(from: request)["after"] == nil {
                    return .json(
                        #"{"conversation_id":"web_conv_recon","messages":[{"internal_id":1,"role":"user","content":"Earlier","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                if phase.value >= 2 {
                    return .json(
                        #"{"conversation_id":"web_conv_recon","messages":[{"internal_id":3,"role":"assistant","content":"X reply","turn_id":"turn-x","timestamp":"2026-06-08T12:06:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                if phase.value >= 1 {
                    return .json(
                        #"{"conversation_id":"web_conv_recon","messages":[{"internal_id":2,"role":"assistant","content":"Other reply","turn_id":"turn-y","timestamp":"2026-06-08T12:05:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                return .json(
                    #"{"conversation_id":"web_conv_recon","messages":[],"count":0,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "POST", path == "/api/v1/chat/ack" {
                return .json("{}")
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                if streamConnects.increment() == 1 {
                    return .hangingStream(
                        "event: text\ndata: {\"turn_id\":\"turn-x\",\"content\":\"Hello\",\"seq\":5}\n\n",
                        controller: leg1
                    )
                }
                // Clean EOF on each later connect so the loop reconnects and
                // re-runs the catch-up merge as the persisted phases advance.
                return .text("")
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_recon",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.selectConversation("web_conv_recon")
        try await waitUntil { model?.messages.contains { $0.text == "Hello" } == true }

        // turn-x ends; an unrelated turn-y row is now the only thing persisted.
        phase.increment()
        leg1.finish(
            appending: "event: turn_ended\ndata: {\"turn_id\":\"turn-x\",\"status\":\"complete\",\"seq\":6}\n\n"
        )

        // The completed bubble is HELD: the merge sees turn-y, not turn-x's reply.
        try await waitUntil { model?.messages.contains { $0.text == "Other reply" } == true }
        XCTAssertEqual(model?.messages.first { $0.id == "local_follow_turn-x" }?.text, "Hello")
        XCTAssertEqual(model?.messages.first { $0.id == "local_follow_turn-x" }?.status, .complete)
        XCTAssertFalse(model?.messages.contains { $0.text == "X reply" } == true)

        // turn-x's own reply now persists; the next catch-up reconciles by turn_id.
        phase.increment()
        try await waitUntil { model?.messages.contains { $0.text == "X reply" } == true }
        XCTAssertFalse(model?.messages.contains { $0.id == "local_follow_turn-x" } == true)
        XCTAssertEqual(model?.messages.map(\.text), ["Earlier", "Other reply", "X reply"])

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

        // Tear the send down before returning: `finish()` closes the stream without
        // a turn_ended, which would otherwise leave the send's resume loop running
        // (resubscribing on a backoff) into the next test and contend for the shared
        // MainActor / URLProtocol loader in the app-hosted bundle.
        stream.finish()
        model.cancelStream()
        try await waitUntil { !model.isStreaming }
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

    func testApplyRouteProcessesRepeatedPromptForExplicitNewRequests() async throws {
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

        await model.applyRoute(conversationID: nil, initialPrompt: "Again", newConversationRequestID: "request-1")
        try await waitUntil { !model.isStreaming }
        await model.applyRoute(conversationID: nil, initialPrompt: "Again", newConversationRequestID: "request-2")
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(sentPrompts, ["Again", "Again"])
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

    func testIndicatorBackToLiveAfterSuccessfulReconnect() async throws {
        // Both channels connect and stay open (hanging), so the coordinator's
        // derived presentation reaches `.live`. The follow stream then drops
        // cleanly; presentation leaves `.live`, and the auto-reconnect brings a
        // fresh follow connect back to `.live`.
        let followController = HangingStream()
        let activityController = HangingStream()
        let followConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path == "/api/v1/chat/activity/stream" {
                return .hangingStream("", controller: activityController)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                let n = followConnects.increment()
                if n == 1 {
                    // First connect drops cleanly (empty event stream).
                    return .text("")
                }
                return .hangingStream("", controller: followController)
            }
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    #"{"conversation_id":"web_conv_live","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path == "/api/v1/profiles" {
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/confirmations/pending" {
                return .json(#"{"confirmations":[]}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_live",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.01
        )
        await model?.bootstrap()
        await model?.selectConversation("web_conv_live")

        // The first follow connect dropped cleanly; the auto-reconnect eventually
        // establishes a held follow stream and, with the held activity stream,
        // drives presentation to `.live`.
        try await waitUntil(timeout: 6) { model?.syncPresentation == .live }
        XCTAssertEqual(model?.syncPresentation, .live)

        followController.finish()
        activityController.finish()
        model = nil
    }

    func testActivityStreamConnectsWhileBootstrapFetchesAreStillOutstanding() async throws {
        // Regression: `bootstrap` used to start the activity stream only after
        // awaiting `loadProfiles` and `refreshConversations`. In production the
        // conversation-list fetch alone runs a ~2s median round trip, so every cold
        // launch spent seconds with no stream even attempted — and the indicator
        // showed a connection warning the whole time. The activity stream depends on
        // neither fetch, so it must connect while they are still in flight.
        // Stale stored conversation → launch opens a generated draft, so liveness
        // rests on the activity stream alone (no follow stream to establish).
        storeLastConversation("web_conv_stale", activeSecondsAgo: 16 * 60)
        let activityController = HangingStream()
        let conversationsController = HangingStream()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path == "/api/v1/chat/activity/stream" {
                return .hangingStream("", controller: activityController)
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                // Never completes: bootstrap stays parked inside `refreshConversations`.
                return .hangingStream("", controller: conversationsController)
            }
            if request.httpMethod == "GET", path == "/api/v1/profiles" {
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/confirmations/pending" {
                return .json(#"{"confirmations":[]}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(conversationID: nil)
        let bootstrapTask = Task { [model] in await model?.bootstrap() }

        // A launch draft has no server-side row, so liveness rests on the
        // account-global activity stream alone. It connects despite the list fetch
        // never returning, and the indicator reaches `.live` rather than sitting on a
        // warning for the duration of bootstrap.
        try await waitUntil(timeout: 6) { model?.syncPresentation == .live }
        XCTAssertEqual(model?.syncPresentation, .live)

        bootstrapTask.cancel()
        conversationsController.finish()
        activityController.finish()
        _ = await bootstrapTask.value
        model = nil
    }

    func testIndicatorNotLiveAfterCleanEOFDrop() async throws {
        // A held (connected) follow stream that then closes cleanly (empty EOF)
        // must leave the derived presentation off `.live`: a clean EOF is still an
        // involuntary drop.
        let followController = HangingStream()
        let activityController = HangingStream()
        let followConnects = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path == "/api/v1/chat/activity/stream" {
                return .hangingStream("", controller: activityController)
            }
            if request.httpMethod == "GET", path.hasSuffix("/stream") {
                let n = followConnects.increment()
                if n == 1 {
                    return .hangingStream("", controller: followController)
                }
                // Later reconnects hang so the drop below is observable before a
                // fresh connect flips the indicator back.
                return .hangingStream("", controller: HangingStream())
            }
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(
                    #"{"conversation_id":"web_conv_eof","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"conversations":[],"count":0}"#)
            }
            if request.httpMethod == "GET", path == "/api/v1/profiles" {
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            }
            if request.httpMethod == "GET", path == "/api/v1/chat/confirmations/pending" {
                return .json(#"{"confirmations":[]}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        var model: ChatViewModel? = makeViewModel(
            conversationID: "web_conv_eof",
            liveReconnectInitialDelaySeconds: 10,
            liveReconnectMaxDelaySeconds: 10
        )
        await model?.bootstrap()
        await model?.selectConversation("web_conv_eof")

        try await waitUntil(timeout: 6) { model?.syncPresentation == .live }

        // Close the held follow stream cleanly (empty EOF). The reconnect backoff
        // is long, so the indicator stays off `.live` while we observe.
        followController.finish()
        try await waitUntil(timeout: 4) { model?.syncPresentation != .live }
        XCTAssertNotEqual(model?.syncPresentation, .live)

        activityController.finish()
        model = nil
    }

    func testPresentationUnaffectedByStaleGenerationFollowCallback() async throws {
        // A follow callback tagged with a superseded generation must not degrade
        // the derived presentation: the coordinator's reducer rejects stale-
        // generation events. Drive the coordinator directly for determinism.
        let (model, coordinator) = makeCoordinatorBackedModel()

        let followGeneration = coordinator.followGeneration
        coordinator.apply(.followConnected(generation: followGeneration))
        coordinator.apply(.activityConnected(generation: coordinator.activityGeneration))
        XCTAssertEqual(model.syncPresentation, .live)

        coordinator.bumpFollowGeneration()
        // A late drop from the old follow generation is rejected; presentation holds.
        coordinator.apply(.followDropped(generation: followGeneration, cleanEOF: true))
        XCTAssertEqual(model.syncPresentation, .live)
    }

    func testBootstrappedViewModelAdoptsLatchedAuthRequiredPresentation() async {
        // A live ChatViewModel bootstrapped AFTER `authRequired` latched must
        // present `.authRequired`: its coordinator learns the state through the
        // observer's synchronous current-state delivery at activation, not only
        // on the next transition.
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        authManager.markAuthRequired()

        let model = track(ChatViewModel(
            authManager: authManager,
            conversationID: "web_conv_latched_auth",
            errorReporter: ErrorReporter(spoolDirectory: nil),
            pathMonitor: StubPathMonitor(isSatisfied: true)
        ))

        XCTAssertNotEqual(model.syncPresentation, .authRequired)

        await model.bootstrap()

        XCTAssertEqual(model.syncPresentation, .authRequired)
    }

    func testSuccessfulReauthenticationRestartsStreamsThroughAuthObserver() async throws {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        authManager.markAuthRequired()
        let model = track(ChatViewModel(
            authManager: authManager,
            conversationID: "web_conv_reauthenticated",
            errorReporter: ErrorReporter(spoolDirectory: nil),
            pathMonitor: StubPathMonitor(isSatisfied: true)
        ))
        await model.bootstrap()
        XCTAssertEqual(model.syncPresentation, .authRequired)

        KeychainHelper.save(key: "fa_api_token", string: "replacement-api-token")
        KeychainHelper.save(key: "fa_refresh_token", string: "replacement-refresh-token")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
        let followStream = HangingStream()
        let activityStream = HangingStream()
        let followRestarted = expectation(description: "follow stream restarted")
        let activityRestarted = expectation(description: "activity stream restarted")
        ChatMockBackendURLProtocol.respond { request in
            switch request.url?.path {
            case "/api/auth/refresh":
                return .json(#"{"api_token":"fresh","refresh_token":"fresh-refresh","expires_in":7200}"#)
            case "/api/v1/chat/conversations/web_conv_reauthenticated/stream":
                followRestarted.fulfill()
                return .hangingStream("", controller: followStream)
            case "/api/v1/chat/activity/stream":
                activityRestarted.fulfill()
                return .hangingStream("", controller: activityStream)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        try await authManager.refreshIfNeeded(force: true)
        await fulfillment(of: [followRestarted, activityRestarted], timeout: 2)

        XCTAssertFalse(authManager.authRequired)
    }

    /// A view model whose coordinator is seeded with a satisfied stub monitor, so
    /// reducer-driven presentation assertions are deterministic.
    private func makeCoordinatorBackedModel() -> (ChatViewModel, SyncCoordinator) {
        let model = makeViewModel(conversationID: "web_conv_stale")
        return (model, model.syncCoordinator)
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

    // MARK: - Pasted images

    /// Formats the backend accepts pass through the paste pipeline untouched;
    /// this covers the declaration-order guarantee that the PNG representation
    /// wins over the generic transcode fallback for PNG pasteboard data.
    func testPastedImagePassesSupportedFormatThroughUnchanged() throws {
        let pngData = try XCTUnwrap(solidImage(size: CGSize(width: 4, height: 4)).pngData())

        let pasted = PastedChatImage(data: pngData, mimeType: "image/png", filenameExtension: "png")

        XCTAssertEqual(pasted.data, pngData)
        XCTAssertTrue(ChatConstants.allowedAttachmentMIMETypes.contains(pasted.mimeType))
    }

    func testPastedImageTranscodesDecodableDataToJPEG() throws {
        let pngData = try XCTUnwrap(solidImage(size: CGSize(width: 4, height: 4)).pngData())

        let pasted = try PastedChatImage.transcodedToJPEG(data: pngData)

        XCTAssertEqual(pasted.mimeType, "image/jpeg")
        XCTAssertEqual(pasted.filenameExtension, "jpg")
        XCTAssertNotNil(UIImage(data: pasted.data))
        XCTAssertTrue(ChatConstants.allowedAttachmentMIMETypes.contains(pasted.mimeType))
    }

    func testPastedImageTranscodeRejectsUndecodableData() {
        XCTAssertThrowsError(try PastedChatImage.transcodedToJPEG(data: Data("not an image".utf8)))
    }

    private func solidImage(size: CGSize) -> UIImage {
        UIGraphicsImageRenderer(size: size).image { context in
            UIColor.systemBlue.setFill()
            context.fill(CGRect(origin: .zero, size: size))
        }
    }

    func testSharedAttachmentBatchUploadsIntoDraftAttachments() async throws {
        let fileURL = FileManager.default.temporaryDirectory.appendingPathComponent("shared-upload.txt")
        try Data("shared".utf8).write(to: fileURL)

        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/attachments/upload"):
                let body = String(data: request.bodyData, encoding: .utf8)
                XCTAssertTrue(body?.contains(#"filename="shared-upload.txt""#) == true)
                XCTAssertTrue(body?.contains("Content-Type: text/plain") == true)
                XCTAssertTrue(body?.contains("shared") == true)
                return .json(
                    """
                    {
                      "attachment_id": "44444444-4444-4444-4444-444444444444",
                      "filename": "shared-upload.txt",
                      "content_type": "text/plain",
                      "size": 6,
                      "url": "/api/attachments/44444444-4444-4444-4444-444444444444"
                    }
                    """
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_shared_upload")

        await model.addSharedAttachments(SharedAttachmentBatch(id: "batch-1", fileURLs: [fileURL], importErrors: []))
        try await waitUntil { model.draftAttachments.first?.uploadState == .uploaded }

        let attachment = try XCTUnwrap(model.draftAttachments.first)
        XCTAssertEqual(attachment.attachmentID, "44444444-4444-4444-4444-444444444444")
        XCTAssertEqual(attachment.name, "shared-upload.txt")
        XCTAssertEqual(attachment.mimeType, "text/plain")
        XCTAssertTrue(model.canSendDraft)
        XCTAssertNotNil(model.composerFocusRequestID)
    }

    func testSharedAttachmentBatchStopsWhenDraftChangesDuringUpload() async throws {
        let importRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("SharedAttachmentImports", isDirectory: true)
        let firstDirectory = importRoot.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let secondDirectory = importRoot.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: firstDirectory, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: secondDirectory, withIntermediateDirectories: true)
        let firstURL = firstDirectory.appendingPathComponent("shared-first.txt")
        let secondURL = secondDirectory.appendingPathComponent("shared-second.txt")
        try Data("first".utf8).write(to: firstURL)
        try Data("second".utf8).write(to: secondURL)
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }
        let firstUpload = HangingStream()
        defer {
            firstUpload.finish()
        }
        let uploadCount = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/attachments/upload"):
                let count = uploadCount.increment()
                if count > 1 {
                    XCTFail("Shared batch should stop before uploading the second file")
                }
                return ChatMockResponse(
                    statusCode: 200,
                    data: Data(),
                    headers: ["Content-Type": "application/json"],
                    hangingStream: firstUpload
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let model = makeViewModel(conversationID: "web_conv_shared_switch")

        let importTask = Task {
            await model.addSharedAttachments(
                SharedAttachmentBatch(id: "batch-switch", fileURLs: [firstURL, secondURL], importErrors: [])
            )
        }
        try await waitUntil { uploadCount.value == 1 }
        try await waitUntil { model.draftAttachments.first?.uploadState == .uploading }

        model.startNewConversation()
        let newConversationID = model.conversationID
        XCTAssertTrue(model.draftAttachments.isEmpty)
        firstUpload.finish(
            appending: """
            {
              "attachment_id": "55555555-5555-5555-5555-555555555555",
              "filename": "shared-first.txt",
              "content_type": "text/plain",
              "size": 5,
              "url": "/api/attachments/55555555-5555-5555-5555-555555555555"
            }
            """
        )
        await importTask.value

        XCTAssertEqual(model.conversationID, newConversationID)
        XCTAssertTrue(model.draftAttachments.isEmpty)
        XCTAssertEqual(uploadCount.value, 1)
        XCTAssertFalse(FileManager.default.fileExists(atPath: secondURL.path))
    }

    func testSharedAttachmentBatchSurfacesImportErrors() async {
        let model = makeViewModel(conversationID: "web_conv_shared_error")

        await model.addSharedAttachments(
            SharedAttachmentBatch(
                id: "batch-errors",
                fileURLs: [],
                importErrors: ["Could not import broken.pdf. The file could not be opened."]
            )
        )

        XCTAssertTrue(model.draftAttachments.isEmpty)
        XCTAssertEqual(
            model.errorMessage,
            """
            Some shared files could not be imported.
            Could not import broken.pdf. The file could not be opened.
            """
        )
        XCTAssertNotNil(model.composerFocusRequestID)
    }

    func testRemovingSharedAttachmentDeletesTemporaryImport() async throws {
        let importDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("SharedAttachmentImports", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: importDirectory, withIntermediateDirectories: true)
        let fileURL = importDirectory.appendingPathComponent("shared-remove.txt")
        try Data("shared".utf8).write(to: fileURL)

        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/attachments/upload"):
                return .json(
                    """
                    {
                      "attachment_id": "55555555-5555-5555-5555-555555555555",
                      "filename": "\(fileURL.lastPathComponent)",
                      "content_type": "text/plain",
                      "size": 6,
                      "url": "/api/attachments/55555555-5555-5555-5555-555555555555"
                    }
                    """
                )
            case ("DELETE", "/api/attachments/55555555-5555-5555-5555-555555555555"):
                return .json(#"{"message":"deleted"}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_shared_remove")
        await model.addSharedAttachments(SharedAttachmentBatch(id: "batch-remove", fileURLs: [fileURL], importErrors: []))
        try await waitUntil { model.draftAttachments.first?.uploadState == .uploaded }
        XCTAssertTrue(FileManager.default.fileExists(atPath: fileURL.path))

        let attachment = try XCTUnwrap(model.draftAttachments.first)
        await model.removeDraftAttachment(attachment)

        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: importDirectory.path))
        XCTAssertTrue(model.draftAttachments.isEmpty)
    }

    func testSendingSharedAttachmentDeletesTemporaryImport() async throws {
        let importDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("SharedAttachmentImports", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: importDirectory, withIntermediateDirectories: true)
        let fileURL = importDirectory.appendingPathComponent("shared-send.txt")
        try Data("shared".utf8).write(to: fileURL)

        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/attachments/upload"):
                return .json(
                    """
                    {
                      "attachment_id": "66666666-6666-6666-6666-666666666666",
                      "filename": "\(fileURL.lastPathComponent)",
                      "content_type": "text/plain",
                      "size": 6,
                      "url": "/api/attachments/66666666-6666-6666-6666-666666666666"
                    }
                    """
                )
            case ("POST", "/api/v1/chat/turns"):
                return .json(#"{"turn_id":"turn-shared-send","conversation_id":"web_conv_shared_send","first_seq":0,"already_complete":true}"#)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_shared_send/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_shared_send",
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

        let model = makeViewModel(conversationID: "web_conv_shared_send")
        await model.addSharedAttachments(SharedAttachmentBatch(id: "batch-send", fileURLs: [fileURL], importErrors: []))
        try await waitUntil { model.draftAttachments.first?.uploadState == .uploaded }
        XCTAssertTrue(FileManager.default.fileExists(atPath: fileURL.path))

        await model.sendDraft()

        XCTAssertFalse(FileManager.default.fileExists(atPath: fileURL.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: importDirectory.path))
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

        // The chip stays, and the failure surfaces inline on the chip (§4.5), not
        // as a modal.
        XCTAssertEqual(model.draftAttachments.map(\.id), [attachment.id])
        XCTAssertNil(model.errorMessage, "a remove failure must not modal")
        XCTAssertTrue(
            model.draftAttachments.first?.errorMessage?.contains("Could not remove") == true
        )
    }

    func testRemoveUploadingDraftAttachmentKeepsAttachmentUntilUploadFinishes() async {
        let model = makeViewModel(conversationID: "web_conv_uploading_remove")
        let attachment = makeAttachment(uploadState: .uploading)
        model.draftAttachments = [attachment]

        await model.removeDraftAttachment(attachment)

        XCTAssertEqual(model.draftAttachments.map(\.id), [attachment.id])
        XCTAssertNil(model.errorMessage, "the not-yet-uploaded guard surfaces inline, not a modal")
        XCTAssertEqual(
            model.draftAttachments.first?.errorMessage,
            "Wait for upload to finish before removing."
        )
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

    func testConfirmFailureSurfacesInlineOnCardNotModal() async throws {
        // A failed tool confirmation surfaces on the confirmation card's own
        // `errorMessage` (its point-of-action anchor), never a modal (§4.5), and
        // emits a `Chat.inlineErrorPresented` breadcrumb tagged operation=confirm_tool.
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/confirm_tool"):
                return .json(#"{"detail":"expired token"}"#, statusCode: 401)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let confirmation = ChatPendingConfirmation(
            requestID: "req-fail",
            toolName: "calendar",
            toolCallID: "call-fail",
            confirmationPrompt: "Approve?",
            args: [:],
            createdAt: nil,
            expiresAt: nil,
            timeoutSeconds: 60,
            timeRemainingSeconds: 45
        )
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-confirm-fail-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_confirm_fail",
            spoolDirectory: spoolDirectory
        )
        model.pendingConfirmations = [confirmation]

        await model.confirm(confirmation, approved: true)

        XCTAssertNil(model.errorMessage, "a confirm failure must not modal")
        XCTAssertNotNil(
            model.pendingConfirmations.first?.errorMessage,
            "the failure surfaces on the confirmation card"
        )
        let reports = try await waitForSpooledReports(
            component: "Chat.inlineErrorPresented",
            in: spoolDirectory
        )
        XCTAssertEqual(reports.first?.extraData?["operation"], "confirm_tool")
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" }
        )
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
        let model = track(ChatViewModel(
            authManager: authManager,
            conversationID: "web_conv_breadcrumb",
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 1,
            streamResumeLivenessSeconds: 60,
            errorReporter: reporter
        ))
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        // Filter by component: the always-on sync breadcrumbs (reachability,
        // presentation, stream connect/restart) also spool here, so grabbing the
        // first report is racy.
        let streamDropReports = try await waitForSpooledReports(
            component: "Chat.streamDrop", in: spoolDirectory
        )
        let payload = try XCTUnwrap(streamDropReports.first)
        let extra = try XCTUnwrap(payload.extraData)
        XCTAssertEqual(payload.componentName, "Chat.streamDrop")
        XCTAssertEqual(extra["phase"], "send-subscribe")
        XCTAssertEqual(extra["outcome"], "interrupted")
        // The decisive diagnostic field: a connection-loss drop, not a timeout or
        // an app-side cancel.
        XCTAssertEqual(extra["url_error_code"], "networkConnectionLost")
    }

    func testFatalStreamErrorEmitsInlineErrorPresentedBreadcrumb() async throws {
        // The fatal subscribe path now surfaces inline at the point of action, not
        // the shared modal (§4.5): it emits exactly one reason-tagged
        // `Chat.inlineErrorPresented` breadcrumb and NO `Chat.alertPresented`, so
        // the inline vs modal popup rates stay measurable.
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-alert-fatal","conversation_id":"web_conv_alert_fatal","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    // A 422 is a genuinely fatal (non-auth) subscribe error that
                    // surfaces inline; a 401/403 would route to the auth flow (F3).
                    return .json(#"{"detail":"unprocessable"}"#, statusCode: 422)
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-alert-stream-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_alert_fatal",
            spoolDirectory: spoolDirectory
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        let reports = try await waitForSpooledReports(
            component: "Chat.inlineErrorPresented",
            in: spoolDirectory
        )
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports.first?.extraData?["reason"], "action_failed")
        XCTAssertEqual(reports.first?.extraData?["operation"], "send_turn")
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "a send-path failure must not raise the modal"
        )
    }

    func testActivityPingRecentRefreshFailureIsAdvisoryNeverModal() async throws {
        // M3 (§4.5): the bootstrap conversation load succeeds; the
        // activity-ping-triggered recent-list refresh then fails. This refresh is a
        // background advisory read — not user-initiated — so its failure must NOT
        // raise the modal or a `Chat.alertPresented` breadcrumb (it used to). It
        // still records the transport breadcrumb (Chat.recentConversations).
        let conversationsRequests = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text(
                    """
                    event: conversation_activity
                    data: {"conversation_id":"web_conv_pinged","reason":"turn_started"}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                // First fetch (bootstrap) succeeds; the refresh triggered by the
                // activity ping fails — now advisory, never modal.
                if conversationsRequests.increment() <= 1 {
                    return .json(#"{"conversations":[],"count":0}"#)
                }
                return .json(#"{"detail":"temporary"}"#, statusCode: 503)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-alert-recent-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: nil,
            spoolDirectory: spoolDirectory
        )
        await model.bootstrap()

        // The transport breadcrumb confirms the advisory refresh ran and failed;
        // give any (erroneous) modal breadcrumb time to spool before asserting none.
        _ = try await waitForSpooledReports(component: "Chat.recentConversations", in: spoolDirectory)
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertNil(model.errorMessage, "an advisory recent-list refresh must not modal")
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "an advisory recent-list refresh failure must not emit Chat.alertPresented"
        )
    }

    func testPendingApprovalsPollFailureNeverModals() async throws {
        // Regression for prod cluster G (M3, §4.5): the ~15s pending-approvals poll
        // is a background advisory read. Its failure — including under suspend/resume
        // conditions where a 503 burst is common — must NEVER raise the shared modal
        // or emit a `Chat.alertPresented` breadcrumb. Persistent failure surfaces as
        // degraded health instead, verified in the advisory-degraded tests below.
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"detail":"temporary"}"#, statusCode: 503)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-alert-approvals-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: nil,
            spoolDirectory: spoolDirectory
        )
        await model.bootstrap()

        // The poll's own breadcrumb (Chat.pendingApprovals) confirms the failing
        // read ran; give it and any (erroneous) modal breadcrumb time to spool.
        _ = try await waitForSpooledReports(component: "Chat.pendingApprovals", in: spoolDirectory)
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertNil(model.errorMessage, "the approvals poll must never raise the modal")
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "an advisory approvals-poll failure must not emit Chat.alertPresented"
        )
    }

    func testPersistentAdvisoryReadFailureDegradesThenClearsOnSuccess() async throws {
        // §4.5: a run of advisory-read failures degrades coordinator presentation
        // after N (=3) in a row, and the next advisory success clears it — so a
        // silent-forever background failure is impossible while recovery is prompt.
        let failing = AtomicFlag(true)
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations"):
                if failing.value {
                    return .json(#"{"detail":"temporary"}"#, statusCode: 503)
                }
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-advisory-degrade-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: nil,
            spoolDirectory: spoolDirectory
        )
        model.syncCoordinator.apply(.followConnected(generation: model.syncCoordinator.followGeneration))
        model.syncCoordinator.apply(.activityConnected(generation: model.syncCoordinator.activityGeneration))
        XCTAssertEqual(model.syncPresentation, .live)

        // Two advisory failures stay silent; the third crosses the degraded floor.
        await model.refreshConversations()
        await model.refreshConversations()
        XCTAssertEqual(model.syncPresentation, .live, "below-threshold advisory failures stay silent")
        await model.refreshConversations()
        XCTAssertEqual(model.syncPresentation, .degraded)
        XCTAssertNil(model.errorMessage, "advisory reads never modal")

        // Recovery: the next good advisory read clears degraded immediately.
        failing.value = false
        await model.refreshConversations()
        XCTAssertEqual(model.syncPresentation, .live)
    }

    func testConversationListRefreshFailureShowsListBannerNotModalOrThread() async throws {
        // A conversation-list refresh failure (pull-to-refresh or background) surfaces
        // on the LIST via `conversationsRefreshFailed` — never a modal or the
        // thread-scoped inline message, which does not render on the list column.
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"detail":"temporary"}"#, statusCode: 503)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let model = makeViewModel(conversationID: nil)

        await model.refreshConversations()

        XCTAssertTrue(model.conversationsRefreshFailed, "the failure shows the list banner")
        XCTAssertNil(model.errorMessage, "a list-refresh failure must not raise the modal")
        XCTAssertNil(model.threadInlineMessage, "and must not use the thread-scoped banner")
    }

    func testConversationListRefreshBannerClearsAndStampsTimeOnSuccess() async throws {
        // The banner state machine: a failure sets the flag; the next successful
        // refresh clears it and stamps the last-refreshed time for the banner subtitle.
        let failing = AtomicFlag(true)
        ChatMockBackendURLProtocol.respond { _ in
            if failing.value {
                return .json(#"{"detail":"temporary"}"#, statusCode: 503)
            }
            return .json(#"{"conversations":[],"count":0}"#)
        }
        let model = makeViewModel(conversationID: nil)

        await model.refreshConversations()
        XCTAssertTrue(model.conversationsRefreshFailed)

        failing.value = false
        await model.refreshConversations()
        XCTAssertFalse(model.conversationsRefreshFailed, "a successful refresh clears the banner")
        XCTAssertNotNil(model.conversationsLastRefreshedAt, "and stamps the last-refreshed time")
    }

    func testConversationReturning404IsTreatedAsGone() async throws {
        // §4.5: a 404 on a per-conversation read means the conversation was deleted.
        // It is dropped from the held list and shown as gone inline, not surfaced as
        // a transport error / modal.
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations/web_conv_gone/messages"):
                return .json(#"{"detail":"not found"}"#, statusCode: 404)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-gone-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_gone",
            spoolDirectory: spoolDirectory
        )
        model.conversations = [
            ChatConversationSummary(
                conversationID: "web_conv_gone",
                lastMessage: "Gone",
                lastTimestamp: Date(),
                messageCount: 1
            ),
        ]

        await model.loadMessages(userInitiated: false)

        XCTAssertFalse(
            model.conversations.contains { $0.conversationID == "web_conv_gone" },
            "a 404 conversation must be removed from the list"
        )
        XCTAssertNil(model.errorMessage, "a gone conversation must not raise the transport modal")
        XCTAssertNotNil(model.threadInlineMessage, "the selected gone conversation shows a gone-state")
    }

    func testAdvisoryRead429SchedulesRetryHonoringRetryAfter() async throws {
        // §4.5: a 429 on an advisory read schedules exactly one retry after the
        // honored Retry-After (encoded here in the response detail). The retry then
        // succeeds and the list converges, without any modal.
        let attempts = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations"):
                if attempts.increment() <= 1 {
                    return .json(#"{"detail":"0"}"#, statusCode: 429)
                }
                return .json(
                    #"{"conversations":[{"conversation_id":"web_conv_after","last_message":"After","last_timestamp":"2026-07-21T00:00:00Z","message_count":1}],"count":1}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-429-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: nil,
            spoolDirectory: spoolDirectory
        )

        await model.refreshConversations()
        XCTAssertNil(model.errorMessage, "a 429 advisory read must not modal")

        try await waitUntil(timeout: 4) {
            model.conversations.contains { $0.conversationID == "web_conv_after" }
        }
    }

    func testThreadInlineMessageClearsOnConversationSwitch() async throws {
        // The thread-scoped inline banner is bound to `threadInlineMessage`. It is
        // scoped to its thread: switching conversations must clear it so a stale
        // banner can't linger over an unrelated conversation.
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations/web_conv_other/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_other","messages":[],"count":0,"total_messages":0,"has_more_before":false,"has_more_after":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_banner")
        model.threadInlineMessage = "You no longer have access to this conversation."
        XCTAssertNotNil(model.threadInlineMessage)

        await model.selectConversation("web_conv_other")

        XCTAssertNil(
            model.threadInlineMessage,
            "switching conversations clears the thread inline banner"
        )
    }

    func testThreadInlineMessageClearsOnSuccessfulReload() async throws {
        // The thread-scoped inline banner (from a prior failed / access-changed load)
        // must clear when the SAME thread reloads successfully, per its documented
        // contract ("cleared when the thread reloads") — not only on a conversation
        // switch — so it can't linger over freshly reloaded content.
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations/web_conv_reload/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_reload","messages":[{"internal_id":1,"role":"assistant","content":"Hello","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_reload")
        model.threadInlineMessage = "You no longer have access to this conversation."
        XCTAssertNotNil(model.threadInlineMessage)

        await model.loadMessages(conversationID: "web_conv_reload", userInitiated: true)

        XCTAssertNil(
            model.threadInlineMessage,
            "a successful reload of the same thread clears the stale inline banner"
        )
    }

    func testSendFailureSurfacesInlineWithNoModalAndOneBreadcrumb() async throws {
        // §4.5 slice 2: a terminal send failure surfaces inline on the bubble —
        // failed styling + retry affordance — with NO modal, NO `Chat.alertPresented`,
        // and exactly ONE `Chat.inlineErrorPresented` breadcrumb.
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                return .json(#"{"detail":"server is down"}"#, statusCode: 500)
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-inline-send-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_inline_send",
            spoolDirectory: spoolDirectory
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed, "the optimistic bubble takes the failed styling")
        XCTAssertNotNil(model.failedSend(for: assistant), "a retry affordance is recorded on the bubble")
        XCTAssertNil(model.errorMessage, "no modal is raised on the send path")

        let inline = try await waitForSpooledReports(
            component: "Chat.inlineErrorPresented",
            in: spoolDirectory
        )
        XCTAssertEqual(inline.count, 1, "exactly one inline breadcrumb per failure")
        XCTAssertEqual(inline.first?.extraData?["operation"], "send_turn")
        XCTAssertEqual(inline.first?.extraData?["reason"], "action_failed")
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "no Chat.alertPresented breadcrumb on the send path"
        )
    }

    func testRetryBeforeAcceptRePostsSameTurnIDExactlyTwice() async throws {
        // Before-accept retry (`POST /turns` was never accepted): retry re-POSTs
        // with the SAME client turn_id, so the server dedupes and double-send is
        // impossible. The first POST 500s; the retry (same id) succeeds and the
        // turn streams to completion. Exactly two POSTs, same turn_id.
        let postedTurnIDs = AtomicStringList()
        let postCount = AtomicCounter()
        var streamedTurnID = "turn-retry-before"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnIDs.append(turnID)
                streamedTurnID = turnID
                if postCount.increment() == 1 {
                    return .json(#"{"detail":"server is down"}"#, statusCode: 500)
                }
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_retry_before","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_retry_before/stream"):
                return .text(
                    """
                    event: turn_started
                    data: {"turn_id":"\(streamedTurnID)","seq":0}

                    event: text
                    data: {"content":"Recovered"}

                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"complete"}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_retry_before/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_retry_before","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Recovered","timestamp":"2026-06-08T12:00:01Z"}],"count":2,"total_messages":2,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_retry_before")
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        let failedAssistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        let retryPayload = try XCTUnwrap(model.failedSend(for: failedAssistant))
        XCTAssertFalse(retryPayload.postAccepted, "a never-accepted POST retries by re-POSTing")

        await model.retryFailedSend(turnID: retryPayload.turnID)
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(postCount.value, 2, "exactly two POSTs: original + one retry")
        XCTAssertEqual(
            Set(postedTurnIDs.values).count, 1,
            "the retry re-POSTs the SAME turn_id, so the server dedupes and never double-sends"
        )
        XCTAssertNil(
            model.failedSends[retryPayload.turnID],
            "a successful retry clears the inline state"
        )
        XCTAssertNil(model.errorMessage)
    }

    func testRetryIsIgnoredWhileAnotherTurnIsStreaming() async throws {
        // Retrying an old failed bubble must NOT tear down a newer, actively-streaming
        // turn: `retryFailedSend` calls `cancelStream()`, so it refuses while another
        // turn is in flight (the Retry button is disabled in that state too).
        let liveLeg = HangingStream()
        let postCount = AtomicCounter()
        var streamedTurnID = "turn-guard"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                streamedTurnID = turnID
                if postCount.increment() == 1 {
                    // First send: the POST fails, producing a failed bubble.
                    return .json(#"{"detail":"server is down"}"#, statusCode: 500)
                }
                // Second send: accepted, then streams indefinitely (hangs).
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_guard","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_guard/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(streamedTurnID)\",\"seq\":0}\n\n",
                    controller: liveLeg
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_guard")

        // First send fails -> a failed bubble carrying a Retry affordance.
        model.draftText = "First"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }
        let failed = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        let failedPayload = try XCTUnwrap(model.failedSend(for: failed))

        // Second send is accepted and streams (hangs), so a turn is actively in flight.
        model.draftText = "Second"
        await model.sendDraft()
        try await waitUntil { model.isStreaming }
        let activeTurnID = try XCTUnwrap(model.activeTurnSessionForTesting?.turnID)
        XCTAssertNotEqual(activeTurnID, failedPayload.turnID)

        // Retrying the OLD failed turn while the second is streaming must be a no-op.
        await model.retryFailedSend(turnID: failedPayload.turnID)

        XCTAssertTrue(model.isStreaming, "the active turn must survive the ignored retry")
        XCTAssertEqual(
            model.activeTurnSessionForTesting?.turnID, activeTurnID,
            "the retry must not replace the in-flight turn's session"
        )
        XCTAssertNotNil(
            model.failedSends[failedPayload.turnID],
            "the failed retry state is preserved so retry still works once streaming ends"
        )

        liveLeg.finish()
        model.cancelStream()
    }

    func testRetryAffordanceIsRestrictedToTheAssistantBubble() {
        // The user and assistant messages of a turn share the same `turnID`, so the
        // retry affordance must resolve to the ASSISTANT bubble only. Otherwise the
        // affordance would also render on the user bubble and `retryFailedSend` would
        // select it (appended first), clearing the prompt instead of the failed
        // reply.
        let model = makeViewModel(conversationID: "web_conv_retry_role")
        let turnID = "turn-shared-role"
        let user = ChatMessage(
            id: "local_user_role", role: .user, text: "Original prompt",
            createdAt: Date(), toolCalls: [], attachments: [], isLoading: false,
            status: .complete, processingProfileID: nil, errorTraceback: nil, turnID: turnID
        )
        let assistant = ChatMessage(
            id: "local_assistant_role", role: .assistant, text: "Sorry, an error occurred.",
            createdAt: Date(), toolCalls: [], attachments: [], isLoading: false,
            status: .failed, processingProfileID: nil, errorTraceback: nil, turnID: turnID
        )
        model.messages = [user, assistant]
        model.failedSends[turnID] = ChatViewModel.FailedSend(
            turnID: turnID, conversationID: "web_conv_retry_role",
            assistantMessageID: "local_assistant_role", prompt: "Original prompt",
            attachments: [], profileID: "default", previousSummary: nil,
            postAccepted: false, lastAppliedSeq: nil
        )

        XCTAssertNil(model.failedSend(for: user), "the user bubble must never expose a retry affordance")
        XCTAssertNotNil(model.failedSend(for: assistant), "the assistant bubble carries the affordance")
    }

    func testStaleMessageLoadDoesNotLeakLoadingFlag() async {
        // A delayed advisory message-load retry can land after the user switched
        // conversations. The stale-selection guard returns early; `isLoadingMessages`
        // must still reset, or the composer stays permanently disabled on the thread
        // the user moved to.
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/v1/chat/conversations/conv_stale/messages" {
                return .json(
                    #"{"conversation_id":"conv_stale","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"}],"count":1,"total_messages":1,"has_more_before":false,"has_more_after":false}"#
                )
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }
        let model = makeViewModel(conversationID: "conv_current")

        // Load a DIFFERENT (no-longer-selected) conversation, as a delayed retry
        // would after the user switched away from `conv_stale`.
        await model.loadMessages(conversationID: "conv_stale", userInitiated: false)

        XCTAssertFalse(
            model.isLoadingMessages,
            "a stale-selection load must reset the loading flag, not brick the composer"
        )
    }

    func testRetryAfterAcceptReconcilesWithoutSecondPost() async throws {
        // After-accept retry (`POST /turns` succeeded but the follow stream died
        // fatally): retry RECONCILES — resubscribe / reload history — rather than
        // re-POSTing, because the turn is already durable server-side. Exactly one
        // POST total.
        let postCount = AtomicCounter()
        let streamAttempts = AtomicCounter()
        var streamedTurnID = "turn-retry-after"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                postCount.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_retry_after","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_retry_after/stream"):
                let query = Self.queryItems(from: request)
                // Only the send-and-watch subscribe (follow=false) is scripted;
                // the always-on follow stream (follow=true) closes cleanly.
                guard query["follow"] == "false" else {
                    return .text("")
                }
                if streamAttempts.increment() == 1 {
                    // Fatal (non-auth) subscribe failure after the POST was accepted.
                    // A 401/403 would route to the auth flow (finding F3), so use a
                    // 422 to exercise the inline reconcile-on-retry path.
                    return .json(#"{"detail":"unprocessable"}"#, statusCode: 422)
                }
                // The reconcile resubscribe closes cleanly (interrupted) so the
                // retry falls through to a history reload.
                return .text("")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_retry_after/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_retry_after","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Durable reply","timestamp":"2026-06-08T12:00:01Z"}],"count":2,"total_messages":2,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_retry_after",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.05
        )
        model.draftText = "Hi"
        await model.sendDraft()
        await model.sendTaskForTesting?.value

        let failedAssistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        let retryPayload = try XCTUnwrap(model.failedSend(for: failedAssistant))
        XCTAssertTrue(retryPayload.postAccepted, "an accepted POST retries by reconciling")

        await model.retryFailedSend(turnID: retryPayload.turnID)
        await model.sendTaskForTesting?.value

        XCTAssertEqual(postCount.value, 1, "the reconcile retry does NOT re-POST the turn")
        XCTAssertNil(
            model.failedSends[retryPayload.turnID],
            "a successful reconcile clears the inline state"
        )
        XCTAssertTrue(
            model.messages.contains { $0.text == "Durable reply" },
            "the reconcile surfaces the durable reply from history"
        )
        XCTAssertNil(model.errorMessage)
    }

    func testReconcileRetryOfPersistedBubbleWithNewerMessagesKeepsTheBubble() async throws {
        // A failed send already merged into persisted history — its bubble is a `msg_`
        // row, not a `local_` placeholder — is retried AFTER newer messages exist. The
        // reconcile must reload the FULL window: an incremental merge keyed off the
        // latest persisted timestamp would never refetch the older removed row, so the
        // bubble would vanish. The mock honors `after` (as the real server does) so the
        // incremental path returns empty, making the regression observable.
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/chat/conversations/web_conv_persist/stream"):
                // The reconcile resubscribe closes cleanly, so the retry falls through
                // to a history reload.
                return .text("")
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_persist/messages"):
                if Self.queryItems(from: request)["after"] != nil {
                    // Incremental cursor at the newest message ⇒ nothing newer. This is
                    // exactly why an incremental reconcile would drop the older bubble.
                    return .json(
                        #"{"conversation_id":"web_conv_persist","messages":[],"count":0,"total_messages":3,"has_more_before":false,"has_more_after":false}"#
                    )
                }
                // Full load: the authoritative window with the persisted bubble.
                return .json(
                    #"{"conversation_id":"web_conv_persist","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Old reply","timestamp":"2026-06-08T12:00:01Z"},{"internal_id":3,"role":"user","content":"Newer","timestamp":"2026-06-08T12:05:00Z"}],"count":3,"total_messages":3,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_persist",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.05,
            maxConsecutiveStreamResumes: 1
        )
        await model.loadMessages(conversationID: "web_conv_persist", userInitiated: false)
        XCTAssertTrue(
            model.messages.contains { $0.id == "msg_2" && $0.text == "Old reply" },
            "precondition: the persisted assistant bubble is present before retry"
        )

        // The failed send resolves to the persisted `msg_2` bubble (finding F4 swap).
        model.failedSends["turn-persist"] = ChatViewModel.FailedSend(
            turnID: "turn-persist",
            conversationID: "web_conv_persist",
            assistantMessageID: "msg_2",
            prompt: "Hi",
            attachments: [],
            profileID: "default_assistant",
            previousSummary: nil,
            postAccepted: true,
            lastAppliedSeq: nil
        )

        await model.retryFailedSend(turnID: "turn-persist")
        await model.sendTaskForTesting?.value

        XCTAssertTrue(
            model.messages.contains { $0.id == "msg_2" && $0.text == "Old reply" },
            "the persisted failed bubble must be restored by the reconcile, not dropped"
        )
        XCTAssertTrue(
            model.messages.contains { $0.text == "Newer" },
            "the newer message is still present"
        )
        XCTAssertNil(model.failedSends["turn-persist"], "a successful reconcile clears the inline state")
    }

    func testReconcileRetrySubscriptionDropMidTurnKeepsRenderingAndCompletes() async throws {
        // Finding F1: a reconcile (after-accept) retry whose subscription drops
        // mid-turn — while the durable turn is still running server-side — must NOT
        // immediately retire the turn and reload an empty history (which would make
        // the reply vanish). It resumes from the last applied seq, exactly like the
        // primary send loop, and completes. The reply is never lost.
        let postCount = AtomicCounter()
        let reconcileStreamAttempts = AtomicCounter()
        var streamedTurnID = "turn-f1"
        var resumeFromSeq: String?
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            let query = Self.queryItems(from: request)
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                postCount.increment()
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_f1","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_f1/stream"):
                guard query["follow"] == "false" else {
                    return .text("")
                }
                let attempt = reconcileStreamAttempts.increment()
                if attempt == 1 {
                    // The ORIGINAL send: a fatal (non-auth) subscribe error records a
                    // reconcile-on-retry affordance.
                    return .json(#"{"detail":"unprocessable"}"#, statusCode: 422)
                }
                if attempt == 2 {
                    // The reconcile retry's FIRST subscribe drops mid-turn (no
                    // turn_ended): the durable turn is still running.
                    return .text(
                        """
                        event: text
                        data: {"turn_id":"\(streamedTurnID)","content":"Partial","seq":1}

                        event: stream_dropped
                        data: {}

                        """
                    )
                }
                // The reconcile resume finishes the durable turn.
                resumeFromSeq = query["from_seq"]
                return .text(
                    """
                    event: text
                    data: {"turn_id":"\(streamedTurnID)","content":" reply","seq":2}

                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"complete","seq":3}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_f1/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_f1","messages":[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Partial reply","turn_id":"\#(streamedTurnID)","timestamp":"2026-06-08T12:00:01Z"}],"count":2,"total_messages":2,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_f1",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.05
        )
        model.draftText = "Hi"
        await model.sendDraft()
        await model.sendTaskForTesting?.value

        let failed = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        let payload = try XCTUnwrap(model.failedSend(for: failed))
        await model.retryFailedSend(turnID: payload.turnID)
        await model.sendTaskForTesting?.value

        XCTAssertEqual(postCount.value, 1, "reconcile never re-POSTs")
        // The reconcile resumed from just past the last applied seq instead of
        // retiring the turn and reloading empty history.
        XCTAssertEqual(resumeFromSeq, "2")
        XCTAssertTrue(
            model.messages.contains { $0.text.contains("Partial reply") || $0.text.contains("reply") },
            "the reconciled turn keeps rendering / completes — the reply never vanishes"
        )
        XCTAssertNil(model.errorMessage)
    }

    func testSubscription401WithRejectedRefreshRoutesToAuthFlowNoRetry() async throws {
        // Finding F3: an accepted POST followed by a stream-GET 401 whose forced
        // refresh is REJECTED routes to the auth flow (authRequired) — NOT a Retry
        // button that would replay the unauthorized request. The bubble is finalized
        // (no spinner). A known-good fresh token lets the POST succeed; the forced
        // stream-401 refresh is rejected (scripted 401, and any leaked refresh token
        // is cleared so the outcome is order-independent in the shared-keychain
        // app-hosted bundle).
        seedFreshValidToken()
        // No refresh token: the forced stream-401 refresh short-circuits SYNCHRONOUSLY
        // (latches authRequired, no network), so this test never depends on a scripted
        // /api/auth/refresh round-trip that could interleave with the shared-keychain
        // app-hosted bundle.
        KeychainHelper.delete(key: "fa_refresh_token")
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-f3-rej","conversation_id":"web_conv_f3_rej","first_seq":0}"#
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

        let model = makeViewModel(conversationID: "web_conv_f3_rej")
        // Register the coordinator's auth observer (as bootstrap would) so the
        // send-path auth latch surfaces as `.authRequired`, without starting the
        // activity stream this mock would 401 before the POST runs.
        model.activateSyncForTesting()
        model.draftText = "Hi"
        await model.sendDraft()
        // Await the send task deterministically rather than polling `isStreaming`
        // with a fixed budget: under a loaded app-hosted bundle the MainActor send
        // flow can legitimately finish later than a 4s poll window.
        await model.sendTaskForTesting?.value

        XCTAssertFalse(model.isStreaming)
        XCTAssertNil(model.errorMessage, "a stream 401 must not raise the generic modal")
        XCTAssertEqual(model.syncPresentation, .authRequired)
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed, "the bubble is finalized, not left running")
        XCTAssertFalse(assistant.isLoading, "no lingering spinner")
        XCTAssertNil(
            model.failedSend(for: assistant),
            "no Retry affordance — re-auth drives recovery, not a replay of the 401"
        )
    }

    func testSubscription401WithSuccessfulRefreshResumesTurn() async throws {
        // Finding F3: an accepted POST followed by a stream-GET 401 whose forced
        // refresh SUCCEEDS resumes the durable turn (one forced refresh, resume on
        // success) and completes — no Retry button, no modal.
        seedExpiringToken()
        let streamAttempts = AtomicCounter()
        let refreshCount = AtomicCounter()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-f3-ok","conversation_id":"web_conv_f3_ok","first_seq":0}"#
                )
            case ("POST", "/api/auth/refresh"):
                refreshCount.increment()
                // `expires_in` keeps the refreshed token from being left EXPIRED in
                // the shared keychain (app-hosted tests share global auth state), so
                // this test can't leak an expired token into a later test.
                return .json(
                    #"{"api_token":"fresh-token","refresh_token":"fresh-refresh","expires_in":7200}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_f3_ok/messages"):
                return .json(
                    #"{"conversation_id":"web_conv_f3_ok","messages":[{"internal_id":1,"role":"user","content":"Hi","turn_id":"turn-f3-ok","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"Recovered","turn_id":"turn-f3-ok","timestamp":"2026-06-08T12:00:01Z"}],"count":2,"total_messages":2,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                if request.httpMethod == "GET", path.hasSuffix("/stream") {
                    // Only the send-and-watch subscribe (follow=false) is scripted to
                    // 401-then-resume; the always-on follow stream closes cleanly.
                    guard Self.queryItems(from: request)["follow"] == "false" else {
                        return .text("")
                    }
                    if streamAttempts.increment() == 1 {
                        return .json(#"{"detail":"expired token"}"#, statusCode: 401)
                    }
                    return .text(
                        """
                        event: text
                        data: {"turn_id":"turn-f3-ok","content":"Recovered","seq":1}

                        event: turn_ended
                        data: {"turn_id":"turn-f3-ok","status":"complete","seq":2}

                        """
                    )
                }
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(
            conversationID: "web_conv_f3_ok",
            liveReconnectInitialDelaySeconds: 0.01,
            liveReconnectMaxDelaySeconds: 0.05
        )
        model.draftText = "Hi"
        await model.sendDraft()
        await model.sendTaskForTesting?.value

        XCTAssertGreaterThanOrEqual(refreshCount.value, 1, "exactly one forced refresh drives the resume")
        XCTAssertNotEqual(model.syncPresentation, .authRequired, "a successful refresh is not authRequired")
        XCTAssertNil(model.errorMessage)
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertNil(model.failedSend(for: assistant), "the resumed turn shows no Retry affordance")
        XCTAssertTrue(
            model.messages.contains { $0.text.contains("Recovered") },
            "the turn resumed after the refresh and completed"
        )
    }

    func testServerFailedTurnRetryAffordanceSurvivesMerge() async throws {
        // Finding F4: a turn that FAILED server-side (turn_ended carrying an error)
        // records its retry affordance under the DURABLE turn id, so it survives
        // `mergeNewMessages` swapping the optimistic local_ bubble for its persisted
        // msg_ row (which carries the same turn_id). The Retry affordance is still
        // resolvable on the rendered bubble after the merge.
        var streamedTurnID = "turn-f4"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                streamedTurnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_f4","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_f4/stream"):
                guard Self.queryItems(from: request)["follow"] == "false" else {
                    return .text("")
                }
                return .text(
                    """
                    event: turn_ended
                    data: {"turn_id":"\(streamedTurnID)","status":"failed","error":"The model failed.","seq":1}

                    """
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_f4/messages"):
                // The persisted row for the failed turn carries the SAME turn_id.
                return .json(
                    #"{"conversation_id":"web_conv_f4","messages":[{"internal_id":1,"role":"user","content":"Hi","turn_id":"\#(streamedTurnID)","timestamp":"2026-06-08T12:00:00Z"},{"internal_id":2,"role":"assistant","content":"The model failed.","turn_id":"\#(streamedTurnID)","timestamp":"2026-06-08T12:00:01Z"}],"count":2,"total_messages":2,"has_more_before":false,"has_more_after":false}"#
                )
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_f4")
        model.draftText = "Hi"
        await model.sendDraft()
        await model.sendTaskForTesting?.value

        // After the completion merge, the rendered assistant bubble is the persisted
        // msg_ row — its local_ predecessor is gone. The retry affordance must still
        // resolve on it via the durable turn id.
        let rendered = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertFalse(rendered.id.hasPrefix("local_"), "the bubble was swapped for its persisted row")
        XCTAssertEqual(rendered.turnID, streamedTurnID)
        XCTAssertNotNil(
            model.failedSend(for: rendered),
            "the Retry affordance survives the optimistic→persisted bubble swap"
        )
    }

    func testPerOperationAdvisoryDegradeIsNotMaskedByAnotherOperationSuccess() async throws {
        // Finding F6: a persistently-failing endpoint interleaved with a succeeding
        // poll must still reach degraded — a single global counter would let the
        // succeeding poll reset it forever (silent-forever). Per-operation tracking:
        // the conversations refresh fails 3× while the approvals poll succeeds
        // between each; the failing operation still crosses the threshold. Its own
        // success then clears it.
        let conversationsFailing = AtomicFlag(true)
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/chat/conversations"):
                if conversationsFailing.value {
                    return .json(#"{"detail":"temporary"}"#, statusCode: 503)
                }
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-f6-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: nil,
            spoolDirectory: spoolDirectory
        )
        model.syncCoordinator.apply(.followConnected(generation: model.syncCoordinator.followGeneration))
        model.syncCoordinator.apply(.activityConnected(generation: model.syncCoordinator.activityGeneration))
        XCTAssertEqual(model.syncPresentation, .live)

        // Interleave a failing conversations refresh with a succeeding approvals
        // poll after each. A global counter would never cross the threshold.
        for _ in 0 ..< 3 {
            await model.refreshConversations()
            await model.loadPendingConfirmationsForTesting()
        }
        XCTAssertEqual(
            model.syncPresentation, .degraded,
            "the failing conversations refresh crosses the threshold despite the interleaved poll success"
        )

        // The failing operation's OWN success clears its counter → live again.
        conversationsFailing.value = false
        await model.refreshConversations()
        XCTAssertEqual(model.syncPresentation, .live)
    }

    func testLongQuietToolCallProducesNoSpuriousSurfaces() async throws {
        // §6.2 (M3): a turn that runs quietly for a long time — a slow tool call
        // with no events — must not spawn any spurious surface: no inline error, no
        // modal, and the presentation stays live (not degraded). Pinned with a
        // hanging stream that emits turn_started then nothing.
        let stream = HangingStream()
        let postedTurnID = AtomicString()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                postedTurnID.set(turnID)
                return .json(
                    #"{"turn_id":"\#(turnID)","conversation_id":"web_conv_quiet","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations/web_conv_quiet/stream"):
                return .hangingStream(
                    "event: turn_started\ndata: {\"turn_id\":\"\(postedTurnID.value ?? "")\",\"seq\":0}\n\n",
                    controller: stream
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }

        let model = makeViewModel(conversationID: "web_conv_quiet")
        model.syncCoordinator.apply(.followConnected(generation: model.syncCoordinator.followGeneration))
        model.syncCoordinator.apply(.activityConnected(generation: model.syncCoordinator.activityGeneration))
        model.draftText = "Do something slow"
        await model.sendDraft()
        try await waitUntil { model.isStreaming && postedTurnID.value != nil }

        // Let the turn run quietly for a while: no further events arrive.
        try await Task.sleep(for: .milliseconds(400))

        XCTAssertTrue(model.isStreaming, "the quiet turn is still running")
        XCTAssertNil(model.errorMessage, "no modal while a turn runs quietly")
        XCTAssertNil(model.threadInlineMessage, "no inline banner while a turn runs quietly")
        XCTAssertTrue(model.failedSends.isEmpty, "no failed-send affordance while a turn runs quietly")
        XCTAssertEqual(model.syncPresentation, .live, "a quiet running turn does not degrade the indicator")

        stream.finish()
        model.cancelStream()
    }

    func testAlertPresentedBreadcrumbFiresOnlyOnNewPresentation() async throws {
        // A failure while the modal is already open replaces its text without a
        // new presentation, so it must not add a second breadcrumb.
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-alert-replace-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: nil,
            spoolDirectory: spoolDirectory
        )

        model.presentErrorAlert("first failure", reason: .pendingApprovalsPoll)
        model.presentErrorAlert("second failure", reason: .recentConversationsRefresh)
        XCTAssertEqual(model.errorMessage, "second failure")

        _ = try await waitForSpooledReports(component: "Chat.alertPresented", in: spoolDirectory)
        try await Task.sleep(for: .milliseconds(300))
        let reports = spooledReports(in: spoolDirectory)
            .filter { $0.componentName == "Chat.alertPresented" }
        XCTAssertEqual(reports.count, 1)
        XCTAssertEqual(reports.first?.extraData?["reason"], "pending_approvals_poll")
    }

    func testAlertPresentedBreadcrumbBypassesDedupeAcrossPresentations() async throws {
        // Dismissing the modal and hitting the identical failure again is a real
        // second presentation; the reporter's dedupe window must not swallow it.
        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-alert-dedupe-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: nil,
            spoolDirectory: spoolDirectory,
            dedupeWindow: 60
        )

        model.presentErrorAlert("same failure", reason: .pendingApprovalsPoll)
        model.errorMessage = nil
        model.presentErrorAlert("same failure", reason: .pendingApprovalsPoll)

        try await waitUntil(timeout: 4) {
            self.spooledReports(in: spoolDirectory)
                .filter { $0.componentName == "Chat.alertPresented" }
                .count == 2
        }
    }

    func testAuthRequiredYieldsAuthRequiredPresentationWithoutModal() async throws {
        // A rejected token refresh on a required request must drive the coordinator
        // to `.authRequired` and NEVER raise the generic error modal or its
        // `Chat.alertPresented` breadcrumb.
        seedExpiringToken()
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/auth/refresh" {
                return .json(#"{"detail":"expired"}"#, statusCode: 401)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-auth-required-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: nil,
            spoolDirectory: spoolDirectory
        )

        await model.bootstrap()

        XCTAssertEqual(model.syncPresentation, .authRequired)
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "an auth-required failure must not raise the generic error modal"
        )
    }

    func testPreTurnNoCredentialsSendDoesNotModal() async throws {
        // Finding 7: a send whose `startTurn` fails pre-turn with
        // `AuthError.noCredentials` (a rejected session) routes through
        // `appendStreamError`, not the underlyingError-carrying `presentErrorAlert`.
        // The underlying error must be threaded through so the auth failure is
        // suppressed: the `.authRequired` presentation covers the UX, and no generic
        // modal (`errorMessage` / `Chat.alertPresented` breadcrumb) may appear.
        seedExpiringToken()
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/auth/refresh" {
                return .json(#"{"detail":"expired"}"#, statusCode: 401)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-preturn-nocreds-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_preturn_nocreds",
            spoolDirectory: spoolDirectory
        )

        await model.bootstrap()
        model.draftText = "Hello"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertNil(model.errorMessage)
        XCTAssertEqual(model.syncPresentation, .authRequired)
        // Finding F2: the optimistic assistant bubble must NOT be left running behind
        // the sign-in screen — no lingering spinner. It is finalized to
        // failed-non-running with no Retry (re-auth then re-send from the composer).
        let assistant = try XCTUnwrap(model.messages.last { $0.role == .assistant })
        XCTAssertEqual(assistant.status, .failed, "the bubble is finalized, not left .running")
        XCTAssertFalse(assistant.isLoading, "no lingering spinner after a noCredentials send")
        XCTAssertNil(model.failedSend(for: assistant), "no Retry — re-auth drives recovery")
        // Give any (erroneous) modal breadcrumb time to spool before asserting absence.
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "a pre-turn noCredentials send must not raise the generic error modal"
        )
    }

    func testBufferedResyncDrainDoesNotModalOnFailingFollowUpRefresh() async throws {
        // Finding 10: a buffered turn_ended drained during a resync routes through
        // the steady-state handler, whose follow-up list/message refresh defaults to
        // modal-raising. The drain must use the silent (`userInitiated: false`)
        // variant, so a failing follow-up refresh degrades silently — no modal.
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            // Every follow-up refresh the drain triggers fails.
            if request.httpMethod == "GET", path == "/api/v1/chat/conversations" {
                return .json(#"{"detail":"boom"}"#, statusCode: 500)
            }
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json(#"{"detail":"boom"}"#, statusCode: 500)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-drain-silent-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_drain_silent",
            spoolDirectory: spoolDirectory
        )

        let turnEnded = ChatStreamEvent(
            type: .turnEnded,
            turnID: "turn-drain",
            seq: 5,
            text: nil,
            toolCall: nil,
            toolCallID: nil,
            toolResult: nil,
            attachments: [],
            attachmentSource: .response,
            confirmation: nil,
            confirmationResult: nil,
            errorMessage: nil,
            status: "completed"
        )

        await model.drainFollowEvent(
            turnEnded,
            conversationID: "web_conv_drain_silent",
            generation: model.syncCoordinator.followGeneration
        )
        await model.drainActivitySignal(generation: model.syncCoordinator.activityGeneration)

        XCTAssertNil(model.errorMessage)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" },
            "a buffered resync drain's failing follow-up refresh must not raise the modal"
        )
    }

    func testMidTurnDropRecoveryEmitsNoAlertPresentedBreadcrumb() async throws {
        // Characterization: a mid-turn transport drop recovers by reloading
        // history. It WILL spool a `Chat.streamDrop` breadcrumb, but it must NOT
        // present the modal, so no `Chat.alertPresented` breadcrumb is spooled.
        var streamedTurnID = "turn-drop-silent"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_drop_silent","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_drop_silent/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_drop_silent",
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

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-silent-drop-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_drop_silent",
            spoolDirectory: spoolDirectory,
            liveReconnectInitialDelaySeconds: 0.001,
            liveReconnectMaxDelaySeconds: 0.001,
            maxConsecutiveStreamResumes: 2,
            streamResumeLivenessSeconds: 60
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Durable reply"])
        XCTAssertNil(model.errorMessage)
        // A stream-drop breadcrumb is expected; a modal-alert breadcrumb is not.
        _ = try await waitForSpooledReports(component: "Chat.streamDrop", in: spoolDirectory)
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" }
        )
    }

    func testSend410RecoveryEmitsNoAlertPresentedBreadcrumb() async throws {
        // Characterization: a 410 on subscribe reloads history silently — no modal,
        // so no `Chat.alertPresented` breadcrumb.
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                return .json(
                    #"{"turn_id":"turn-410-silent","conversation_id":"web_conv_410_silent","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_410_silent/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_410_silent",
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

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-silent-410-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_410_silent",
            spoolDirectory: spoolDirectory
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Persisted reply"])
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" }
        )
    }

    func testTransientServerErrorRecoveryEmitsNoAlertPresentedBreadcrumb() async throws {
        // Characterization: a transient 5xx on subscribe resubscribes and completes
        // silently — no modal, so no `Chat.alertPresented` breadcrumb.
        let streamRequests = AtomicCounter()
        var streamedTurnID = "turn-5xx-silent"
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("POST", "/api/v1/chat/turns"):
                if let body = (try? Self.jsonObject(from: request)) as? [String: Any],
                   let postedTurnID = body["turn_id"] as? String {
                    streamedTurnID = postedTurnID
                }
                return .json(
                    #"{"turn_id":"\#(streamedTurnID)","conversation_id":"web_conv_5xx_silent","first_seq":0}"#
                )
            case ("GET", "/api/v1/chat/conversations"):
                return .json(#"{"conversations":[],"count":0}"#)
            case ("GET", "/api/v1/chat/conversations/web_conv_5xx_silent/messages"):
                return .json(
                    """
                    {
                      "conversation_id":"web_conv_5xx_silent",
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
                if request.httpMethod == "GET",
                   path == "/api/v1/chat/conversations/web_conv_5xx_silent/stream" {
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

        let spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("vm-silent-5xx-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: spoolDirectory) }
        let model = makeViewModelWithSpooledReporter(
            conversationID: "web_conv_5xx_silent",
            spoolDirectory: spoolDirectory
        )
        model.draftText = "Hi"
        await model.sendDraft()
        try await waitUntil { !model.isStreaming }

        XCTAssertEqual(streamRequests.value, 2)
        XCTAssertEqual(model.messages.map(\.text), ["Hi", "Recovered reply"])
        XCTAssertNil(model.errorMessage)
        XCTAssertFalse(
            spooledReports(in: spoolDirectory).contains { $0.componentName == "Chat.alertPresented" }
        )
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

    /// Poll a reporter spool directory until at least one report with the given
    /// component lands, then return every matching report. Reports with other
    /// components (e.g. `Chat.streamDrop`) are ignored so a recovery breadcrumb
    /// doesn't satisfy an assertion about the modal-alert breadcrumb.
    private func waitForSpooledReports(
        component: String,
        in directory: URL,
        timeout: TimeInterval = 4
    ) async throws -> [ErrorReportPayload] {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let matching = spooledReports(in: directory).filter { $0.componentName == component }
            if !matching.isEmpty {
                return matching
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        let components = spooledReports(in: directory).map { $0.componentName ?? "<nil>" }
        XCTFail("Timed out waiting for a spooled \(component) report. Spooled: \(components)")
        throw ChatAPIError.validation("no spooled report")
    }

    /// Decode every report currently spooled in the directory.
    private func spooledReports(in directory: URL) -> [ErrorReportPayload] {
        let files = (try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil
        )) ?? []
        return files.compactMap { file in
            guard file.pathExtension == "json",
                  let data = try? Data(contentsOf: file) else {
                return nil
            }
            return try? JSONDecoder().decode(ErrorReportPayload.self, from: data)
        }
    }

    /// Build a view model wired to a spool-backed reporter with no base URL, so
    /// every breadcrumb is persisted to the spool for deterministic assertions.
    private func makeViewModelWithSpooledReporter(
        conversationID: String?,
        spoolDirectory: URL,
        dedupeWindow: TimeInterval = 0,
        liveReconnectInitialDelaySeconds: Double = 2,
        liveReconnectMaxDelaySeconds: Double = 30,
        maxConsecutiveStreamResumes: Int = 5,
        streamResumeLivenessSeconds: Double = 2,
        pathMonitor: PathMonitoring? = nil
    ) -> ChatViewModel {
        let reporter = ErrorReporter(spoolDirectory: spoolDirectory, dedupeWindow: dedupeWindow)
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return track(ChatViewModel(
            authManager: authManager,
            conversationID: conversationID,
            liveReconnectInitialDelaySeconds: liveReconnectInitialDelaySeconds,
            liveReconnectMaxDelaySeconds: liveReconnectMaxDelaySeconds,
            maxConsecutiveStreamResumes: maxConsecutiveStreamResumes,
            streamResumeLivenessSeconds: streamResumeLivenessSeconds,
            errorReporter: reporter,
            pathMonitor: pathMonitor ?? StubPathMonitor(isSatisfied: true)
        ))
    }

    private func makeViewModel(
        conversationID: String?,
        initialPrompt: String? = nil,
        startsNewConversation: Bool = false,
        liveReconnectInitialDelaySeconds: Double = 2,
        liveReconnectMaxDelaySeconds: Double = 30,
        maxConsecutiveStreamResumes: Int = 5,
        streamResumeLivenessSeconds: Double = 2,
        streamTextFlushInterval: Duration = .milliseconds(50),
        pathMonitor: PathMonitoring? = nil
    ) -> ChatViewModel {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return track(ChatViewModel(
            authManager: authManager,
            conversationID: conversationID,
            initialPrompt: initialPrompt,
            startsNewConversation: startsNewConversation,
            liveReconnectInitialDelaySeconds: liveReconnectInitialDelaySeconds,
            liveReconnectMaxDelaySeconds: liveReconnectMaxDelaySeconds,
            maxConsecutiveStreamResumes: maxConsecutiveStreamResumes,
            streamResumeLivenessSeconds: streamResumeLivenessSeconds,
            streamTextFlushInterval: streamTextFlushInterval,
            errorReporter: ErrorReporter(spoolDirectory: nil),
            pathMonitor: pathMonitor ?? StubPathMonitor(isSatisfied: true)
        ))
    }

    private func track(_ model: ChatViewModel) -> ChatViewModel {
        trackedViewModels.append(WeakChatViewModelReference(model))
        return model
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

    /// Force a known-good, non-expiring api token with NO refresh token, overriding
    /// any state a prior test leaked into the shared keychain (app-hosted tests share
    /// global auth state). Used by tests that must deterministically exercise a
    /// forced-refresh rejection regardless of ordering.
    private func seedFreshValidToken() {
        KeychainHelper.save(key: "fa_api_token", string: "chat-view-model-token")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
    }

    /// Overwrite the fresh setUp token with one due for refresh, so the next
    /// authorized request drives `refreshIfNeeded` through its network path.
    private func seedExpiringToken() {
        KeychainHelper.save(key: "fa_api_token", string: "chat-view-model-token")
        KeychainHelper.save(key: "fa_refresh_token", string: "chat-view-model-refresh")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(-60)),
            forKey: "fa_token_expiry"
        )
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

    private func privateStringArray(_ name: String, in model: ChatViewModel) -> [String] {
        for child in Mirror(reflecting: model).children where child.label == name {
            guard let value = child.value as? [String] else {
                XCTFail("Private property \(name) was not a [String]")
                return []
            }
            return value
        }
        XCTFail("Private property \(name) was not found")
        return []
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

private final class WeakChatViewModelReference {
    weak var model: ChatViewModel?

    init(_ model: ChatViewModel) {
        self.model = model
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

    func set(_ newValue: Int) {
        lock.withLock { count = newValue }
    }

    var value: Int {
        lock.withLock { count }
    }
}

/// Lock-guarded boolean flipped from the test body but read on the URLProtocol
/// loading thread, so a stubbed endpoint can change its response mid-test.
private final class AtomicFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var flag: Bool

    init(_ initial: Bool) {
        flag = initial
    }

    var value: Bool {
        get { lock.withLock { flag } }
        set { lock.withLock { flag = newValue } }
    }
}

/// Lock-guarded optional string for capturing a value (e.g. a client-generated
/// turn id) off the URLProtocol loading thread.
private final class AtomicString: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: String?

    func set(_ value: String?) {
        lock.withLock { stored = value }
    }

    var value: String? {
        lock.withLock { stored }
    }
}

/// Lock-guarded ordered list of strings for capturing every value a stubbed
/// endpoint saw (e.g. the turn_id on each POST retry) off the loader thread.
private final class AtomicStringList: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [String] = []

    func append(_ value: String) {
        lock.withLock { stored.append(value) }
    }

    var values: [String] {
        lock.withLock { stored }
    }
}
