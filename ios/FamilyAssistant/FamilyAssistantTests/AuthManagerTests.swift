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

    func testRefreshSkipsPersistWhenOwnerEpochIsSuperseded() async throws {
        seedStoredAuth(apiToken: "old-api-token", refreshToken: "old-refresh-token", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json(
                """
                {"api_token": "rotated", "refresh_token": "rotated-refresh", "expires_in": 7200}
                """
            )
        }

        // A stale owner epoch stands in for a bootstrap superseded by a logout /
        // re-login while its refresh was in flight.
        try await authManager.refreshIfNeeded(ownerEpoch: authManager.authEpoch - 1)

        XCTAssertEqual(AuthBackendURLProtocol.requests.count, 1, "the refresh request is still made")
        XCTAssertEqual(
            KeychainHelper.readString(key: "fa_api_token"),
            "old-api-token",
            "a superseded bootstrap must not overwrite the current session's credentials"
        )
        XCTAssertEqual(KeychainHelper.readString(key: "fa_refresh_token"), "old-refresh-token")
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
            XCTAssertTrue(
                authManager.authRequired,
                "missing credentials on the required path must latch authRequired"
            )
        }
    }

    func testConcurrentRefreshIsSingleFlighted() async throws {
        seedStoredAuth(apiToken: "expiring", refreshToken: "the-refresh-token", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
        }

        // Both callers reach `refreshIfNeeded` while the token is due: the first
        // starts the network refresh and suspends; the second coalesces onto it.
        async let first: Void = authManager.refreshIfNeeded()
        async let second: Void = authManager.refreshIfNeeded()
        _ = try await (first, second)

        let refreshPosts = AuthBackendURLProtocol.requests.filter { $0.url?.path == "/api/auth/refresh" }
        XCTAssertEqual(refreshPosts.count, 1, "two concurrent callers must share exactly one refresh POST")
        XCTAssertEqual(KeychainHelper.readString(key: "fa_api_token"), "rotated")
    }

    func testRefreshRejectionLatchesAuthRequired() async {
        seedStoredAuth(apiToken: "expiring", refreshToken: "stale-refresh", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json("{}", statusCode: 401)
        }

        do {
            try await authManager.refreshIfNeeded()
            XCTFail("Expected refreshIfNeeded to throw on rejection")
        } catch AuthError.authRejected {
            XCTAssertTrue(authManager.authRequired)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testAuthRequiredSignalsReachObserver() async {
        seedStoredAuth(apiToken: "expiring", refreshToken: "stale-refresh", expiresIn: -60)
        let authManager = makeAuthManager()
        var signals: [AuthManager.AuthStateSignal] = []
        authManager.onAuthStateChange = { signals.append($0) }
        AuthBackendURLProtocol.respond { _ in .json("{}", statusCode: 401) }

        try? await authManager.refreshIfNeeded()

        XCTAssertEqual(signals, [.refreshing, .authRequired])
    }

    func testSuccessfulReAuthClearsAuthRequired() async {
        seedStoredAuth(apiToken: "expiring", refreshToken: "stale-refresh", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in .json("{}", statusCode: 401) }
        try? await authManager.refreshIfNeeded()
        XCTAssertTrue(authManager.authRequired)

        // A successful refresh (token still valid) clears the terminal state.
        seedStoredAuth(apiToken: "expiring-again", refreshToken: "good-refresh", expiresIn: -60)
        var signals: [AuthManager.AuthStateSignal] = []
        authManager.onAuthStateChange = { signals.append($0) }
        AuthBackendURLProtocol.respond { _ in
            .json(#"{"api_token":"fresh","refresh_token":"fresh-refresh","expires_in":7200}"#)
        }

        try? await authManager.refreshIfNeeded()

        XCTAssertFalse(authManager.authRequired)
        XCTAssertEqual(signals, [.refreshing, .ok])
    }

    func testLogoutEpochChangeStillSupersedesInFlightRefresh() async throws {
        // A logout during an in-flight refresh must fence out the rotation exactly
        // as before single-flighting: the stale owner epoch stands in for the
        // logout that bumped the epoch while the refresh was on the wire.
        seedStoredAuth(apiToken: "old-api-token", refreshToken: "old-refresh-token", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
        }

        try await authManager.refreshIfNeeded(ownerEpoch: authManager.authEpoch - 1)

        XCTAssertEqual(
            KeychainHelper.readString(key: "fa_api_token"),
            "old-api-token",
            "a refresh superseded by a logout epoch bump must not resurrect credentials"
        )
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

    func testWatchdogAbandonsAStallAndReturnsPromptly() async {
        let authManager = makeAuthManager()

        let start = Date()
        let completed = await authManager.runWithWatchdog(seconds: 0.2) {
            // A non-cancellable stall — a dispatch timer cannot be unstuck by task
            // cancellation, standing in for the WKWebsiteDataStore hand-off this
            // watchdog exists to survive. It resumes far past the watchdog budget,
            // so the test passes ONLY if runWithWatchdog abandons (does not await)
            // the operation; a cancel-then-await regression would wait the full
            // delay and fail the elapsed assertion. (It does eventually resume, to
            // avoid leaking the continuation.)
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
                    continuation.resume()
                }
            }
        }
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertFalse(completed, "watchdog should report the operation did not finish")
        XCTAssertLessThan(elapsed, 2, "watchdog should return shortly after its budget, not wait for the stall")
    }

    func testWatchdogReturnsTrueWhenOperationFinishesInTime() async {
        let authManager = makeAuthManager()

        let completed = await authManager.runWithWatchdog(seconds: 5) {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }

        XCTAssertTrue(completed)
    }

    func testAuthEpochOwnershipIsLostAfterAuthInvalidation() async {
        seedStoredAuth(apiToken: "api-token", refreshToken: "refresh-token", expiresIn: 7200)
        let authManager = makeAuthManager()

        let captured = authManager.authEpoch
        XCTAssertTrue(authManager.isCurrentAuthEpoch(captured))

        // A logout (the case where a watchdog-abandoned bridge could resume and
        // re-add the stale cookie or clear a fresh login) invalidates ownership, so
        // both the cookie write and the auth-rejection clear are fenced out.
        await authManager.logout()

        XCTAssertFalse(
            authManager.isCurrentAuthEpoch(captured),
            "a bridge captured before logout must no longer own the auth state"
        )
    }

    func testStaleCookieSelectionDeletesOnlyOurOwnUnreplacedWrites() {
        let session = "session"
        let domain = "assistant.example.test"
        let path = "/"
        func cookie(_ value: String) -> HTTPCookie {
            HTTPCookie(properties: [
                .name: session, .value: value, .domain: domain, .path: path,
            ])!
        }
        let written = [cookie("OLD")]

        // Our write is still the live value -> delete it (stale, superseded).
        XCTAssertEqual(
            AuthManager.staleCookiesToDelete(written: written, live: [cookie("OLD")]).map(\.value),
            ["OLD"]
        )
        // A fresh login overwrote it with a new value -> never delete the new one.
        XCTAssertTrue(
            AuthManager.staleCookiesToDelete(written: written, live: [cookie("NEW")]).isEmpty
        )
        // Already gone (e.g. logout cleared the store) -> nothing to delete.
        XCTAssertTrue(
            AuthManager.staleCookiesToDelete(written: written, live: []).isEmpty
        )
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
        let handler: Handler? = Self.lock.withLock {
            Self.recordedRequests.append(request)
            return Self.handler
        }

        guard let handler else {
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
