import AuthenticationServices
import CryptoKit
import Foundation
import os
import WebKit

@Observable
final class AuthManager {
    var serverURL: String = ""
    var isAuthenticated = false
    var isLoading = false
    var errorMessage: String?

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
            try await establishSession(apiToken: tokens.apiToken)
            isAuthenticated = true
        } catch {
            errorMessage = "Authentication failed: \(error.localizedDescription)"
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
        guard KeychainHelper.readString(key: Keys.apiToken) != nil else {
            isAuthenticated = false
            return
        }

        guard await refreshIfNeeded() else {
            await clearLocalAuthState()
            return
        }

        guard let token = KeychainHelper.readString(key: Keys.apiToken) else {
            await clearLocalAuthState()
            return
        }

        do {
            try await establishSession(apiToken: token)
        } catch {
            logger.error("Session bootstrap failed: \(error.localizedDescription, privacy: .public)")
            await clearLocalAuthState()
        }
    }

    @MainActor
    private func clearLocalAuthState() async {
        KeychainHelper.delete(key: Keys.apiToken)
        KeychainHelper.delete(key: Keys.refreshToken)
        UserDefaults.standard.removeObject(forKey: Keys.tokenExpiry)
        isAuthenticated = false
    }

    // MARK: - Token Refresh

    @MainActor
    func refreshIfNeeded() async -> Bool {
        guard let expiryString = UserDefaults.standard.string(forKey: Keys.tokenExpiry),
              let expiry = ISO8601DateFormatter().date(from: expiryString)
        else {
            return false
        }

        // Refresh if token expires within the next hour
        if expiry.timeIntervalSinceNow > 3600 {
            return true
        }

        guard let refreshToken = KeychainHelper.readString(key: Keys.refreshToken),
              let baseURL = validatedServerURL()
        else {
            return false
        }

        let url = baseURL.appendingPathComponent("api/auth/refresh")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONEncoder().encode(["refresh_token": refreshToken])

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                return false
            }
            let tokenResponse = try JSONDecoder().decode(TokenResponse.self, from: data)
            saveTokens(tokenResponse)
            return true
        } catch {
            return false
        }
    }

    // MARK: - Session Establishment

    func establishSession(apiToken: String) async throws {
        guard let baseURL = validatedServerURL() else {
            throw AuthError.invalidServerURL
        }

        let url = baseURL.appendingPathComponent("api/auth/token-session")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")

        let (_, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw AuthError.sessionFailed
        }

        // Bridge cookies from URLSession to WKWebView
        if let headerFields = httpResponse.allHeaderFields as? [String: String],
           let responseURL = httpResponse.url
        {
            let cookies = HTTPCookie.cookies(withResponseHeaderFields: headerFields, for: responseURL)
            let cookieStore = WKWebsiteDataStore.default().httpCookieStore
            for cookie in cookies {
                await cookieStore.setCookie(cookie)
            }
        }
    }

    // MARK: - Logout

    @MainActor
    func logout() async {
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

        // Clear WKWebView data
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

enum AuthError: LocalizedError {
    case invalidServerURL
    case exchangeFailed
    case sessionFailed

    var errorDescription: String? {
        switch self {
        case .invalidServerURL: "Invalid server URL"
        case .exchangeFailed: "Failed to exchange authorization code"
        case .sessionFailed: "Failed to establish session"
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
