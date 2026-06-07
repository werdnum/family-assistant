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

    func testNotesRouteRejectsDifferentOrigin() throws {
        let url = try XCTUnwrap(URL(string: "https://example.test/notes"))

        XCTAssertNil(NotesRoute.route(for: url, relativeTo: baseURL))
    }

    func testRouterClaimsNativeNotesRoutes() throws {
        let router = AppRouter()
        let url = try XCTUnwrap(URL(string: "https://assistant.example.test/notes"))

        XCTAssertTrue(router.openNativeURL(url, relativeTo: baseURL))
        XCTAssertEqual(router.route, .notes(.list))
    }
}
