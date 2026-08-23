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
        UserDefaults.standard.set(1800, forKey: "fa_token_lifetime")
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
                  "expires_in": 3600
                }
                """
            )
        }

        try await authManager.refreshIfNeeded()

        XCTAssertEqual(AuthBackendURLProtocol.requests.count, 1)
        XCTAssertEqual(KeychainHelper.readString(key: "fa_api_token"), "new-api-token")
        XCTAssertEqual(KeychainHelper.readString(key: "fa_refresh_token"), "new-refresh-token")
        XCTAssertNotNil(UserDefaults.standard.string(forKey: "fa_token_expiry"))
        XCTAssertEqual(UserDefaults.standard.object(forKey: "fa_token_lifetime") as? Int, 3600)
    }

    func testValidAccessTokenRefreshesExpiredStoredToken() async throws {
        seedStoredAuth(apiToken: "expired-api-token", refreshToken: "stored-refresh-token", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json(#"{"api_token":"fresh-api-token","refresh_token":"fresh-refresh","expires_in":3600}"#)
        }

        let token = try await authManager.validAccessToken()

        XCTAssertEqual(token, "fresh-api-token")
        XCTAssertEqual(AuthBackendURLProtocol.requests.count, 1)
        XCTAssertEqual(AuthBackendURLProtocol.requests.first?.url?.path, "/api/auth/refresh")
    }

    func testValidAccessTokenSurfacesRefreshAuthWallWithoutClearingCredentials() async throws {
        seedStoredAuth(apiToken: "expired-api-token", refreshToken: "stored-refresh-token", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json("<html><body>Sign in</body></html>")
        }

        do {
            _ = try await authManager.validAccessToken()
            XCTFail("Expected refresh markup to surface as an authentication wall")
        } catch AuthError.authWall {}

        XCTAssertEqual(KeychainHelper.readString(key: "fa_api_token"), "expired-api-token")
        XCTAssertEqual(KeychainHelper.readString(key: "fa_refresh_token"), "stored-refresh-token")
        XCTAssertFalse(authManager.authRequired)
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
            XCTAssertTrue(
                authManager.isAuthenticated,
                "shell must stay mounted so in-place sign-in can recover the session"
            )
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

    func testForcedRefreshSkipsRejectionForAlreadyRotatedToken() async throws {
        seedStoredAuth(apiToken: "rotated", refreshToken: "rotated-refresh", expiresIn: 7200)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            XCTFail("An already-recovered rejection must not issue another refresh")
            return .json("{}")
        }

        try await authManager.refreshIfNeeded(
            force: true,
            rejectedAccessToken: "rejected-token"
        )

        XCTAssertTrue(AuthBackendURLProtocol.requests.isEmpty)
        XCTAssertEqual(KeychainHelper.readString(key: "fa_api_token"), "rotated")
    }

    func testLogoutUsesInjectedWebsiteDataCleaner() async {
        seedStoredAuth(apiToken: "stored", refreshToken: "stored-refresh", expiresIn: 7200)
        var cleanedWebsiteData = false
        let authManager = AuthManager(websiteDataCleaner: {
            cleanedWebsiteData = true
        })
        authManager.serverURL = serverURL
        AuthBackendURLProtocol.respond { _ in .json("{}") }

        await authManager.logout()

        XCTAssertTrue(cleanedWebsiteData)
        XCTAssertFalse(authManager.isAuthenticated)
        XCTAssertNil(KeychainHelper.readString(key: "fa_api_token"))
        XCTAssertNil(KeychainHelper.readString(key: "fa_refresh_token"))
    }

    func testNonForcedRefreshAwaitsInFlightForcedRefresh() async throws {
        // A forced refresh (the response-time-401 path) rotates the token even
        // though the stored expiry still looks fresh. A concurrent non-forced
        // caller must NOT short-circuit at the freshness check and send the token
        // the server just rejected — it must await the in-flight forced refresh and
        // observe the rotated credentials.
        seedStoredAuth(apiToken: "rejected-but-unexpired", refreshToken: "the-refresh", expiresIn: 7200)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":7200}"#)
        }
        let gate = AuthBackendURLProtocol.installResponseGate()

        // Start the forced refresh and let it reach the (gated) network POST so
        // `inFlightRefresh` is latched before the non-forced caller runs.
        async let forced: Void = authManager.refreshIfNeeded(force: true)
        try await waitUntil(timeout: 2) { !AuthBackendURLProtocol.requests.isEmpty }

        async let concurrent: Void = authManager.refreshIfNeeded()
        // The concurrent caller must be parked on the in-flight task; signal the
        // gate to allow the forced refresh to complete and return.
        gate.signal()

        _ = try await (forced, concurrent)

        let refreshPosts = AuthBackendURLProtocol.requests.filter { $0.url?.path == "/api/auth/refresh" }
        XCTAssertEqual(
            refreshPosts.count,
            1,
            "the non-forced caller must coalesce onto the in-flight forced refresh, not issue its own"
        )
        XCTAssertEqual(
            KeychainHelper.readString(key: "fa_api_token"),
            "rotated",
            "the non-forced caller must return only after observing the rotated token"
        )
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

    func testForcedRefreshWithNoRefreshTokenLatchesAuthRequired() async {
        // An api token exists but no refresh token: the forced response-time-401
        // retry path reaches `performRefresh` directly (bypassing
        // `authorizedRequest`) and hits the no-credentials branch. That branch
        // must latch the terminal auth state and emit the `.authRequired` signal so
        // the coordinator surfaces the re-auth affordance rather than nothing.
        KeychainHelper.save(key: "fa_api_token", string: "api-token-without-refresh")
        let authManager = makeAuthManager()
        var signals: [AuthManager.AuthStateSignal] = []
        authManager.addAuthStateObserver { signals.append($0) }

        do {
            try await authManager.refreshIfNeeded(force: true)
            XCTFail("Expected refreshIfNeeded to throw with no refresh token")
        } catch AuthError.noCredentials {
            XCTAssertTrue(
                authManager.authRequired,
                "a forced refresh with no refresh token must latch the terminal auth state"
            )
            // The observer receives the current state (`.ok`) immediately on
            // registration, then the `.authRequired` latch.
            XCTAssertEqual(signals, [.ok, .authRequired])
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testAuthRequiredSignalsReachObserver() async {
        seedStoredAuth(apiToken: "expiring", refreshToken: "stale-refresh", expiresIn: -60)
        let authManager = makeAuthManager()
        var signals: [AuthManager.AuthStateSignal] = []
        authManager.addAuthStateObserver { signals.append($0) }
        AuthBackendURLProtocol.respond { _ in .json("{}", statusCode: 401) }

        try? await authManager.refreshIfNeeded()

        XCTAssertEqual(signals, [.ok, .refreshing, .authRequired])
    }

    func testObserverRegisteredAfterLatchLearnsAuthRequiredImmediately() async {
        // SwiftUI can build a fresh ChatViewModel (and its coordinator) after
        // `authRequired` has already latched. Its observer, registered post-latch,
        // must be told the current state at once rather than defaulting to `.ok`
        // and never learning re-auth is required until the next transition.
        seedStoredAuth(apiToken: "expiring", refreshToken: "stale-refresh", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in .json("{}", statusCode: 401) }
        try? await authManager.refreshIfNeeded()
        XCTAssertTrue(authManager.authRequired)

        var lateSignals: [AuthManager.AuthStateSignal] = []
        authManager.addAuthStateObserver { lateSignals.append($0) }

        XCTAssertEqual(lateSignals, [.authRequired])
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
        authManager.addAuthStateObserver { signals.append($0) }
        AuthBackendURLProtocol.respond { _ in
            .json(#"{"api_token":"fresh","refresh_token":"fresh-refresh","expires_in":7200}"#)
        }

        try? await authManager.refreshIfNeeded()

        XCTAssertFalse(authManager.authRequired)
        // Registered while `authRequired` was already latched, so the observer
        // first learns the current `.authRequired` state, then the re-auth's
        // `.refreshing`/`.ok`.
        XCTAssertEqual(signals, [.authRequired, .refreshing, .ok])
    }

    func testMultipleAuthStateObserversAllReceiveSignals() async {
        // SwiftUI can construct a fresh ChatViewModel (and coordinator) while
        // @State retains the original, so a single rebindable callback would leave
        // the on-screen model's coordinator without auth signals. Every live
        // observer must receive every transition.
        seedStoredAuth(apiToken: "expiring", refreshToken: "stale-refresh", expiresIn: -60)
        let authManager = makeAuthManager()
        var firstSignals: [AuthManager.AuthStateSignal] = []
        var secondSignals: [AuthManager.AuthStateSignal] = []
        authManager.addAuthStateObserver { firstSignals.append($0) }
        authManager.addAuthStateObserver { secondSignals.append($0) }
        AuthBackendURLProtocol.respond { _ in .json("{}", statusCode: 401) }

        try? await authManager.refreshIfNeeded()

        XCTAssertEqual(firstSignals, [.ok, .refreshing, .authRequired])
        XCTAssertEqual(secondSignals, [.ok, .refreshing, .authRequired])
    }

    func testRemovedAuthStateObserverStopsReceivingSignalsWhileOthersContinue() async {
        // Deallocating one view model (which removes its observer by token) must not
        // break signal delivery to the still-live model.
        seedStoredAuth(apiToken: "expiring", refreshToken: "stale-refresh", expiresIn: -60)
        let authManager = makeAuthManager()
        var survivorSignals: [AuthManager.AuthStateSignal] = []
        var removedSignals: [AuthManager.AuthStateSignal] = []
        authManager.addAuthStateObserver { survivorSignals.append($0) }
        let removedToken = authManager.addAuthStateObserver { removedSignals.append($0) }
        authManager.removeAuthStateObserver(removedToken)
        AuthBackendURLProtocol.respond { _ in .json("{}", statusCode: 401) }

        try? await authManager.refreshIfNeeded()

        XCTAssertEqual(survivorSignals, [.ok, .refreshing, .authRequired])
        // The removed observer still received the immediate current-state (`.ok`)
        // delivered synchronously at registration, but nothing after removal.
        XCTAssertEqual(removedSignals, [.ok], "a removed observer must receive no signals after removal")
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

    func testEstablishSessionSurfacesAuthWallBeforeStatusHandling() async throws {
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json("<html><body>Sign in</body></html>", statusCode: 403)
        }

        do {
            try await authManager.establishSession(apiToken: "valid-token")
            XCTFail("Expected session bridge markup to surface as an authentication wall")
        } catch AuthError.authWall {}
    }

    func testBootstrapSessionClearsLocalStateWhenRefreshIsRejected() async {
        seedStoredAuth(apiToken: "expired-api-token", refreshToken: "stored-refresh-token", expiresIn: -60)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/auth/refresh")
            return .json("{}", statusCode: 401)
        }

        await authManager.bootstrapSession()

        XCTAssertTrue(
            authManager.isAuthenticated,
            "shell must stay mounted so in-place sign-in can recover the session"
        )
        XCTAssertFalse(authManager.isBootstrapping)
        XCTAssertNil(KeychainHelper.readString(key: "fa_api_token"))
        XCTAssertNil(KeychainHelper.readString(key: "fa_refresh_token"))
        XCTAssertNil(UserDefaults.standard.string(forKey: "fa_token_expiry"))
        XCTAssertTrue(
            authManager.authRequired,
            "authRequired must be latched so in-place sign-in is presented"
        )
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
        XCTAssertTrue(AuthError.authWall.errorDescription?.contains("authentication wall detected") == true)
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

    func testRefreshThresholdIsProportionalToTokenLifetime() {
        XCTAssertEqual(AuthManager.refreshThreshold(tokenTTL: nil), 3600)
        XCTAssertEqual(AuthManager.refreshThreshold(tokenTTL: 0), 3600)
        XCTAssertEqual(AuthManager.refreshThreshold(tokenTTL: 7200), 3600, "a long-lived token caps at the historical hour")
        XCTAssertEqual(AuthManager.refreshThreshold(tokenTTL: 1800), 900)

        XCTAssertTrue(AuthManager.shouldRefresh(remaining: 900, ttl: 1800))
        XCTAssertTrue(AuthManager.shouldRefresh(remaining: 899, ttl: 1800))
        XCTAssertFalse(AuthManager.shouldRefresh(remaining: 901, ttl: 1800))
    }

    func testShortLivedTokenRefreshesAtHalfLifetimeNotFixedHour() async throws {
        // A one-hour token with 45 minutes left is fresh under a proportional
        // half-lifetime threshold, though the old fixed check would have
        // refreshed it on nearly every call.
        seedStoredAuth(apiToken: "short-lived", refreshToken: "the-refresh", expiresIn: 2700)
        UserDefaults.standard.set(3600, forKey: "fa_token_lifetime")
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            XCTFail("A token past its proportional threshold must not be refreshed early")
            return .json("{}")
        }

        try await authManager.refreshIfNeeded()

        XCTAssertTrue(AuthBackendURLProtocol.requests.isEmpty)
    }

    func testShortLivedTokenPastHalfLifetimeStillRefreshes() async throws {
        // Half of the one-hour lifetime has elapsed: the token is due even
        // though it still has ~29 minutes left.
        seedStoredAuth(apiToken: "short-lived", refreshToken: "the-refresh", expiresIn: 1740)
        UserDefaults.standard.set(3600, forKey: "fa_token_lifetime")
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            .json(#"{"api_token":"rotated","refresh_token":"rotated-refresh","expires_in":3600}"#)
        }

        try await authManager.refreshIfNeeded()

        let refreshPosts = AuthBackendURLProtocol.requests.filter { $0.url?.path == "/api/auth/refresh" }
        XCTAssertEqual(refreshPosts.count, 1)
        XCTAssertEqual(KeychainHelper.readString(key: "fa_api_token"), "rotated")
    }

    func testMissingLifetimeFallsBackToHistoricalHourThreshold() async throws {
        // No stored lifetime (pre-proportional credentials): remaining time above
        // an hour must skip the refresh exactly as before.
        seedStoredAuth(apiToken: "legacy", refreshToken: "the-refresh", expiresIn: 5400)
        let authManager = makeAuthManager()
        AuthBackendURLProtocol.respond { _ in
            XCTFail("A legacy token over an hour from expiry must not be refreshed")
            return .json("{}")
        }

        try await authManager.refreshIfNeeded()

        XCTAssertTrue(AuthBackendURLProtocol.requests.isEmpty)
    }

    private func waitUntil(
        timeout: TimeInterval = 2,
        predicate: @escaping () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate() {
                return
            }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail("Timed out waiting for predicate")
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
        UserDefaults.standard.removeObject(forKey: "fa_token_lifetime")
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
    /// When set, `startLoading` blocks on this gate before producing a response,
    /// so a test can hold a request in flight (e.g. a forced refresh) while it
    /// exercises a concurrent caller, then release it deterministically.
    private static var responseGate: DispatchSemaphore?

    static var requests: [URLRequest] {
        lock.withLock { recordedRequests }
    }

    static func respond(with handler: @escaping Handler) {
        lock.withLock {
            self.handler = handler
        }
    }

    static func installResponseGate() -> DispatchSemaphore {
        let gate = DispatchSemaphore(value: 0)
        lock.withLock { responseGate = gate }
        return gate
    }

    static func reset() {
        lock.withLock {
            handler = nil
            recordedRequests = []
            responseGate = nil
        }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "assistant.example.test"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        let (handler, gate): (Handler?, DispatchSemaphore?) = Self.lock.withLock {
            Self.recordedRequests.append(request)
            return (Self.handler, Self.responseGate)
        }

        gate?.wait()

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
