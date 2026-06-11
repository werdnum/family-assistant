import Foundation
import XCTest

@testable import FamilyAssistant

final class ErrorReporterTests: XCTestCase {
    private var spoolDirectory: URL!
    private let baseURL = URL(string: "https://errors.example.test")!

    override func setUp() {
        super.setUp()
        spoolDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ErrorReporterTests-\(UUID().uuidString)", isDirectory: true)
        MockErrorURLProtocol.reset()
    }

    override func tearDown() {
        MockErrorURLProtocol.reset()
        if let spoolDirectory {
            try? FileManager.default.removeItem(at: spoolDirectory)
        }
        super.tearDown()
    }

    func testDeliverSendsPayloadToErrorsEndpoint() async throws {
        MockErrorURLProtocol.respond { _ in .init(statusCode: 200) }
        let reporter = makeReporter()
        reporter.configure { self.baseURL }

        await reporter.deliver(
            message: "Boom",
            component: "Notes.editor.save",
            errorType: .handled,
            stack: nil,
            extraData: ["error_type_name": "NotesAPIError"]
        )

        XCTAssertEqual(MockErrorURLProtocol.requests.count, 1)
        let request = try XCTUnwrap(MockErrorURLProtocol.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.absoluteString, "https://errors.example.test/api/errors/")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")

        let payload = try XCTUnwrap(MockErrorURLProtocol.decodedBodies.first)
        XCTAssertEqual(payload.message, "Boom")
        XCTAssertEqual(payload.componentName, "Notes.editor.save")
        XCTAssertEqual(payload.errorType, "manual")
        XCTAssertEqual(payload.url, "familyassistant://ios/Notes.editor.save")
        XCTAssertEqual(payload.extraData?["platform"], "ios")
        XCTAssertEqual(payload.extraData?["error_type_name"], "NotesAPIError")
        XCTAssertEqual(try XCTUnwrap(payload.userAgent).hasPrefix("FamilyAssistant-iOS/"), true)
        XCTAssertTrue(spooledFiles().isEmpty)
    }

    func testDeliverDeduplicatesWithinWindow() async throws {
        MockErrorURLProtocol.respond { _ in .init(statusCode: 200) }
        let reporter = makeReporter()
        reporter.configure { self.baseURL }

        for _ in 0..<3 {
            await reporter.deliver(
                message: "Same",
                component: "Chat.messages",
                errorType: .handled,
                stack: nil,
                extraData: [:]
            )
        }

        XCTAssertEqual(MockErrorURLProtocol.requests.count, 1)
    }

    func testDistinctComponentsAreNotDeduplicated() async throws {
        MockErrorURLProtocol.respond { _ in .init(statusCode: 200) }
        let reporter = makeReporter()
        reporter.configure { self.baseURL }

        await reporter.deliver(message: "Same", component: "A", errorType: .handled, stack: nil, extraData: [:])
        await reporter.deliver(message: "Same", component: "B", errorType: .handled, stack: nil, extraData: [:])

        XCTAssertEqual(MockErrorURLProtocol.requests.count, 2)
    }

    func testDeliverPersistsWhenBaseURLUnknown() async throws {
        MockErrorURLProtocol.respond { _ in .init(statusCode: 200) }
        let reporter = makeReporter()
        // No base URL configured (e.g. error before sign-in).

        await reporter.deliver(
            message: "Pre-login failure",
            component: "Auth.callback",
            errorType: .handled,
            stack: nil,
            extraData: [:]
        )

        XCTAssertEqual(MockErrorURLProtocol.requests.count, 0)
        XCTAssertEqual(spooledFiles().count, 1)
    }

    func testDeliverPersistsOnServerError() async throws {
        MockErrorURLProtocol.respond { _ in .init(statusCode: 500) }
        let reporter = makeReporter()
        reporter.configure { self.baseURL }

        await reporter.deliver(
            message: "Server down",
            component: "Chat.stream",
            errorType: .handled,
            stack: nil,
            extraData: [:]
        )

        XCTAssertEqual(MockErrorURLProtocol.requests.count, 1)
        XCTAssertEqual(spooledFiles().count, 1)
    }

    func testFlushPersistedDeliversAndRemovesSpooledReports() async throws {
        // First, spool a report by delivering with no base URL.
        let reporter = makeReporter()
        await reporter.deliver(
            message: "Queued",
            component: "Notes.list.load",
            errorType: .handled,
            stack: nil,
            extraData: [:]
        )
        XCTAssertEqual(spooledFiles().count, 1)

        // Now the server is reachable: flushing should deliver and clean up.
        MockErrorURLProtocol.respond { _ in .init(statusCode: 200) }
        reporter.configure { self.baseURL }
        await reporter.flushPersisted()

        XCTAssertEqual(MockErrorURLProtocol.requests.count, 1)
        let payload = try XCTUnwrap(MockErrorURLProtocol.decodedBodies.first)
        XCTAssertEqual(payload.message, "Queued")
        XCTAssertTrue(spooledFiles().isEmpty)
    }

    func testFlushPersistedKeepsReportsWhenServerStillFailing() async throws {
        let reporter = makeReporter()
        await reporter.deliver(
            message: "Queued",
            component: "Notes.list.load",
            errorType: .handled,
            stack: nil,
            extraData: [:]
        )

        MockErrorURLProtocol.respond { _ in .init(statusCode: 503) }
        reporter.configure { self.baseURL }
        await reporter.flushPersisted()

        XCTAssertEqual(spooledFiles().count, 1)
    }

    // MARK: - Helpers

    private func makeReporter() -> ErrorReporter {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockErrorURLProtocol.self]
        return ErrorReporter(
            session: URLSession(configuration: configuration),
            spoolDirectory: spoolDirectory,
            dedupeWindow: 60
        )
    }

    private func spooledFiles() -> [URL] {
        (try? FileManager.default.contentsOfDirectory(at: spoolDirectory, includingPropertiesForKeys: nil))?
            .filter { $0.pathExtension == "json" } ?? []
    }
}

private final class MockErrorURLProtocol: URLProtocol {
    struct Response {
        let statusCode: Int
    }

    typealias Handler = (URLRequest) -> Response

    private static let lock = NSLock()
    private static var handler: Handler?
    private static var recordedRequests: [URLRequest] = []

    static var requests: [URLRequest] {
        lock.withLock { recordedRequests }
    }

    static var decodedBodies: [ErrorReportPayload] {
        lock.withLock {
            recordedRequests.compactMap { request in
                guard let body = request.httpBody ?? Data(reading: request.httpBodyStream) else {
                    return nil
                }
                return try? JSONDecoder().decode(ErrorReportPayload.self, from: body)
            }
        }
    }

    static func respond(with handler: @escaping Handler) {
        lock.withLock { self.handler = handler }
    }

    static func reset() {
        lock.withLock {
            handler = nil
            recordedRequests = []
        }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "errors.example.test"
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

        let response = handler(request)
        let httpResponse = HTTPURLResponse(
            url: request.url!,
            statusCode: response.statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: httpResponse, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("{\"status\":\"reported\"}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
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
