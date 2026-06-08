import XCTest

@testable import FamilyAssistant

final class RouteTests: XCTestCase {
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

    // MARK: - owningTab resolution (table-driven)

    func testOwningTabResolvesEachDestination() throws {
        let cases: [(path: String, expected: AppTab?)] = [
            ("/chat", .chat),
            ("/chat?conversation_id=abc&q=hi", .chat),
            ("/notes", .notes),
            ("/notes/edit/Milk", .notes),
            ("/documents/", .documents),
            ("/documents", .documents),
            ("/documents/123", .documents),
            ("/events", .more),
            ("/voice", .more),
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

    func testNavigateForeignOriginReturnsFalse() throws {
        let router = AppRouter()
        let foreign = try XCTUnwrap(URL(string: "https://evil.test/chat"))
        XCTAssertFalse(router.navigate(to: foreign, relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .chat)
    }

    // MARK: - followWebLink (in-page link taps)

    func testFollowWebLinkPushesWithinSameDocumentsTab() throws {
        let router = AppRouter()
        router.selectedTab = .documents
        XCTAssertTrue(router.followWebLink(try url("/documents/42"), from: .documents, relativeTo: baseURL))
        XCTAssertEqual(router.selectedTab, .documents)
        XCTAssertEqual(router.documentsPath, [WebRoute(path: "/documents/42")])
    }

    func testFollowWebLinkAppendsWithinMoreTab() throws {
        let router = AppRouter()
        router.selectedTab = .more
        router.morePath = [.web(WebRoute(path: "/events"))]
        XCTAssertTrue(router.followWebLink(try url("/automations"), from: .more, relativeTo: baseURL))
        XCTAssertEqual(router.morePath, [.web(WebRoute(path: "/events")), .web(WebRoute(path: "/automations"))])
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

        router.reset()

        XCTAssertEqual(router.selectedTab, .chat)
        XCTAssertEqual(router.chatSelection, ChatRoute())
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
            "/voice",
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
}
