# Family Assistant iOS App

A native iOS app that wraps the Family Assistant web UI in a WKWebView with secure PKCE-based
authentication.

## Requirements

- Xcode 15.4+ (free from Mac App Store)
- iOS 17.0+ device or simulator
- Any Apple ID for signing (free personal team works for sideloading)

## Setup

1. Open `FamilyAssistant.xcodeproj` in Xcode
2. Select your development team under **Signing & Capabilities**
3. Change the bundle identifier to something unique (e.g., `com.yourname.familyassistant`)
4. Build & Run on your device or simulator

## How It Works

### Authentication Flow

The app uses a PKCE-based auth flow to securely obtain API credentials:

1. User enters their Family Assistant server URL
2. App opens an in-app browser (`ASWebAuthenticationSession`) to the server's `/app-auth` endpoint
   with a PKCE code challenge
3. User completes normal OIDC login
4. Server redirects back with an authorization code
5. App exchanges the code + PKCE verifier for API and refresh tokens
6. Tokens are stored securely in the iOS Keychain
7. App establishes a session cookie and loads the web UI

### Token Lifecycle

- API tokens expire after 30 days
- Refresh tokens expire after 90 days
- On each launch, the app refreshes tokens if needed
- If the refresh token is expired, the user is prompted to re-authenticate

## Architecture

```
FamilyAssistant/
├── FamilyAssistantApp.swift      # App entry point, URL scheme handling
├── ContentView.swift              # Root view: setup or web view
├── Auth/
│   ├── KeychainHelper.swift       # iOS Keychain wrapper
│   ├── AuthManager.swift          # Auth state + PKCE flow
│   └── SetupView.swift            # Server URL entry + sign in
└── WebView/
    ├── WebViewContainer.swift     # WKWebView wrapper
    └── WebViewToolbar.swift       # Navigation controls
```

## Dependencies

None. Uses only Apple frameworks:

- **SwiftUI** - UI
- **WebKit** - WKWebView
- **Security** - Keychain
- **AuthenticationServices** - ASWebAuthenticationSession
- **CryptoKit** - SHA-256 for PKCE

## Local Development

To test against a local server over HTTP, the app's Info.plist includes
`NSAllowsLocalNetworking = true`. For remote servers, HTTPS is required (iOS default).
