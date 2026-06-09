# iOS App Design

## Overview

A native SwiftUI iOS app for Family Assistant. Phase 1 wraps the existing web UI in a WKWebView with
a native auth flow. Later phases add a native chat interface and CallKit voice integration.

## Phase 1: WKWebView Wrapper with Native Auth

### Auth Flow (Web-Assisted Login)

Instead of making the user copy-paste an API token, the app opens a special auth helper page in the
web UI that handles the OIDC flow and hands back credentials securely via an authorization code
exchange (PKCE-style):

01. User launches app for the first time, enters server URL (e.g., `https://fa.example.com`)
02. App generates a random `code_verifier` and derives `code_challenge` (SHA-256)
03. App opens `{serverURL}/app-auth?code_challenge=<hash>&code_challenge_method=S256` in an in-app
    browser (`ASWebAuthenticationSession`)
04. User completes normal OIDC login on that page
05. Backend generates a short-lived authorization code (single-use, expires in 60 seconds), stores
    it with the code challenge
06. Page redirects to `https://{serverHost}/.well-known/app-auth-callback?code=<auth_code>` — this
    is a Universal Link claimed by the app (no secrets in URL — the code alone is useless without
    the verifier). Universal Links are verified via the server's
    `/.well-known/apple-app-site-association` file, preventing scheme hijacking by other apps
07. `ASWebAuthenticationSession` catches this, app extracts the code
08. App calls `POST /api/auth/exchange` with `{code, code_verifier}` server-side
09. Backend verifies `SHA256(code_verifier) == stored code_challenge`, then returns an API token +
    refresh token
10. App stores both tokens in Keychain
11. App calls `POST /api/auth/token-session` with the API token to establish a session cookie
12. WKWebView loads the web UI with the session cookie — everything just works

**Token lifecycle**:

- API tokens have a short expiry (e.g., 30 days)
- Refresh tokens have a longer expiry (e.g., 90 days) and can be used to obtain new API tokens
- On each launch, the app checks token expiry and refreshes if needed via `POST /api/auth/refresh`
- If the refresh token is also expired, the user is prompted to re-authenticate
- Tokens can be revoked server-side (e.g., via the web UI token management page)

### Backend Changes Required

#### 1. App Auth Helper Page (`/app-auth`)

A simple endpoint that:

- Initiates the standard OIDC login flow
- After successful login, generates a short-lived authorization code bound to the PKCE challenge
- Renders a page that redirects to
  `https://{serverHost}/.well-known/app-auth-callback?code=<auth_code>` (Universal Link caught by
  the app)

This can be a server-rendered page (no React needed) since it's a transient auth flow.

Implementation: New router `src/family_assistant/web/routers/app_auth.py` with:

- `GET /app-auth` — Accepts `code_challenge` and `code_challenge_method` params, stores them in
  session, redirects to OIDC provider
- `GET /app-auth-callback` — After OIDC callback, generates authorization code (single-use, 60s TTL)
  bound to the code challenge, renders redirect page
- `GET /.well-known/apple-app-site-association` — Serves the AASA file that claims the
  `/.well-known/app-auth-callback` path for the iOS app (enables Universal Links, prevents scheme
  hijacking). Requires the app's team ID and bundle ID.

#### 2. Code Exchange Endpoint (`POST /api/auth/exchange`)

Exchanges an authorization code + PKCE verifier for API and refresh tokens:

```python
@router.post("/api/auth/exchange")
async def exchange_code(request: ExchangeRequest, db=Depends(get_db)):
    """PKCE-style code exchange. Returns API token + refresh token."""
    # Verify: SHA256(code_verifier) == stored code_challenge
    # Generate API token (30-day expiry) and refresh token (90-day expiry)
    # Delete the authorization code (single-use)
    return {"api_token": token, "refresh_token": refresh, "expires_in": 2592000}
```

#### 3. Token Refresh Endpoint (`POST /api/auth/refresh`)

```python
@router.post("/api/auth/refresh")
async def refresh_token(request: RefreshRequest, db=Depends(get_db)):
    """Exchange a valid refresh token for a new API token."""
    # Validate refresh token, issue new API token
    return {"api_token": new_token, "expires_in": 2592000}
```

#### 4. Token Session Endpoint (`POST /api/auth/token-session`)

Exchanges a Bearer token for a session cookie:

```python
@router.post("/api/auth/token-session")
async def token_session(request: Request, user=Depends(get_current_user)):
    """Exchange a valid API Bearer token for a session cookie."""
    request.session["user"] = user
    # Store the token ID so session validity is tied to token validity
    request.session["api_token_id"] = user["token_id"]
    return {"ok": True}
```

The session cookie is tied to the API token that created it: the `AuthMiddleware` checks that the
referenced `api_token_id` is still valid (not revoked/expired) on each request. This ensures that
revoking the API token also invalidates the session. The iOS app calls this endpoint on each launch
(after refreshing the API token if needed), so session cookies are naturally short-lived.

This lets the iOS app authenticate once per launch with the token, then the WKWebView uses the
session cookie for all requests (avoiding the complexity of intercepting every WKWebView request to
inject auth headers).

### iOS Project Structure

```
ios/FamilyAssistant/
├── FamilyAssistant.xcodeproj/
│   └── project.pbxproj
├── FamilyAssistant/
│   ├── FamilyAssistantApp.swift      # @main entry, URL scheme handling
│   ├── ContentView.swift              # Root: shows SetupView or WebViewContainer
│   ├── Auth/
│   │   ├── KeychainHelper.swift       # Keychain read/write/delete (Security framework)
│   │   ├── AuthManager.swift          # ObservableObject: manages login state + token
│   │   └── SetupView.swift            # Server URL entry + "Sign In" button
│   ├── WebView/
│   │   ├── WebViewContainer.swift     # UIViewRepresentable wrapping WKWebView
│   │   └── WebViewToolbar.swift       # Back/forward/reload/settings toolbar
│   ├── Assets.xcassets/
│   │   ├── Contents.json
│   │   ├── AppIcon.appiconset/
│   │   │   └── Contents.json
│   │   └── AccentColor.colorset/
│   │       └── Contents.json
│   └── Info.plist
└── README.md                          # Build instructions
```

### Key Implementation Details

**FamilyAssistantApp.swift**:

- `@main` struct with `WindowGroup`
- Registers `familyassistant://` URL scheme via `onOpenURL`
- Creates and injects `AuthManager` as environment object

**AuthManager.swift**:

- `@Observable` class (iOS 17+) or `ObservableObject` (iOS 16+)
- Properties: `serverURL`, `apiToken`, `refreshToken`, `isAuthenticated`, `isLoading`
- `login()`: Generates PKCE code verifier/challenge, opens `ASWebAuthenticationSession` to
  `{serverURL}/app-auth?code_challenge=...&code_challenge_method=S256`
- `handleCallback(url:)`: Extracts auth code from callback URL, calls `POST /api/auth/exchange` with
  code + code verifier, stores API token + refresh token in Keychain
- `refreshIfNeeded()`: Checks token expiry, calls `POST /api/auth/refresh` if needed
- `establishSession()`: Calls `POST /api/auth/token-session` via `URLSession`, extracts the
  `Set-Cookie` from the response, then explicitly writes it into
  `WKWebsiteDataStore.default().httpCookieStore` so the WKWebView shares the session
- `logout()`: Clears Keychain, clears WKWebView cookies, revokes tokens server-side

**KeychainHelper.swift**:

- Simple wrapper using `Security` framework directly (no external deps)
- `save(key:data:)`, `read(key:)`, `delete(key:)` using `kSecClassGenericPassword`
- ~40 lines of code

**SetupView.swift**:

- TextField for server URL
- "Sign In" button that triggers `authManager.login()`
- Loading state while auth completes
- Error display if auth fails

**WebViewContainer.swift**:

- `UIViewRepresentable` wrapping `WKWebView`
- Uses `WKWebsiteDataStore.default()` — session cookie is explicitly written there by
  `AuthManager.establishSession()` before the web view loads (note: `URLSession` and `WKWebView`
  have separate cookie jars, so the cookie must be bridged explicitly via
  `httpCookieStore.setCookie`)
- Loads `{serverURL}/chat`
- `WKNavigationDelegate` for handling external links (open in Safari)
- Pull-to-refresh via `UIRefreshControl`
- Exposes `canGoBack`/`canGoForward` for toolbar

**WebViewToolbar.swift**:

- Bottom toolbar or navigation bar items
- Back, Forward, Reload buttons bound to WKWebView
- Settings/logout button

**Info.plist**:

- Associated Domains entitlement for Universal Links (`applinks:{serverHost}`)
- `NSAppTransportSecurity` if needed for local dev (HTTP)

### Dependencies

None. The entire app uses only Apple frameworks:

- `SwiftUI` — UI
- `WebKit` — WKWebView
- `Security` — Keychain
- `AuthenticationServices` — ASWebAuthenticationSession

### Build Requirements

- Xcode (free from Mac App Store)
- Any Apple ID for signing (free personal team works for sideloading)
- Change bundle identifier to something unique
- Build & Run to device

## Phase 2: Native Chat Interface (Future)

Replace WKWebView for the chat screen with a native SwiftUI view:

- `ChatView.swift` with message list and input bar
- `ChatService.swift` consuming `/api/v1/chat/send_message_stream` SSE via `URLSession`
- Native message bubbles, markdown rendering, attachment previews
- Tool call display with confirmation UI
- Conversation list/sidebar
- Keep WKWebView for non-chat pages (documents, notes, settings, etc.)

The API surface is well-suited for this — SSE streaming works naturally with `URLSession`'s async
bytes API.

## Phase 3: CallKit + Voice Mode (Future)

- Use `/api/gemini/ephemeral_token` to get Gemini Live API credentials
- Stream audio to/from Gemini Live API using `AVAudioEngine`
- `CXProvider` + `CXCallController` for CallKit integration
- Incoming call UI when assistant needs to notify user
- Background audio session for continued conversation

## Phase 4: Push Notifications (Future)

- APNs integration (requires paid Apple Developer account, $99/year)
- Backend addition: APNs provider alongside existing VAPID web push
- Store device tokens via new `/api/push/apns/register` endpoint
- Rich notifications with conversation context

## Phase 5: App Intents / Siri / Shortcuts (Done)

In-app App Intents expose core actions (ask the assistant, quick capture, create a note, open a
chat) to Siri and the Shortcuts app, with an `AppShortcutsProvider` registering Siri phrases
automatically. No new targets or entitlements — the intents run in the app's process and reuse
`AuthManager` and the existing API clients. See [ios-app-intents.md](ios-app-intents.md) for the
full design.
