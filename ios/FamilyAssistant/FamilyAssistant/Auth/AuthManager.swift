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
            try await refreshIfNeeded()
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

    @MainActor
    func refreshIfNeeded() async throws {
        guard let expiryString = UserDefaults.standard.string(forKey: Keys.tokenExpiry),
              let expiry = ISO8601DateFormatter().date(from: expiryString)
        else {
            throw AuthError.noCredentials
        }

        if expiry.timeIntervalSinceNow > 3600 {
            return
        }

        guard let refreshToken = KeychainHelper.readString(key: Keys.refreshToken),
              let baseURL = validatedServerURL()
        else {
            throw AuthError.noCredentials
        }

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
                saveTokens(tokenResponse)
            } catch {
                throw AuthError.transient(underlying: error)
            }
        case 401, 403:
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
        do {
            try await refreshIfNeeded()
        } catch AuthError.authRejected, AuthError.noCredentials {
            clearLocalAuthState()
            throw AuthError.noCredentials
        }

        guard let apiToken = KeychainHelper.readString(key: Keys.apiToken) else {
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
