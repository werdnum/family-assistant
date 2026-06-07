import XCTest

@testable import FamilyAssistant

final class RouteTests: XCTestCase {
    private let baseURL = URL(string: "https://assistant.example.test")!

    func testNotesListRouteMatchesSameOriginNotesPath() throws {
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test/notes"))

        XCTAssertEqual(NotesRoute.route(for: url, relativeTo: baseURL), .list)
    }

    func testNotesAddRouteMatchesSameOriginAddPath() throws {
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test/notes/add"))

        XCTAssertEqual(NotesRoute.route(for: url, relativeTo: baseURL), .add)
    }

    func testNotesEditRouteDecodesEncodedTitle() throws {
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test/notes/edit/Wi-Fi%20Password"))

        XCTAssertEqual(NotesRoute.route(for: url, relativeTo: baseURL), .edit(title: "Wi-Fi Password"))
    }

    func testNotesEditRouteRejectsMissingTitle() throws {
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test/notes/edit/"))

        XCTAssertNil(NotesRoute.route(for: url, relativeTo: baseURL))
    }

    func testNotesRouteAcceptsTrailingSlashAndDefaultPort() throws {
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test:443/notes/"))

        XCTAssertEqual(NotesRoute.route(for: url, relativeTo: baseURL), .list)
    }

    func testNotesRouteRejectsDifferentOrigin() throws {
        let url = try XCTUnwrap(URL(string: "https://example.test/notes"))

        XCTAssertNil(NotesRoute.route(for: url, relativeTo: baseURL))
    }

    func testNotesRouteRejectsDifferentPort() throws {
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test:444/notes"))

        XCTAssertNil(NotesRoute.route(for: url, relativeTo: baseURL))
    }

    func testRouterClaimsNativeNotesRoutes() throws {
        let router = AppRouter()
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test/notes"))

        XCTAssertTrue(router.openNativeURL(url, relativeTo: baseURL))
        XCTAssertEqual(router.route, .notes(.list))
    }

    func testRouterKeepsWebRouteWhenURLIsNotNative() throws {
        let router = AppRouter()
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test/chat"))

        XCTAssertFalse(router.openNativeURL(url, relativeTo: baseURL))
        XCTAssertEqual(router.route, .web(path: "/chat"))
    }

    func testRouterExplicitRouteMethodsUpdateState() {
        let router = AppRouter()

        router.openWebPath("/chat?conversation_id=abc123")
        XCTAssertEqual(router.route, .web(path: "/chat?conversation_id=abc123"))

        router.openNotesList()
        XCTAssertEqual(router.route, .notes(.list))

        router.reset()
        XCTAssertEqual(router.route, .web(path: "/chat"))
    }

    func testPathAndQueryPreservesQueryAndFragment() throws {
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test/chat?conversation_id=abc123#latest"))

        XCTAssertEqual(url.pathAndQuery, "/chat?conversation_id=abc123#latest")
    }
}
