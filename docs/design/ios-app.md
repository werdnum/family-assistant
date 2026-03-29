# iOS App Design

## Overview

A native SwiftUI iOS app for Family Assistant. Phase 1 wraps the existing web UI in a WKWebView with
a native auth flow. Later phases add a native chat interface and CallKit voice integration.

## Phase 1: WKWebView Wrapper with Native Auth

### Auth Flow (Web-Assisted Login)

Instead of making the user copy-paste an API token, the app opens a special auth helper page in the
web UI that handles the OIDC flow and hands back a token via URL scheme:

1. User launches app for the first time, enters server URL (e.g., `https://fa.example.com`)
2. App opens `{serverURL}/app-auth` in an in-app browser (ASWebAuthenticationSession)
3. User completes normal OIDC login on that page
4. Backend generates an API token for the user automatically
5. Page redirects to `familyassistant://auth-callback?token=<token>`
6. `ASWebAuthenticationSession` catches this, app extracts the token
7. App stores server URL + token in Keychain
8. App calls `POST /api/auth/token-session` with the Bearer token to establish a session cookie
9. WKWebView loads the web UI with the session cookie — everything just works

**On subsequent launches**: App calls `/api/auth/token-session` with stored token to refresh the
session cookie, then loads the web view.

### Backend Changes Required

#### 1. App Auth Helper Page (`/app-auth`)

A simple endpoint that:

- Initiates the standard OIDC login flow
- After successful login, auto-generates an API token (name: "iOS App", no expiry)
- Renders a page that redirects to `familyassistant://auth-callback?token=<full_token>`

This can be a server-rendered page (no React needed) since it's a transient auth flow.

Implementation: New router `src/family_assistant/web/routers/app_auth.py` with:

- `GET /app-auth` — Redirects to OIDC provider (same as `/login` but with a
  `redirect_to=app-auth-callback` param in session)
- `GET /app-auth-callback` — After OIDC callback, generates token and renders redirect page

#### 2. Token Session Endpoint (`POST /api/auth/token-session`)

Exchanges a Bearer token for a session cookie:

```python
@router.post("/api/auth/token-session")
async def token_session(request: Request, user=Depends(get_current_user)):
    """Exchange a valid API Bearer token for a session cookie."""
    request.session["user"] = user
    return {"ok": True}
```

This lets the iOS app authenticate once with the token, then the WKWebView uses the session cookie
for all requests (avoiding the complexity of intercepting every WKWebView request to inject auth
headers).

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
- Properties: `serverURL`, `apiToken`, `isAuthenticated`, `isLoading`
- `login()`: Opens `ASWebAuthenticationSession` to `{serverURL}/app-auth`
- `handleCallback(url:)`: Extracts token from callback URL, stores in Keychain
- `establishSession()`: Calls `POST /api/auth/token-session` to get session cookie for WKWebView
- `logout()`: Clears Keychain, clears WKWebView cookies

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
- Uses shared `WKWebsiteDataStore` (session cookie set by `establishSession()` is available)
- Loads `{serverURL}/chat`
- `WKNavigationDelegate` for handling external links (open in Safari)
- Pull-to-refresh via `UIRefreshControl`
- Exposes `canGoBack`/`canGoForward` for toolbar

**WebViewToolbar.swift**:

- Bottom toolbar or navigation bar items
- Back, Forward, Reload buttons bound to WKWebView
- Settings/logout button

**Info.plist**:

- `CFBundleURLTypes` registering `familyassistant` URL scheme
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
