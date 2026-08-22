import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class NotesAPIClientTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private let apiToken = "test-api-token"

    override func setUp() {
        super.setUp()
        resetStoredAuth()
        KeychainHelper.save(key: "fa_api_token", string: apiToken)
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
        URLProtocol.registerClass(MockBackendURLProtocol.self)
        MockBackendURLProtocol.reset()
    }

    override func tearDown() {
        MockBackendURLProtocol.reset()
        URLProtocol.unregisterClass(MockBackendURLProtocol.self)
        resetStoredAuth()
        super.tearDown()
    }

    func testListNotesSendsAuthorizedRequestAndDecodesResponse() async throws {
        MockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/api/notes")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            return .json(
                """
                [
                  {
                    "title": "Shopping",
                    "content": "Buy milk",
                    "include_in_prompt": false,
                    "attachment_ids": ["receipt"],
                    "visibility_labels": ["family"]
                  }
                ]
                """
            )
        }

        let notes = try await makeClient().listNotes()

        XCTAssertEqual(MockBackendURLProtocol.requests.count, 1)
        XCTAssertEqual(notes.count, 1)
        let note = try XCTUnwrap(notes.first)
        XCTAssertEqual(note.title, "Shopping")
        XCTAssertEqual(note.content, "Buy milk")
        XCTAssertFalse(note.includeInPrompt)
        XCTAssertEqual(note.attachmentIds, ["receipt"])
        XCTAssertEqual(note.visibilityLabels, ["family"])
        XCTAssertFalse(note.isSkill)
        XCTAssertNil(note.skillName)
        XCTAssertNil(note.skillDescription)
    }

    func testGetNoteEncodesTitleAsOnePathComponent() async throws {
        let title = "Wi-Fi Password / meds?#"
        MockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(
                Self.percentEncodedPath(from: request),
                "/api/notes/Wi-Fi%20Password%20%2F%20meds%3F%23"
            )
            return .json(
                """
                {
                  "title": "\(title)",
                  "content": "Keep it private."
                }
                """
            )
        }

        let note = try await makeClient().getNote(title: title)

        XCTAssertEqual(note.title, title)
        XCTAssertEqual(note.content, "Keep it private.")
    }

    func testSaveNotePostsServerPayload() async throws {
        MockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/notes")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")

            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: Any])
            XCTAssertEqual(payload["title"] as? String, "School")
            XCTAssertEqual(payload["content"] as? String, "Early pickup")
            XCTAssertEqual(payload["include_in_prompt"] as? Bool, true)
            XCTAssertEqual(payload["original_title"] as? String, "Old School")
            XCTAssertEqual(payload["attachment_ids"] as? [String], ["calendar"])
            XCTAssertEqual(payload["visibility_labels"] as? [String], ["parents"])
            return .json("{}")
        }

        try await makeClient().saveNote(
            NativeNoteSaveRequest(
                title: "School",
                content: "Early pickup",
                includeInPrompt: true,
                originalTitle: "Old School",
                attachmentIds: ["calendar"],
                visibilityLabels: ["parents"]
            )
        )

        XCTAssertEqual(MockBackendURLProtocol.requests.count, 1)
    }

    func testDeleteNoteSendsEncodedDeleteRequest() async throws {
        MockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "DELETE")
            XCTAssertEqual(Self.percentEncodedPath(from: request), "/api/notes/Meal%20Plan%2FSunday")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(self.apiToken)")
            return .json("{}")
        }

        try await makeClient().deleteNote(title: "Meal Plan/Sunday")

        XCTAssertEqual(MockBackendURLProtocol.requests.count, 1)
    }

    func testServerErrorUsesBackendDetail() async throws {
        MockBackendURLProtocol.respond { _ in
            .json(#"{"detail":"Note title already exists."}"#, statusCode: 409)
        }

        do {
            try await makeClient().saveNote(
                NativeNoteSaveRequest(
                    title: "Shopping",
                    content: "Buy milk",
                    includeInPrompt: true,
                    originalTitle: nil,
                    attachmentIds: [],
                    visibilityLabels: []
                )
            )
            XCTFail("Expected saveNote to throw")
        } catch let NotesAPIError.server(statusCode, detail) {
            XCTAssertEqual(statusCode, 409)
            XCTAssertEqual(detail, "Note title already exists.")
        }
    }

    func testHTMLResponseSurfacesAuthWallInsteadOfDecodeError() async throws {
        MockBackendURLProtocol.respond { _ in
            .json(
                "<html><head><title>Just a moment...</title></head><body></body></html>",
                headers: ["Content-Type": "text/html; charset=utf-8"]
            )
        }

        do {
            _ = try await makeClient().listNotes()
            XCTFail("Expected an HTML login page to throw authWall")
        } catch NotesAPIError.authWall {}
    }

    func testMarkupBodyWithJSONContentTypeSurfacesAuthWall() async throws {
        MockBackendURLProtocol.respond { _ in
            .json(
                "\n  <html><body>Sign in</body></html>",
                headers: ["Content-Type": "application/json"]
            )
        }

        do {
            _ = try await makeClient().listNotes()
            XCTFail("Expected HTML markup in the body to throw authWall")
        } catch NotesAPIError.authWall {}
    }

    private func makeClient() -> NotesAPIClient {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return NotesAPIClient(authManager: authManager)
    }

    private func resetStoredAuth() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
    }

    private static func jsonObject(from request: URLRequest) throws -> Any {
        let bodyData = try XCTUnwrap(request.httpBody ?? Data(reading: request.httpBodyStream))
        return try JSONSerialization.jsonObject(with: bodyData)
    }

    private static func percentEncodedPath(from request: URLRequest) -> String? {
        guard let url = request.url else { return nil }
        return URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath
    }
}

private final class MockBackendURLProtocol: URLProtocol {
    typealias Handler = (URLRequest) throws -> MockResponse

    private static let lock = NSLock()
    private static var handler: Handler?
    private static var recordedRequests: [URLRequest] = []

    static var requests: [URLRequest] {
        lock.withLock { recordedRequests }
    }

    static func respond(with handler: @escaping Handler) {
        lock.withLock {
            self.handler = handler
        }
    }

    static func reset() {
        lock.withLock {
            handler = nil
            recordedRequests = []
        }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "assistant.example.test"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.lock.withLock {
            Self.recordedRequests.append(request)
        }

        guard let handler = Self.lock.withLock({ Self.handler }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }

        do {
            let response = try handler(request)
            client?.urlProtocol(self, didReceive: response.urlResponse(for: request), cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: response.data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private struct MockResponse {
    let statusCode: Int
    let data: Data
    let headers: [String: String]

    static func json(
        _ json: String,
        statusCode: Int = 200,
        headers extraHeaders: [String: String] = [:]
    ) -> MockResponse {
        var headers = ["Content-Type": "application/json"]
        headers.merge(extraHeaders) { _, new in new }
        return MockResponse(statusCode: statusCode, data: Data(json.utf8), headers: headers)
    }

    func urlResponse(for request: URLRequest) -> HTTPURLResponse {
        HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!
    }
}

private extension Data {
    init?(reading stream: InputStream?) {
        guard let stream else { return nil }
        self.init()
        stream.open()
        defer { stream.close() }

        let bufferSize = 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }

        while stream.hasBytesAvailable {
            let bytesRead = stream.read(buffer, maxLength: bufferSize)
            if bytesRead < 0 {
                return nil
            }
            append(buffer, count: bytesRead)
        }
    }
}
