# Family Assistant iOS App

A native iOS app that uses secure PKCE-based authentication, native APNs notification registration,
native Notes screens, and a WKWebView fallback for the rest of the Family Assistant web UI.

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

### Native Notes

The app renders `/notes`, `/notes/add`, and `/notes/edit/<title>` as native SwiftUI screens instead
of loading those pages in the web view. Notes support:

- Searchable native list
- Detail view with selectable text
- Create, edit, and delete
- `include_in_prompt` editing
- Attachment and visibility metadata preservation when saving

The native Notes client calls the existing authenticated API endpoints:

```http
GET /api/notes/
GET /api/notes/{title}
POST /api/notes/
DELETE /api/notes/{title}
```

Attachment upload and preview still live in the web UI. The native editor preserves existing
`attachment_ids` and `visibility_labels` values when saving so notes are not stripped of metadata
that the native screen does not edit yet.

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
├── ContentView.swift              # Root view: setup, native routes, or web fallback
├── AppRouting.swift               # Native/web route selection
├── AppSettingsMenu.swift          # Shared settings, notification, and sign-out menu
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
└── Notes/
    ├── NotesAPIClient.swift       # Authenticated Notes API calls
    ├── NotesRootView.swift        # Notes route container
    ├── NotesListView.swift        # Searchable native notes list
    ├── NoteDetailView.swift       # Native note reader
    └── NoteEditorView.swift       # Native create/edit form
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

## Continuous Integration

The `iOS Tests` GitHub Actions workflow (`.github/workflows/ios-tests.yml`) runs the
`FamilyAssistantTests` unit tests on every push and pull request that touches `ios/**`. It builds
the `FamilyAssistant` scheme on a `macos-26` runner (Apple Silicon, Xcode 26.x) against an iOS
Simulator with code signing disabled. The workflow resolves an available iPhone simulator
dynamically (the device lineup changes between Xcode releases), so the effective command is roughly:

```bash
xcodebuild test \
  -project FamilyAssistant.xcodeproj \
  -scheme FamilyAssistant \
  -destination "platform=iOS Simulator,id=<resolved-iphone-simulator-udid>" \
  CODE_SIGNING_ALLOWED=NO
```

Changes outside `ios/**` do not trigger this workflow, and iOS-only changes are excluded from the
Linux devcontainer CI so the two suites stay independent.

## Local Development

To test against a local server over HTTP, the app's Info.plist includes
`NSAllowsLocalNetworking = true`. For remote servers, HTTPS is required (iOS default).

APNs delivery requires a signed device build with the Push Notifications entitlement. The simulator
can exercise notification response handling with local test payloads, but it cannot validate real
APNs token delivery.
