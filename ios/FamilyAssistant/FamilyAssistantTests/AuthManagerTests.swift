import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class AuthManagerTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"

    override func setUp() {
        super.setUp()
        resetStoredAuth()
        URLProtocol.registerClass(AuthBackendURLProtocol.self)
        AuthBackendURLProtocol.reset()
    }

    override func tearDown() {
        AuthBackendURLProtocol.reset()
        URLProtocol.unregisterClass(AuthBackendURLProtocol.self)
        resetStoredAuth()
        super.tearDown()
    }

    func testValidatedServerURLAddsSchemeAndTrimsTrailingSlash() {
        let authManager = AuthManager()
        authManager.serverURL = " assistant.example.test/ "

        XCTAssertEqual(authManager.validatedServerURL()?.absoluteString, serverURL)
    }

    func testSaveServerURLPersistsForNextManager() {
        let authManager = AuthManager()
        authManager.serverURL = serverURL

        authManager.saveServerURL()

        XCTAssertEqual(AuthManager().serverURL, serverURL)
    }

    func testAuthorizedRequestUsesFreshStoredTokenWithoutRefreshing() async throws {
        seedStoredAuth(apiToken: "fresh-api-token", refreshToken: "refresh-token", expiresIn: 7200)
        let authManager = makeAuthManager()
        let url = try XCTUnwrap(URL(string: "\(serverURL)/api/notes"))

        let request = try await authManager.authorizedRequest(url: url, method: "GET")

        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fresh-api-token")
        XCTAssertTrue(AuthBackendURLProtocol.requests.isEmpty)
    }

    func testRefreshIfNeededPostsRefreshTokenAndStoresReturnedCredentials() async throws {
        seedStoredAuth(apiToken: "expired-api-token", refreshToken: "stored-refresh-token", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/auth/refresh")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")

            let payload = try XCTUnwrap(Self.jsonObject(from: request) as? [String: String])
            XCTAssertEqual(payload["refresh_token"], "stored-refresh-token")
            return .json(
                """
                {
                  "api_token": "new-api-token",
                  "refresh_token": "new-refresh-token",
                  "expires_in": 7200
                }
                """
            )
        }

        try await authManager.refreshIfNeeded()

        XCTAssertEqual(AuthBackendURLProtocol.requests.count, 1)
        XCTAssertEqual(KeychainHelper.readString(key: "fa_api_token"), "new-api-token")
        XCTAssertEqual(KeychainHelper.readString(key: "fa_refresh_token"), "new-refresh-token")
        XCTAssertNotNil(UserDefaults.standard.string(forKey: "fa_token_expiry"))
    }

    func testAuthorizedRequestClearsCredentialsWhenRefreshCredentialsAreMissing() async throws {
        KeychainHelper.save(key: "fa_api_token", string: "api-token-without-refresh")
        let authManager = makeAuthManager()
        let url = try XCTUnwrap(URL(string: "\(serverURL)/api/notes"))

        do {
            _ = try await authManager.authorizedRequest(url: url, method: "GET")
            XCTFail("Expected authorizedRequest to throw")
        } catch AuthError.noCredentials {
            XCTAssertNil(KeychainHelper.readString(key: "fa_api_token"))
            XCTAssertFalse(authManager.isAuthenticated)
        }
    }

    func testEstablishSessionPostsBearerTokenAndAcceptsCookieResponse() async throws {
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/auth/token-session")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer bridge-token")
            return .json("{}", headers: ["Set-Cookie": "session=abc123; Path=/; HttpOnly"])
        }

        try await authManager.establishSession(apiToken: "bridge-token")

        XCTAssertEqual(AuthBackendURLProtocol.requests.count, 1)
    }

    func testEstablishSessionMapsRejectedResponseToAuthRejected() async throws {
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json("{}", statusCode: 403)
        }

        do {
            try await authManager.establishSession(apiToken: "bad-token")
            XCTFail("Expected establishSession to throw")
        } catch AuthError.authRejected {
            XCTAssertEqual(AuthBackendURLProtocol.requests.count, 1)
        }
    }

    func testBootstrapSessionClearsLocalStateWhenRefreshIsRejected() async {
        seedStoredAuth(apiToken: "expired-api-token", refreshToken: "stored-refresh-token", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/auth/refresh")
            return .json("{}", statusCode: 401)
        }

        await authManager.bootstrapSession()

        XCTAssertFalse(authManager.isAuthenticated)
        XCTAssertFalse(authManager.isBootstrapping)
        XCTAssertNil(KeychainHelper.readString(key: "fa_api_token"))
        XCTAssertNil(KeychainHelper.readString(key: "fa_refresh_token"))
        XCTAssertNil(UserDefaults.standard.string(forKey: "fa_token_expiry"))
    }

    func testBootstrapSessionCompletesNormallyWhenSessionBridgeSucceeds() async {
        seedStoredAuth(apiToken: "valid-api-token", refreshToken: "refresh-token", expiresIn: 7200)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/auth/token-session")
            return .json("{}", headers: ["Set-Cookie": "session=abc123; Path=/; HttpOnly"])
        }

        await authManager.bootstrapSession()

        XCTAssertFalse(authManager.isBootstrapping)
        XCTAssertTrue(authManager.isAuthenticated)
        XCTAssertEqual(KeychainHelper.readString(key: "fa_api_token"), "valid-api-token")
    }

    func testBootstrapSessionWatchdogStopsHangAndKeepsStoredCredentials() async {
        seedStoredAuth(apiToken: "valid-api-token", refreshToken: "refresh-token", expiresIn: 7200)
        let authManager = makeAuthManager()
        authManager.bootstrapWatchdogSeconds = 0.2
        // Respond far later than the watchdog so the session bridge would otherwise
        // pin "Signing in…"; the watchdog must abandon it and return promptly.
        AuthBackendURLProtocol.setResponseDelay(3)
        AuthBackendURLProtocol.respond { _ in .json("{}") }

        let start = Date()
        await authManager.bootstrapSession()
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertLessThan(elapsed, 2, "watchdog should abandon the stalled bridge well before the response")
        XCTAssertFalse(authManager.isBootstrapping)
        // Stored credentials are preserved so the app proceeds and can re-auth lazily.
        XCTAssertTrue(authManager.isAuthenticated)
        XCTAssertEqual(KeychainHelper.readString(key: "fa_api_token"), "valid-api-token")
    }

    func testAuthSupportTypesExposeExpectedUserFacingValues() {
        XCTAssertEqual(AuthError.invalidServerURL.errorDescription, "Invalid server URL")
        XCTAssertEqual(AuthError.exchangeFailed.errorDescription, "Failed to exchange authorization code")
        XCTAssertEqual(AuthError.authRejected.errorDescription, "Server rejected stored credentials")
        XCTAssertEqual(AuthError.noCredentials.errorDescription, "No stored credentials")
        XCTAssertEqual(
            AuthError.transient(underlying: nil).errorDescription,
            "Temporary network or server error"
        )
        XCTAssertTrue(
            AuthError.transient(underlying: URLError(.timedOut)).errorDescription?
                .contains("Temporary failure") == true
        )
        XCTAssertEqual(Data([0xfb, 0xff]).base64URLEncoded, "-_8")
    }

    private func makeAuthManager() -> AuthManager {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return authManager
    }

    private func seedStoredAuth(apiToken: String, refreshToken: String, expiresIn: TimeInterval) {
        KeychainHelper.save(key: "fa_api_token", string: apiToken)
        KeychainHelper.save(key: "fa_refresh_token", string: refreshToken)
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(expiresIn)),
            forKey: "fa_token_expiry"
        )
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
}

private final class AuthBackendURLProtocol: URLProtocol {
    typealias Handler = (URLRequest) throws -> AuthMockResponse

    private static let lock = NSLock()
    private static var handler: Handler?
    private static var recordedRequests: [URLRequest] = []
    private static var responseDelay: TimeInterval = 0

    private let cancelLock = NSLock()
    private var cancelled = false

    static var requests: [URLRequest] {
        lock.withLock { recordedRequests }
    }

    static func respond(with handler: @escaping Handler) {
        lock.withLock {
            self.handler = handler
        }
    }

    /// Delay every response by `seconds` so a test can drive `bootstrapSession`'s
    /// watchdog (set a short watchdog and a longer response delay).
    static func setResponseDelay(_ seconds: TimeInterval) {
        lock.withLock { responseDelay = seconds }
    }

    static func reset() {
        lock.withLock {
            handler = nil
            recordedRequests = []
            responseDelay = 0
        }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "assistant.example.test"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        let (handler, delay): (Handler?, TimeInterval) = Self.lock.withLock {
            Self.recordedRequests.append(request)
            return (Self.handler, Self.responseDelay)
        }

        guard let handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }

        let deliver: () -> Void = { [weak self] in
            guard let self, !self.cancelLock.withLock({ self.cancelled }) else { return }
            do {
                let response = try handler(self.request)
                self.client?.urlProtocol(
                    self,
                    didReceive: response.urlResponse(for: self.request),
                    cacheStoragePolicy: .notAllowed
                )
                self.client?.urlProtocol(self, didLoad: response.data)
                self.client?.urlProtocolDidFinishLoading(self)
            } catch {
                self.client?.urlProtocol(self, didFailWithError: error)
            }
        }

        if delay > 0 {
            DispatchQueue.global().asyncAfter(deadline: .now() + delay, execute: deliver)
        } else {
            deliver()
        }
    }

    override func stopLoading() {
        cancelLock.withLock { cancelled = true }
    }
}

private struct AuthMockResponse {
    let statusCode: Int
    let data: Data
    let headers: [String: String]

    static func json(
        _ json: String,
        statusCode: Int = 200,
        headers: [String: String] = [:]
    ) -> AuthMockResponse {
        AuthMockResponse(statusCode: statusCode, data: Data(json.utf8), headers: headers)
    }

    func urlResponse(for request: URLRequest) -> HTTPURLResponse {
        var headerFields = headers
        headerFields["Content-Type"] = headerFields["Content-Type"] ?? "application/json"
        return HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: headerFields
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
