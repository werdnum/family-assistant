import AuthenticationServices
import CryptoKit
import Foundation
import os
import WebKit

@Observable
final class AuthManager {
    var serverURL: String = ""
    var isAuthenticated = false
    /// True between app launch and the first completion of `bootstrapSession()`
    /// when stored credentials exist. Consumers should render a loading state
    /// (not the web view) while this is true so the WKWebView's first request
    /// happens after `establishSession` has bridged the session cookie.
    var isBootstrapping = false
    var isLoading = false
    var errorMessage: String?

    /// Wall-clock budget for ``bootstrapSession()``. If the refresh + session-bridge
    /// work (notably the `WKWebsiteDataStore` cookie hand-off, whose first access
    /// can stall on WebKit-process spin-up) exceeds this, the watchdog abandons it
    /// and lets the app proceed with the stored token rather than pinning the
    /// "Signing in…" screen indefinitely. `var` so tests can shorten it.
    var bootstrapWatchdogSeconds: Double = 15

    /// Per-request timeout for the bootstrap auth calls, kept below the watchdog so
    /// a dead network surfaces as a transient failure before the watchdog trips.
    private let authRequestTimeoutSeconds: TimeInterval = 10

    /// Monotonic counter bumped on every auth-invalidating transition (logout,
    /// credential clear, new login). A session bridge captures it on entry and
    /// only writes its cookie if it is still current — so a bridge abandoned by
    /// the ``bootstrapSession()`` watchdog cannot resurrect a stale session cookie
    /// after a later logout/re-login has superseded it. `private(set)` so tests can
    /// observe it; only `bumpAuthEpoch()` mutates it.
    @MainActor private(set) var authEpoch = 0

    /// Terminal auth state: the server has rejected the stored credentials (or they
    /// are missing where required) and the user must re-authenticate. Distinct from
    /// a transient refresh failure, which keeps the existing credentials. Consumers
    /// surface this as a dedicated re-auth affordance — it must NEVER be routed
    /// through the generic error modal. Cleared on any successful (re)auth/login.
    @MainActor private(set) var authRequired = false

    /// A coarse auth-state transition observers (the sync coordinator) can react to
    /// without depending on `AuthManager`'s internals. Fires on `refreshIfNeeded`
    /// entering its network refresh (`.refreshing`), on that refresh (or a login)
    /// succeeding (`.ok`), and on a rejection latching `authRequired` (`.authRequired`).
    enum AuthStateSignal {
        case ok
        case refreshing
        case authRequired
    }

    /// Live auth-state observers, keyed by an opaque token returned at
    /// registration. Every observer receives every transition — SwiftUI can
    /// construct a fresh `ChatViewModel` (and its coordinator) while `@State`
    /// retains the original, so a single rebindable callback would leave the
    /// on-screen model's coordinator without auth signals. Each view model
    /// registers its own entry and removes it on `deinit`.
    @ObservationIgnored @MainActor private var authStateObservers:
        [UUID: (AuthStateSignal) -> Void] = [:]

    /// Register `observer` for auth-state transitions. Returns a token the caller
    /// passes to ``removeAuthStateObserver(_:)`` on teardown so a discarded view
    /// model's closure does not linger and drive a dead coordinator.
    @MainActor
    @discardableResult
    func addAuthStateObserver(_ observer: @escaping (AuthStateSignal) -> Void) -> UUID {
        let token = UUID()
        authStateObservers[token] = observer
        // Deliver the current stable state immediately: an observer registered
        // after `authRequired` latched (a fresh ChatViewModel whose coordinator
        // is built post-rejection) would otherwise stay at its default `.ok` and
        // never learn re-auth is required until the next transition.
        observer(authRequired ? .authRequired : .ok)
        return token
    }

    @MainActor
    func removeAuthStateObserver(_ token: UUID) {
        authStateObservers.removeValue(forKey: token)
    }

    @MainActor
    private func emitAuthStateSignal(_ signal: AuthStateSignal) {
        for observer in authStateObservers.values {
            observer(signal)
        }
    }

    /// Coalesces concurrent ``refreshIfNeeded(ownerEpoch:)`` callers onto one
    /// in-flight refresh so a resume that fans out several requests cannot race
    /// token rotation. The task clears this on completion; every caller awaits the
    /// same task rather than blocking the main actor.
    @ObservationIgnored @MainActor private var inFlightRefresh: Task<Void, Error>?

    private var codeVerifier: String?
    private var authSession: ASWebAuthenticationSession?
    private var contextProvider: PresentationContextProvider?
    private let logger = Logger(subsystem: "com.familyassistant.app", category: "auth")

    private enum Keys {
        static let serverURL = "fa_server_url"
        static let apiToken = "fa_api_token"
        static let refreshToken = "fa_refresh_token"
        static let tokenExpiry = "fa_token_expiry"
    }

    init() {
        serverURL = UserDefaults.standard.string(forKey: Keys.serverURL) ?? ""
        if KeychainHelper.readString(key: Keys.apiToken) != nil {
            isAuthenticated = true
            isBootstrapping = true
        }
    }

    // MARK: - PKCE

    private func generateCodeVerifier() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64URLEncoded
    }

    private func codeChallenge(for verifier: String) -> String {
        let data = Data(verifier.utf8)
        let hash = SHA256.hash(data: data)
        return Data(hash).base64URLEncoded
    }

    // MARK: - Login

    @MainActor
    func login() {
        guard let baseURL = validatedServerURL() else {
            errorMessage = "Please enter a valid server URL"
            return
        }

        isLoading = true
        errorMessage = nil

        let verifier = generateCodeVerifier()
        codeVerifier = verifier
        let challenge = codeChallenge(for: verifier)

        var components = URLComponents(url: baseURL.appendingPathComponent("app-auth"), resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "code_challenge", value: challenge),
            URLQueryItem(name: "code_challenge_method", value: "S256"),
        ]

        guard let authURL = components.url else {
            errorMessage = "Failed to construct auth URL"
            isLoading = false
            return
        }

        let callbackScheme = "familyassistant"

        // Cancel any existing session before creating a new one
        authSession?.cancel()
        authSession = nil

        authSession = ASWebAuthenticationSession(url: authURL, callbackURLScheme: callbackScheme) { [weak self] callbackURL, error in
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    if (error as NSError).code == ASWebAuthenticationSessionError.canceledLogin.rawValue {
                        self.isLoading = false
                        return
                    }
                    self.errorMessage = error.localizedDescription
                    ErrorReporter.shared.report(error, component: "Auth.login")
                    self.isLoading = false
                    return
                }
                if let callbackURL {
                    await self.handleCallback(url: callbackURL)
                }
            }
        }

        authSession?.prefersEphemeralWebBrowserSession = false
        // Must retain the context provider — ASWebAuthenticationSession holds a weak reference
        contextProvider = PresentationContextProvider()
        authSession?.presentationContextProvider = contextProvider

        if authSession?.start() != true {
            logger.error("ASWebAuthenticationSession failed to start")
            errorMessage = "Failed to start authentication"
            isLoading = false
        }
    }

    // MARK: - Callback Handling

    @MainActor
    func handleCallback(url: URL) async {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let code = components.queryItems?.first(where: { $0.name == "code" })?.value,
              let verifier = codeVerifier
        else {
            errorMessage = "Invalid callback URL"
            isLoading = false
            return
        }

        codeVerifier = nil

        do {
            let tokens = try await exchangeCode(code: code, codeVerifier: verifier)
            saveTokens(tokens)
            // Supersede any abandoned bootstrap bridge so it cannot clobber this
            // fresh login's cookie; this login's own bridge captures the new epoch.
            bumpAuthEpoch()
            try await establishSession(apiToken: tokens.apiToken)
            isAuthenticated = true
            setAuthRequired(false)
        } catch {
            errorMessage = "Authentication failed: \(error.localizedDescription)"
            ErrorReporter.shared.report(error, component: "Auth.callback")
        }

        isLoading = false
    }

    // MARK: - Token Exchange

    private func exchangeCode(code: String, codeVerifier: String) async throws -> TokenResponse {
        guard let baseURL = validatedServerURL() else {
            throw AuthError.invalidServerURL
        }

        let url = baseURL.appendingPathComponent("api/auth/exchange")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode([
            "code": code,
            "code_verifier": codeVerifier,
        ])

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw AuthError.exchangeFailed
        }

        return try JSONDecoder().decode(TokenResponse.self, from: data)
    }

    // MARK: - Session Bootstrap

    @MainActor
    func bootstrapSession() async {
        defer { isBootstrapping = false }

        guard KeychainHelper.readString(key: Keys.apiToken) != nil else {
            clearLocalAuthState()
            return
        }

        let completed = await runWithWatchdog(seconds: bootstrapWatchdogSeconds) { [weak self] in
            await self?.performBootstrap()
        }

        if !completed {
            logger.error(
                "Sign-in bootstrap exceeded \(self.bootstrapWatchdogSeconds, privacy: .public)s watchdog; proceeding with stored credentials"
            )
            ErrorReporter.shared.report(
                message: "Sign-in bootstrap exceeded "
                    + "\(Int(bootstrapWatchdogSeconds))s watchdog; proceeded with stored credentials",
                component: "Auth.bootstrap",
                errorType: .component,
                extraData: ["watchdog_seconds": String(Int(bootstrapWatchdogSeconds))]
            )
        }
    }

    /// The refresh + session-bridge work run under ``bootstrapSession()``'s watchdog.
    /// Handles its own errors so the watchdog only needs to observe completion.
    @MainActor
    private func performBootstrap() async {
        // Ownership token: if the watchdog abandons this task and a logout/re-login
        // bumps the epoch, a late resume must not mutate the now-current auth state.
        let epoch = authEpoch

        guard let token = KeychainHelper.readString(key: Keys.apiToken) else {
            clearLocalAuthState()
            return
        }

        do {
            try await refreshIfNeeded(ownerEpoch: epoch)
        } catch AuthError.authRejected, AuthError.noCredentials {
            if isCurrentAuthEpoch(epoch) { clearLocalAuthState() }
            return
        } catch is CancellationError {
            return
        } catch {
            logger.warning(
                "Token refresh failed transiently; will attempt session bridge with existing token: \(error.localizedDescription, privacy: .public)"
            )
        }

        // Superseded while refreshing? Stop before the bridge mutates auth state.
        guard isCurrentAuthEpoch(epoch) else { return }

        let activeToken = KeychainHelper.readString(key: Keys.apiToken) ?? token

        do {
            try await establishSession(apiToken: activeToken)
        } catch AuthError.authRejected {
            if isCurrentAuthEpoch(epoch) { clearLocalAuthState() }
        } catch is CancellationError {
            return
        } catch {
            logger.warning(
                "Session bridge failed transiently; keeping local auth state: \(error.localizedDescription, privacy: .public)"
            )
        }
    }

    /// Runs `operation`, returning `true` when it finishes, or `false` if it does
    /// not complete within `seconds`. On timeout the operation is cancelled and
    /// abandoned (not awaited), so a non-cancellable stall (e.g. a stuck
    /// `WKWebsiteDataStore`) cannot pin the caller. Both children run on the main
    /// actor, matching the isolation of `operation` and the auth state it mutates.
    @MainActor
    func runWithWatchdog(
        seconds: Double,
        operation: @escaping @MainActor () async -> Void
    ) async -> Bool {
        let race = WatchdogRaceState()
        return await withCheckedContinuation { continuation in
            let work = Task { @MainActor in
                await operation()
                if race.tryFinish() { continuation.resume(returning: true) }
            }
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                if race.tryFinish() {
                    work.cancel()
                    continuation.resume(returning: false)
                }
            }
        }
    }

    @MainActor
    private func clearLocalAuthState() {
        bumpAuthEpoch()
        KeychainHelper.delete(key: Keys.apiToken)
        KeychainHelper.delete(key: Keys.refreshToken)
        UserDefaults.standard.removeObject(forKey: Keys.tokenExpiry)
        isAuthenticated = false
    }

    /// Invalidate any in-flight (possibly watchdog-abandoned) session bridge so it
    /// cannot mutate auth state after this transition. See ``authEpoch``.
    @MainActor
    private func bumpAuthEpoch() {
        authEpoch += 1
    }

    /// Mutate ``authRequired`` and, on an actual change, emit the matching
    /// transition signal (`.authRequired` / `.ok`) so the sync coordinator's
    /// presentation follows. Clearing it also emits `.ok`, since a successful
    /// (re)auth is exactly the transition observers surface as "connected again".
    @MainActor
    private func setAuthRequired(_ required: Bool) {
        guard authRequired != required else { return }
        authRequired = required
        emitAuthStateSignal(required ? .authRequired : .ok)
    }

    /// Latch the terminal ``authRequired`` state from a caller that observed a
    /// response-time rejection the refresh path itself did not surface — namely
    /// the ChatAPIClient retry helper, when a request still 401s after a forced
    /// refresh minted a fresh token. Idempotent; emits the transition once.
    @MainActor
    func markAuthRequired() {
        setAuthRequired(true)
    }

    /// Clear the stored credentials and latch authRequired for in-place re-auth,
    /// but only if `capturedEpoch` still owns the current auth state. The response-time
    /// 401 on stream connect or request paths reach here to clear the rejected token
    /// while preserving the authenticated shell (isAuthenticated stays true) so the
    /// in-place re-auth affordance stays mounted and can recover without a full logout.
    /// Honoring the epoch fence: a logout/re-login that bumped the epoch during the
    /// refresh must not have a stale rejection clear the newer login's state.
    @MainActor
    func clearAuthStateForReauthIfCurrent(capturedEpoch: Int) {
        guard isCurrentAuthEpoch(capturedEpoch) else { return }
        bumpAuthEpoch()
        setAuthRequired(true)
        clearAuthCredentialsOnly()
    }

    /// Clear only the stored credentials (keychain/defaults) without flipping
    /// isAuthenticated. Used for in-place re-auth where the shell stays mounted.
    @MainActor
    private func clearAuthCredentialsOnly() {
        KeychainHelper.delete(key: Keys.apiToken)
        KeychainHelper.delete(key: Keys.refreshToken)
        UserDefaults.standard.removeObject(forKey: Keys.tokenExpiry)
    }

    /// Latch terminal auth state, clear credentials, and bump epoch with fencing.
    /// Used when a request is rejected both initially AND after a forced refresh
    /// (terminal auth failure), and by tests simulating concurrent auth-state
    /// changes (logout/relogin while a refresh is in flight). Only clears if the
    /// captured epoch is still current; a stale call (from a superseded operation)
    /// is dropped so it doesn't undo a fresh login/re-auth. Bumps the epoch to
    /// invalidate any in-flight operations using the old epoch.
    /// Preserves isAuthenticated=true (shell stays mounted for in-place re-auth).
    @MainActor
    func clearAuthStateIfCurrent(capturedEpoch: Int) {
        guard isCurrentAuthEpoch(capturedEpoch) else { return }
        bumpAuthEpoch()
        setAuthRequired(true)
        clearAuthCredentialsOnly()
    }

    /// Whether the bridge that captured `epoch` still owns the current auth state.
    /// False once an auth-invalidating transition (logout / credential clear / new
    /// login) has bumped ``authEpoch``. A watchdog-abandoned bridge that resumes
    /// after such a transition must not mutate auth state (cookies or keychain).
    @MainActor
    func isCurrentAuthEpoch(_ epoch: Int) -> Bool {
        epoch == authEpoch
    }

    /// Of the cookies a bridge `written`, the subset still present in `live` with
    /// the exact value we wrote — i.e. ours, not overwritten by a newer login. Used
    /// to undo a stalled-then-superseded bridge's writes without ever deleting a
    /// fresh login's same-named cookie (which carries a different value).
    static func staleCookiesToDelete(
        written: [HTTPCookie],
        live: [HTTPCookie]
    ) -> [HTTPCookie] {
        written.filter { ours in
            live.contains {
                $0.name == ours.name
                    && $0.domain == ours.domain
                    && $0.path == ours.path
                    && $0.value == ours.value
            }
        }
    }

    // MARK: - Token Refresh

    /// - Parameter ownerEpoch: when set (the bootstrap path), rotated tokens are
    ///   persisted only if this epoch is still current. A watchdog-abandoned
    ///   bootstrap whose refresh returns late after a logout/re-login must not
    ///   overwrite the current session's credentials. The lazy `authorizedRequest`
    ///   path passes `nil` and always persists.
    ///
    /// Concurrent callers are single-flighted: the first caller that finds the
    /// token due for refresh performs the network refresh and every other caller
    /// awaits that same in-flight `Task` rather than issuing its own refresh POST,
    /// so a resume that fans out several requests cannot race token rotation. Each
    /// caller awaits the shared task (never blocks the main actor).
    ///
    /// - Parameter force: skip the clock-based freshness check and always refresh
    ///   (still single-flighted). The response-time-401 retry path uses this: the
    ///   server rejected a token the client still believes is unexpired, so a
    ///   freshness check would no-op and the retry would resend the same rejected
    ///   token.
    @MainActor
    func refreshIfNeeded(ownerEpoch: Int? = nil, force: Bool = false) async throws {
        // Capture the current epoch before awaiting any in-flight refresh, so we
        // can detect if a concurrent auth-state change happened and reject adopting
        // a stale refresh result.
        let currentEpoch = authEpoch

        // Await any in-flight refresh BEFORE the freshness short-circuit. A forced
        // refresh (response-time 401 path) can be running while the stored expiry
        // still looks fresh; a non-forced caller that returned at the freshness
        // check here would send the token the server just rejected. Awaiting the
        // in-flight refresh first ensures such callers observe the rotated token.
        if let inFlightRefresh {
            try await inFlightRefresh.value
            // If the epoch changed while we were awaiting the in-flight refresh,
            // the refresh's result is stale and we must not return success. Either
            // the previous owner was superseded or we were, and either way adopting
            // the stale result would corrupt the auth state.
            if authEpoch != currentEpoch {
                throw AuthError.noCredentials
            }
            return
        }

        if !force {
            guard let expiryString = UserDefaults.standard.string(forKey: Keys.tokenExpiry),
                  let expiry = ISO8601DateFormatter().date(from: expiryString)
            else {
                throw AuthError.noCredentials
            }

            if expiry.timeIntervalSinceNow > 3600 {
                return
            }
        }

        let refresh = Task { @MainActor [weak self] in
            defer { self?.inFlightRefresh = nil }
            try await self?.performRefresh(ownerEpoch: ownerEpoch)
        }
        inFlightRefresh = refresh
        try await refresh.value
    }

    /// The network refresh coalesced by ``refreshIfNeeded(ownerEpoch:)``. Emits the
    /// `.refreshing` transition on entry, latches ``authRequired`` on a rejection,
    /// and clears it on success.
    @MainActor
    private func performRefresh(ownerEpoch: Int?) async throws {
        guard let refreshToken = KeychainHelper.readString(key: Keys.refreshToken),
              let baseURL = validatedServerURL()
        else {
            // No refresh token to present (or no server URL): re-auth is required.
            // Latch it here so EVERY no-credential refresh attempt is terminal,
            // including the forced response-time-401 retry path that reaches
            // `performRefresh` directly rather than through `authorizedRequest`.
            setAuthRequired(true)
            throw AuthError.noCredentials
        }

        emitAuthStateSignal(.refreshing)

        let url = baseURL.appendingPathComponent("api/auth/refresh")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = authRequestTimeoutSeconds
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(["refresh_token": refreshToken])

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw AuthError.transient(underlying: error)
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw AuthError.transient(underlying: nil)
        }

        switch httpResponse.statusCode {
        case 200:
            do {
                let tokenResponse = try JSONDecoder().decode(TokenResponse.self, from: data)
                if let ownerEpoch, !isCurrentAuthEpoch(ownerEpoch) {
                    // Superseded while refreshing; drop the rotation rather than
                    // clobber the current session's credentials.
                    return
                }
                saveTokens(tokenResponse)
                setAuthRequired(false)
            } catch {
                throw AuthError.transient(underlying: error)
            }
        case 401, 403:
            // Only latch authRequired if this refresh still owns the current auth state.
            // A stale rejection from a superseded epoch (logout/re-login happened while
            // the refresh was in flight) must not mark a freshly re-authenticated session
            // as needing sign-in, and must not clear the new credentials.
            if let ownerEpoch, !isCurrentAuthEpoch(ownerEpoch) {
                throw AuthError.authRejected
            }
            setAuthRequired(true)
            throw AuthError.authRejected
        default:
            throw AuthError.transient(underlying: nil)
        }
    }

    // MARK: - Session Establishment

    func establishSession(apiToken: String) async throws {
        guard let baseURL = validatedServerURL() else {
            throw AuthError.invalidServerURL
        }

        let capturedEpoch = await authEpoch

        let url = baseURL.appendingPathComponent("api/auth/token-session")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = authRequestTimeoutSeconds
        request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")

        let response: URLResponse
        do {
            (_, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw AuthError.transient(underlying: error)
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw AuthError.transient(underlying: nil)
        }

        switch httpResponse.statusCode {
        case 200:
            break
        case 401, 403:
            throw AuthError.authRejected
        default:
            throw AuthError.transient(underlying: nil)
        }

        // Fence: if an auth-invalidating transition (logout / re-login) happened
        // while this bridge was in flight — e.g. it was abandoned by the bootstrap
        // watchdog and only now resumed — do not write the now-stale session cookie.
        guard await isCurrentAuthEpoch(capturedEpoch) else {
            return
        }

        if let headerFields = httpResponse.allHeaderFields as? [String: String],
           let responseURL = httpResponse.url
        {
            let cookies = HTTPCookie.cookies(withResponseHeaderFields: headerFields, for: responseURL)
            let cookieStore = await WKWebsiteDataStore.default().httpCookieStore
            for cookie in cookies {
                await cookieStore.setCookie(cookie)
            }

            // The setCookie awaits above are themselves where the non-cancellable
            // WebKit stall can occur. If auth was invalidated while we were
            // suspended there, undo our writes — but only the cookies whose live
            // value is still exactly what we wrote, so a fresh login's same-named
            // cookie (a different value) is never collateral. Failing safe to a
            // re-bridge beats persisting a stale/cross-session cookie.
            if await !isCurrentAuthEpoch(capturedEpoch) {
                let stale = Self.staleCookiesToDelete(
                    written: cookies,
                    live: await cookieStore.allCookies()
                )
                for cookie in stale {
                    await cookieStore.deleteCookie(cookie)
                }
            }
        }
    }

    // MARK: - Logout

    @MainActor
    func logout() async {
        // Supersede any in-flight session bridge before clearing WebKit data, so a
        // watchdog-abandoned bootstrap cannot re-add the cookie after this cleanup.
        bumpAuthEpoch()

        // Revoke token server-side (best effort)
        if let apiToken = KeychainHelper.readString(key: Keys.apiToken),
           let baseURL = validatedServerURL()
        {
            let url = baseURL.appendingPathComponent("api/auth/logout")
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")
            _ = try? await URLSession.shared.data(for: request)
        }

        // Clear local state
        KeychainHelper.delete(key: Keys.apiToken)
        KeychainHelper.delete(key: Keys.refreshToken)
        UserDefaults.standard.removeObject(forKey: Keys.tokenExpiry)

        // Clear WKWebView data before flipping the auth state, so a fast
        // re-login's fresh session cookie cannot be wiped by this cleanup.
        let dataStore = WKWebsiteDataStore.default()
        let types = WKWebsiteDataStore.allWebsiteDataTypes()
        let records = await dataStore.dataRecords(ofTypes: types)
        await dataStore.removeData(ofTypes: types, for: records)

        isAuthenticated = false
        setAuthRequired(false)
    }

    // MARK: - Helpers

    func validatedServerURL() -> URL? {
        var urlString = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if !urlString.hasPrefix("http://") && !urlString.hasPrefix("https://") {
            urlString = "https://\(urlString)"
        }
        if urlString.hasSuffix("/") {
            urlString = String(urlString.dropLast())
        }
        return URL(string: urlString)
    }

    func saveServerURL() {
        UserDefaults.standard.set(serverURL, forKey: Keys.serverURL)
    }

    @MainActor
    func authorizedRequest(url: URL, method: String) async throws -> URLRequest {
        let capturedEpoch = authEpoch
        do {
            try await refreshIfNeeded()
        } catch AuthError.authRejected, AuthError.noCredentials {
            // Only clear state if the epoch hasn't changed since we started. If
            // logout/relogin bumped the epoch while we were in an in-flight refresh,
            // don't clear the new session's credentials; let the error propagate so
            // the caller can retry with the current credentials.
            if isCurrentAuthEpoch(capturedEpoch) {
                setAuthRequired(true)
                clearLocalAuthState()
            }
            throw AuthError.noCredentials
        }

        guard let apiToken = KeychainHelper.readString(key: Keys.apiToken) else {
            setAuthRequired(true)
            clearLocalAuthState()
            throw AuthError.noCredentials
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")
        return request
    }

    private func saveTokens(_ tokens: TokenResponse) {
        KeychainHelper.save(key: Keys.apiToken, string: tokens.apiToken)
        if let refresh = tokens.refreshToken {
            KeychainHelper.save(key: Keys.refreshToken, string: refresh)
        }
        if let expiresIn = tokens.expiresIn {
            let expiry = Date().addingTimeInterval(TimeInterval(expiresIn))
            UserDefaults.standard.set(ISO8601DateFormatter().string(from: expiry), forKey: Keys.tokenExpiry)
        }
    }
}

// MARK: - Supporting Types

struct TokenResponse: Decodable {
    let apiToken: String
    let refreshToken: String?
    let expiresIn: Int?

    enum CodingKeys: String, CodingKey {
        case apiToken = "api_token"
        case refreshToken = "refresh_token"
        case expiresIn = "expires_in"
    }
}

/// One-shot, thread-safe "who finished first" flag shared by the two racers in
/// ``AuthManager/runWithWatchdog(seconds:operation:)`` so the continuation is
/// resumed exactly once.
private final class WatchdogRaceState: @unchecked Sendable {
    private let lock = NSLock()
    private var finished = false

    func tryFinish() -> Bool {
        lock.withLock {
            if finished { return false }
            finished = true
            return true
        }
    }
}

enum AuthError: LocalizedError {
    case invalidServerURL
    case exchangeFailed
    case authRejected
    case noCredentials
    case transient(underlying: Error?)

    var errorDescription: String? {
        switch self {
        case .invalidServerURL: "Invalid server URL"
        case .exchangeFailed: "Failed to exchange authorization code"
        case .authRejected: "Server rejected stored credentials"
        case .noCredentials: "No stored credentials"
        case .transient(let underlying):
            if let underlying { "Temporary failure: \(underlying.localizedDescription)" }
            else { "Temporary network or server error" }
        }
    }
}

// MARK: - ASWebAuthenticationSession Presentation

private final class PresentationContextProvider: NSObject, ASWebAuthenticationPresentationContextProviding {
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        let windowScenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let allWindows = windowScenes.flatMap { $0.windows }
        return allWindows.first { $0.isKeyWindow } ?? allWindows.first ?? UIWindow()
    }
}

// MARK: - Base64URL Encoding

extension Data {
    var base64URLEncoded: String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
