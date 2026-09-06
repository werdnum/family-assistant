import XCTest

@testable import FamilyAssistant

final class RouteTests: XCTestCase {
    func testWatchComplicationLaunchesVoice() {
        XCTAssertTrue(WatchVoiceLaunch.opensVoice(WatchVoiceLaunch.url))
    }

    func testWatchLaunchRejectsUnrelatedURLs() throws {
        for value in ["familyassistant://voice", "familyassistant-watch://settings",
                      "https://voice", "familyassistant-watch://voice/other"] {
            XCTAssertFalse(WatchVoiceLaunch.opensVoice(try XCTUnwrap(URL(string: value))), value)
        }
    }

    private let baseURL = URL(string: "https://assistant.example.test")!

    private func url(_ string: String) throws -> URL {
        try XCTUnwrap(URL(string: string, relativeTo: baseURL)?.absoluteURL)
    }

    // MARK: - NotesRoute parsing

    func testNotesListRouteMatchesSameOriginNotesPath() throws {
        XCTAssertEqual(NotesRoute.route(for: try url("/notes"), relativeTo: baseURL), .list)
    }

    func testNotesAddRouteMatchesSameOriginAddPath() throws {
        XCTAssertEqual(NotesRoute.route(for: try url("/notes/add"), relativeTo: baseURL), .add)
    }

    func testNotesEditRouteDecodesEncodedTitle() throws {
        let parsed = NotesRoute.route(for: try url("/notes/edit/Wi-Fi%20Password"), relativeTo: baseURL)
        XCTAssertEqual(parsed, .edit(title: "Wi-Fi Password"))
    }

    func testNotesEditRouteRejectsMissingTitle() throws {
        XCTAssertNil(NotesRoute.route(for: try url("/notes/edit/"), relativeTo: baseURL))
    }

    func testNotesRouteAcceptsTrailingSlashAndDefaultPort() throws {
        let direct = try XCTUnwrap(URL(string: "https://assistant.example.test:443/notes/"))
        XCTAssertEqual(NotesRoute.route(for: direct, relativeTo: baseURL), .list)
    }

    func testNotesRouteRejectsDifferentOrigin() throws {
        let foreign = try XCTUnwrap(URL(string: "https://example.test/notes"))
        XCTAssertNil(NotesRoute.route(for: foreign, relativeTo: baseURL))
    }

    func testNotesRouteRejectsDifferentPort() throws {
        let other = try XCTUnwrap(URL(string: "https://assistant.example.test:444/notes"))
        XCTAssertNil(NotesRoute.route(for: other, relativeTo: baseURL))
    }

    // MARK: - ChatRoute parsing

    func testChatRouteParsesBarePath() throws {
        XCTAssertEqual(ChatRoute.route(for: try url("/chat"), relativeTo: baseURL), ChatRoute())
    }

    func testChatRouteParsesConversationAndPrompt() throws {
        let parsed = ChatRoute.route(
            for: try url("/chat?conversation_id=web_conv_abc&q=Hello%20there"),
            relativeTo: baseURL
        )
        XCTAssertEqual(parsed, ChatRoute(conversationID: "web_conv_abc", initialPrompt: "Hello there"))
    }

    func testChatRouteParsesNewConversationRequest() throws {
        let parsed = try XCTUnwrap(ChatRoute.route(for: try url("/chat?new=1"), relativeTo: baseURL))

        XCTAssertNil(parsed.conversationID)
        XCTAssertNil(parsed.initialPrompt)
        XCTAssertNotNil(parsed.newConversationRequestID)
    }

    // MARK: - SharedConversationRoute parsing

    func testSharedConversationRouteParsesSameOriginToken() throws {
        let parsed = SharedConversationRoute.route(
            for: try url("/shared/conversations/share-token_123"),
            relativeTo: baseURL
        )

        XCTAssertEqual(parsed, SharedConversationRoute(token: "share-token_123"))
    }

    func testSharedConversationRouteRejectsMissingOrNestedToken() throws {
        XCTAssertNil(SharedConversationRoute.route(
            for: try url("/shared/conversations/"),
            relativeTo: baseURL
        ))
        XCTAssertNil(SharedConversationRoute.route(
            for: try url("/shared/conversations/token/extra"),
            relativeTo: baseURL
        ))
    }

    func testSharedConversationRouteRejectsForeignOrigin() throws {
        let foreign = try XCTUnwrap(URL(string: "https://evil.test/shared/conversations/token"))
        XCTAssertNil(SharedConversationRoute.route(for: foreign, relativeTo: baseURL))
    }

    // MARK: - owningTab resolution (table-driven)

    func testOwningTabResolvesEachDestination() throws {
        let cases: [(path: String, expected: AppTab?)] = [
            ("/chat", .chat),
            ("/chat?conversation_id=abc&q=hi", .chat),
            ("/chat?new=1", .chat),
            ("/shared/conversations/token", .chat),
            ("/notes", .notes),
            ("/notes/edit/Milk", .notes),
            ("/documents/", .documents),
            ("/documents", .documents),
            ("/documents/123", .documents),
            ("/voice", .voice),
            ("/events", .more),
            ("/automations", .more),
            ("/vector-search", .more),
            ("/tasks", .more),
            ("/", .more),
        ]
        for testCase in cases {
            let resolved = AppRouter.owningTab(for: try url(testCase.path), relativeTo: baseURL)
            XCTAssertEqual(resolved, testCase.expected, "path \(testCase.path)")
        }
    }

    func testOwningTabRejectsForeignOrigin() throws {
        let foreign = try XCTUnwrap(URL(string: "https://evil.test/chat"))
        XCTAssertNil(AppRouter.owningTab(for: foreign, relativeTo: baseURL))
    }

    // MARK: - navigate() applies per-tab routes

    func testNavigateSelectsChatWithSelection() throws {
        let router = AppRouter()
        XCTAssertTrue(router.navigate(to: try url("/chat?conversation_id=c1&q=hey"), relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .chat)
        XCTAssertEqual(router.chatSelection, ChatRoute(conversationID: "c1", initialPrompt: "hey"))
        XCTAssertNil(router.sharedConversationRoute)
    }

    func testNavigateSelectsNativeSharedConversation() throws {
        let router = AppRouter()

        XCTAssertTrue(router.navigate(
            to: try url("/shared/conversations/share-token"),
            relativeTo: baseURL
        ))

        XCTAssertEqual(router.selectedTab, .chat)
        XCTAssertEqual(router.sharedConversationRoute, SharedConversationRoute(token: "share-token"))
    }

    func testNavigateFromSharedConversationBackToChatClearsSharedRoute() throws {
        let router = AppRouter()
        XCTAssertTrue(router.navigate(
            to: try url("/shared/conversations/share-token"),
            relativeTo: baseURL
        ))

        XCTAssertTrue(router.navigate(to: try url("/chat?conversation_id=c1"), relativeTo: baseURL))

        XCTAssertNil(router.sharedConversationRoute)
        XCTAssertEqual(router.chatSelection.conversationID, "c1")
    }

    func testNavigateSelectsNotesRoute() throws {
        let router = AppRouter()
        XCTAssertTrue(router.navigate(to: try url("/notes/edit/Milk"), relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .notes)
        XCTAssertEqual(router.notesRoute, .edit(title: "Milk"))
    }

    func testNavigateDocumentsRootClearsStack() throws {
        let router = AppRouter()
        router.documentsPath = [WebRoute(path: "/documents/stale")]
        XCTAssertTrue(router.navigate(to: try url("/documents/"), relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .documents)
        XCTAssertEqual(router.documentsPath, [])
    }

    func testNavigateDocumentsSubPathPushesStack() throws {
        let router = AppRouter()
        XCTAssertTrue(router.navigate(to: try url("/documents/123"), relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .documents)
        XCTAssertEqual(router.documentsPath, [WebRoute(path: "/documents/123")])
    }

    func testNavigateLongTailGoesToMore() throws {
        let router = AppRouter()
        XCTAssertTrue(router.navigate(to: try url("/events"), relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .more)
        XCTAssertEqual(router.morePath, [.web(WebRoute(path: "/events"))])
    }

    func testNavigateVoiceGoesToNativeVoiceRoute() throws {
        let router = AppRouter()
        XCTAssertTrue(router.navigate(to: try url("/voice"), relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .voice)
        XCTAssertEqual(router.morePath, [])
    }

    func testNavigateForeignOriginReturnsFalse() throws {
        let router = AppRouter()
        let foreign = try XCTUnwrap(URL(string: "https://evil.test/chat"))
        XCTAssertFalse(router.navigate(to: foreign, relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .chat)
    }

    func testOpenSharedAttachmentsSelectsNewChatRoute() {
        let router = AppRouter()

        router.openSharedAttachments(batchID: "batch-1")

        XCTAssertEqual(router.selectedTab, .chat)
        XCTAssertNil(router.chatSelection.conversationID)
        XCTAssertEqual(router.chatSelection.newConversationRequestID, "batch-1")
        XCTAssertEqual(router.chatSelection.sharedAttachmentBatchID, "batch-1")
    }

    // MARK: - followWebLink (in-page link taps)

    func testFollowWebLinkWithinSameDocumentsTabIsLeftToWebView() throws {
        // Same-tab navigation is handled by the web view itself (no native
        // push), so the call returns false and the documents stack is untouched.
        let router = AppRouter()
        router.selectedTab = .documents
        router.documentsPath = [WebRoute(path: "/documents/1")]
        XCTAssertFalse(router.followWebLink(try url("/documents/42"), from: .documents, relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .documents)
        XCTAssertEqual(router.documentsPath, [WebRoute(path: "/documents/1")])
    }

    func testFollowWebLinkWithinSameMoreTabIsLeftToWebView() throws {
        let router = AppRouter()
        router.selectedTab = .more
        router.morePath = [.web(WebRoute(path: "/events"))]
        XCTAssertFalse(router.followWebLink(try url("/automations"), from: .more, relativeTo: baseURL))
        XCTAssertEqual(router.morePath, [.web(WebRoute(path: "/events"))])
    }

    func testFollowWebLinkCrossesTabsWhenDestinationBelongsElsewhere() throws {
        let router = AppRouter()
        router.selectedTab = .more
        XCTAssertTrue(router.followWebLink(try url("/notes"), from: .more, relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .notes)
        XCTAssertEqual(router.notesRoute, .list)
    }

    func testFollowWebLinkForeignOriginReturnsFalse() throws {
        let router = AppRouter()
        let foreign = try XCTUnwrap(URL(string: "https://evil.test/x"))
        XCTAssertFalse(router.followWebLink(foreign, from: .documents, relativeTo: baseURL))
    }

    // MARK: - reset

    func testResetReturnsToChatRoot() throws {
        let router = AppRouter()
        router.selectedTab = .more
        router.morePath = [.settings]
        router.documentsPath = [WebRoute(path: "/documents/1")]
        router.notesRoute = .add
        router.chatSelection = ChatRoute(conversationID: "c", initialPrompt: "p")
        router.sharedConversationRoute = SharedConversationRoute(token: "token")

        router.reset()

        XCTAssertEqual(router.selectedTab, .chat)
        XCTAssertEqual(router.chatSelection, ChatRoute())
        XCTAssertNil(router.sharedConversationRoute)
        XCTAssertEqual(router.notesRoute, .list)
        XCTAssertEqual(router.documentsPath, [])
        XCTAssertEqual(router.morePath, [])
    }

    // MARK: - URL helpers

    func testPathAndQueryPreservesQueryAndFragment() throws {
        let direct = try XCTUnwrap(URL(string: "https://assistant.example.test/chat?conversation_id=abc123#latest"))
        XCTAssertEqual(direct.pathAndQuery, "/chat?conversation_id=abc123#latest")
    }

    func testNormalizedPathStripsTrailingSlash() throws {
        XCTAssertEqual(try url("/documents/").normalizedPath, "/documents")
        XCTAssertEqual(try url("/documents").normalizedPath, "/documents")
        XCTAssertEqual(try url("/").normalizedPath, "/")
    }

    // MARK: - Shared attachment inbox

    @MainActor
    func testSharedAttachmentInboxCopiesFileURLsForDeferredImport() async throws {
        let source = FileManager.default.temporaryDirectory
            .appendingPathComponent("route-test-\(UUID().uuidString).txt")
        let secondSource = FileManager.default.temporaryDirectory
            .appendingPathComponent("route-test-\(UUID().uuidString)-second.txt")
        try Data("shared text".utf8).write(to: source)
        try Data("second text".utf8).write(to: secondSource)
        let inbox = SharedAttachmentInbox()

        inbox.receive(urls: [source])
        try await waitUntil { inbox.pendingBatch?.fileURLs.count == 1 }
        let firstBatchID = try XCTUnwrap(inbox.pendingBatch?.id)
        inbox.receive(urls: [secondSource])
        try await waitUntil { inbox.pendingBatch?.fileURLs.count == 2 }

        let batch = try XCTUnwrap(inbox.pendingBatch)
        XCTAssertEqual(batch.id, firstBatchID)
        XCTAssertEqual(batch.fileURLs.count, 2)
        XCTAssertTrue(batch.importErrors.isEmpty)
        let imported = try XCTUnwrap(batch.fileURLs.first)
        XCTAssertNotEqual(imported, source)
        XCTAssertEqual(imported.lastPathComponent, source.lastPathComponent)
        XCTAssertEqual(try Data(contentsOf: imported), Data("shared text".utf8))
        XCTAssertTrue(SharedAttachmentInbox.canReceive(imported))
        let secondImported = try XCTUnwrap(batch.fileURLs.last)
        XCTAssertEqual(secondImported.lastPathComponent, secondSource.lastPathComponent)
        XCTAssertEqual(try Data(contentsOf: secondImported), Data("second text".utf8))

        XCTAssertEqual(inbox.consume(batchID: batch.id), batch)
        XCTAssertNil(inbox.pendingBatch)
    }

    @MainActor
    func testSharedAttachmentInboxBuffersAdjacentOpenCallbacksIntoOneBatch() async throws {
        let source = FileManager.default.temporaryDirectory
            .appendingPathComponent("route-buffer-\(UUID().uuidString).txt")
        let secondSource = FileManager.default.temporaryDirectory
            .appendingPathComponent("route-buffer-\(UUID().uuidString)-second.txt")
        try Data("first".utf8).write(to: source)
        try Data("second".utf8).write(to: secondSource)
        let inbox = SharedAttachmentInbox(receiveDebounce: .milliseconds(1))

        inbox.receive(urls: [source])
        inbox.receive(urls: [secondSource])
        try await waitUntil { inbox.pendingBatch?.fileURLs.count == 2 }

        let batch = try XCTUnwrap(inbox.pendingBatch)
        XCTAssertEqual(batch.fileURLs.count, 2)
        XCTAssertEqual(batch.fileURLs[0].lastPathComponent, source.lastPathComponent)
        XCTAssertEqual(batch.fileURLs[1].lastPathComponent, secondSource.lastPathComponent)
        XCTAssertEqual(try Data(contentsOf: batch.fileURLs[0]), Data("first".utf8))
        XCTAssertEqual(try Data(contentsOf: batch.fileURLs[1]), Data("second".utf8))
    }

    @MainActor
    func testSharedAttachmentInboxPreservesImportFailuresInBatch() async throws {
        let inbox = SharedAttachmentInbox()

        inbox.receive(urls: [try XCTUnwrap(URL(string: "https://assistant.example.test/not-a-file.txt"))])
        try await waitUntil { inbox.pendingBatch?.importErrors.isEmpty == false }

        let batch = try XCTUnwrap(inbox.pendingBatch)
        XCTAssertTrue(batch.fileURLs.isEmpty)
        XCTAssertEqual(batch.importErrors.count, 1)
        XCTAssertTrue(batch.importErrors[0].contains("Could not import not-a-file.txt"))
    }

    @MainActor
    func testSharedAttachmentInboxRejectsOversizedFilesBeforeCopying() async throws {
        let source = FileManager.default.temporaryDirectory
            .appendingPathComponent("route-oversized-\(UUID().uuidString).pdf")
        XCTAssertTrue(FileManager.default.createFile(atPath: source.path, contents: Data()))
        let sourceHandle = try FileHandle(forWritingTo: source)
        try sourceHandle.truncate(atOffset: UInt64(ChatConstants.maxAttachmentSizeBytes + 1))
        try sourceHandle.close()
        defer {
            try? FileManager.default.removeItem(at: source)
        }
        let inbox = SharedAttachmentInbox()

        inbox.receive(urls: [source])
        try await waitUntil { inbox.pendingBatch?.importErrors.isEmpty == false }

        let batch = try XCTUnwrap(inbox.pendingBatch)
        XCTAssertTrue(batch.fileURLs.isEmpty)
        XCTAssertEqual(batch.importErrors.count, 1)
        XCTAssertTrue(batch.importErrors[0].contains("Could not import \(source.lastPathComponent)"))
        XCTAssertTrue(batch.importErrors[0].contains("File size exceeds 100MB."))
        XCTAssertFalse(sharedImportExists(named: source.lastPathComponent))
    }

    // MARK: - Opened-URL hand-off

    @MainActor
    func testOpenURLCenterBuffersAndConsumesInOrder() throws {
        let center = OpenURLCenter()
        let first = try XCTUnwrap(URL(string: "familyassistant://chat?q=hello"))
        let second = try XCTUnwrap(URL(string: "file:///tmp/shared.pdf"))

        XCTAssertEqual(center.consumePendingURLs(), [])
        center.receive([first])
        center.receive([second])
        XCTAssertEqual(center.pendingURLs, [first, second])

        XCTAssertEqual(center.consumePendingURLs(), [first, second])
        XCTAssertTrue(center.pendingURLs.isEmpty)
        XCTAssertEqual(center.consumePendingURLs(), [])
    }

    /// The custom quick-action scene delegate replaces SwiftUI's internal scene
    /// delegate, so it is the only receiver of URL-open events. It must forward
    /// them into `OpenURLCenter.shared` for the app to see them at all.
    @MainActor
    func testSceneDelegateForwardsOpenedURLsToSharedCenter() throws {
        _ = OpenURLCenter.shared.consumePendingURLs()
        defer { _ = OpenURLCenter.shared.consumePendingURLs() }
        let url = try XCTUnwrap(URL(string: "familyassistant://chat?q=forwarded"))

        HomeScreenShortcutSceneDelegate.forwardOpenedURLs([url])

        XCTAssertEqual(OpenURLCenter.shared.pendingURLs, [url])
    }

    @MainActor
    func testSceneDelegateForwardsUniversalLinkUserActivityToSharedCenter() throws {
        _ = OpenURLCenter.shared.consumePendingURLs()
        defer { _ = OpenURLCenter.shared.consumePendingURLs() }
        let url = try XCTUnwrap(
            URL(string: "https://assistant.andrewgarrett.dev/shared/conversations/token")
        )
        let activity = NSUserActivity(activityType: NSUserActivityTypeBrowsingWeb)
        activity.webpageURL = url

        HomeScreenShortcutSceneDelegate.forwardUserActivities([activity])

        XCTAssertEqual(OpenURLCenter.shared.pendingURLs, [url])
    }

    // MARK: - More catalog nav-divergence guard

    /// Guards the More destination set against silent drift from the canonical
    /// web nav (`frontend/src/shared/navigation.ts`) minus the entries promoted
    /// to their own tabs (Chat, Notes, Documents list). If you intentionally
    /// change the catalog, update this expected set in the same commit.
    func testMoreCatalogMatchesExpectedDestinationPaths() {
        let actual = MoreCatalog.sections.flatMap { $0.destinations.map(\.path) }
        let expected = [
            "/context",
            "/documents/upload",
            "/vector-search",
            "/history",
            "/automations",
            "/events",
            "/tools",
            "/tasks",
            "/errors",
            "/docs/",
            "/about",
        ]
        XCTAssertEqual(actual, expected)
    }

    func testMoreCatalogPathsAreUnique() {
        let paths = MoreCatalog.sections.flatMap { $0.destinations.map(\.path) }
        XCTAssertEqual(Set(paths).count, paths.count)
    }

    private func waitUntil(
        timeout: TimeInterval = 4,
        predicate: @escaping @MainActor () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if await predicate() {
                return
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTFail("Timed out waiting for predicate")
    }

    private func sharedImportExists(named filename: String) -> Bool {
        let importsRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("SharedAttachmentImports", isDirectory: true)
        guard let enumerator = FileManager.default.enumerator(
            at: importsRoot,
            includingPropertiesForKeys: nil
        ) else {
            return false
        }
        for case let fileURL as URL in enumerator where fileURL.lastPathComponent == filename {
            return true
        }
        return false
    }
}
