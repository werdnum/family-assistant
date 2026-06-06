# Family Assistant iOS App

A native iOS app that wraps the Family Assistant web UI in a WKWebView with secure PKCE-based
authentication and native APNs notification registration.

## Requirements

- Xcode 15.4+ (free from Mac App Store)
- iOS 17.0+ device or simulator
- Any Apple ID for signing (free personal team works for sideloading)

## Setup

1. Open `FamilyAssistant.xcodeproj` in Xcode
2. Select your development team under **Signing & Capabilities**
3. Change the bundle identifier to something unique (e.g., `com.yourname.familyassistant`)
4. Make sure **Push Notifications** is enabled for the bundle ID in Apple Developer
5. Build & Run on your device or simulator

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

### Native Notifications

The app can register with APNs and send the device token to the Family Assistant server. Users turn
notifications on or off from the gear menu in the web view toolbar.

Client behavior:

1. User taps **Enable Notifications** in the toolbar menu
2. App requests iOS notification permission
3. App registers with APNs and receives a device token
4. App stores the token locally and sends it to the server using the current API token
5. App re-registers the stored token after launch/auth bootstrap if notifications are still enabled
6. App unregisters the stored token and disables APNs registration on sign-out or explicit disable

Expected server endpoints:

```http
POST /api/ios/push-tokens
Authorization: Bearer <api token>
Content-Type: application/json

{
  "token": "<apns-device-token-hex>",
  "environment": "sandbox",
  "bundle_id": "dev.andrewgarrett.assistant",
  "installation_id": "<stable-app-installation-uuid>",
  "device_name": "Andrew's iPhone"
}
```

```http
DELETE /api/ios/push-tokens/{token}
Authorization: Bearer <api token>
```

Notification payload handling:

- `path`: Relative app path to open, such as `/chat` or `/tasks`
- `url`: Absolute or relative URL; the client strips external origins and navigates by path
- `conversation_id`: Opens `/chat?conversation_id=<id>` when no explicit path is present
- `request_id`: Required for approval/denial actions on confirmation notifications

For notification action buttons, send APNs notifications with category
`FAMILY_ASSISTANT_CONFIRMATION` and include `request_id`. Optional `conversation_id` lets the app
return the user to the source conversation. The app posts action results to the existing
`POST /api/v1/chat/confirm_tool` endpoint with:

```json
{
  "request_id": "<confirmation-request-id>",
  "approved": true,
  "conversation_id": "<conversation-id-or-null>"
}
```

Use category `FAMILY_ASSISTANT_MESSAGE` for normal message notifications.

Manual APNs configuration:

- Enable Push Notifications for the App ID in Apple Developer
- Create/configure an APNs auth key on the server side
- Keep the Xcode bundle identifier, server `apns-topic`, and Apple App ID aligned
- Use a real iOS device for end-to-end APNs testing

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
└── Notifications/
    ├── AppDelegate.swift          # APNs and notification response callbacks
    └── NotificationManager.swift  # Permission, token sync, notification routing
```

## Dependencies

None. Uses only Apple frameworks:

- **SwiftUI** - UI
- **WebKit** - WKWebView
- **Security** - Keychain
- **AuthenticationServices** - ASWebAuthenticationSession
- **CryptoKit** - SHA-256 for PKCE
- **UserNotifications** - APNs permission, notification presentation, actions

## Building from Command Line

If you prefer to build from the command line:

```bash
# Build for iOS Simulator
xcodebuild -project FamilyAssistant.xcodeproj \
  -target FamilyAssistant \
  -sdk iphonesimulator \
  -configuration Debug \
  CODE_SIGN_IDENTITY="" \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGNING_ALLOWED=NO

# Output will be in:
# build/Debug-iphonesimulator/FamilyAssistant.app
```

## Local Development

To test against a local server over HTTP, the app's Info.plist includes
`NSAllowsLocalNetworking = true`. For remote servers, HTTPS is required (iOS default).

APNs delivery requires a signed device build with the Push Notifications entitlement. The simulator
can exercise notification response handling with local test payloads, but it cannot validate real
APNs token delivery.
