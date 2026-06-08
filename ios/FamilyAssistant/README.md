# Family Assistant iOS App

A native iOS app that uses secure PKCE-based authentication, native APNs notification registration,
native Chat and Notes screens, and a WKWebView fallback for the rest of the Family Assistant web UI.

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
7. App establishes a session cookie for fallback web pages and opens native Chat

### Token Lifecycle

- API tokens expire after 30 days
- Refresh tokens expire after 90 days
- On each launch, the app refreshes tokens if needed
- If the refresh token is expired, the user is prompted to re-authenticate

### Native Chat

The app renders `/chat`, `/chat?conversation_id=<id>`, and `/chat?q=<prompt>` as native SwiftUI
screens instead of loading the browser chat in a web view. Native Chat keeps parity with the web
chat behavior rather than matching every pixel:

- Shared browser/iOS history through `interface_type=web` and `web_conv_<UUID>` conversation IDs
- Streaming assistant replies via the resumable-streaming flow: `POST /api/v1/chat/turns` to start a
  turn, then `GET /api/v1/chat/conversations/{id}/stream` to consume its SSE events
- Searchable conversation list and persisted last conversation
- Profile picker with profile selection persisted locally
- New conversation on profile switch
- Markdown rendering via Swift Markdown plus native `AttributedString`
- Tool-call cards and grouped completed tools
- Pending approval banner and inline approve/reject actions
- Image, PDF, plain text, and Markdown uploads up to 100 MB
- Authenticated attachment previews/downloads
- Live message update SSE connection and notification/deep-link routing
- Stop-generating and reload/error recovery paths

The native chat client calls these existing authenticated endpoints:

```http
GET /api/v1/chat/conversations?interface_type=web
GET /api/v1/chat/conversations/{conversation_id}/messages
GET /api/v1/profiles
POST /api/v1/chat/turns
GET /api/v1/chat/conversations/{conversation_id}/stream?from_seq=<n>&follow=<bool>
GET /api/v1/chat/confirmations/pending
POST /api/v1/chat/confirm_tool
POST /api/attachments/upload
DELETE /api/attachments/{attachment_id}
GET /api/attachments/{attachment_id}
```

Confirmation actions sent by the iOS app include `"approving_interface": "ios"` while the backend
continues to default older web/APNs callers to `"web"`.

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
- `conversation_id`: Opens native Chat at `/chat?conversation_id=<id>` when no explicit path is
  present
- `request_id`: Required for approval/denial actions on confirmation notifications

For notification action buttons, send APNs notifications with category
`FAMILY_ASSISTANT_CONFIRMATION` and include `request_id`. Optional `conversation_id` lets the app
return the user to the source conversation. The app posts action results to the existing
`POST /api/v1/chat/confirm_tool` endpoint with:

```json
{
  "request_id": "<confirmation-request-id>",
  "approved": true,
  "conversation_id": "<conversation-id-or-null>",
  "approving_interface": "ios"
}
```

Use category `FAMILY_ASSISTANT_MESSAGE` for normal message notifications.

### App Intents (Siri & Shortcuts)

The app exposes its core actions to Siri and the Shortcuts app via App Intents. They run in the
app's own process and reuse `AuthManager` and the existing API clients — no new target, entitlement,
or backend change is required. `FamilyAssistantAppShortcuts` registers Siri phrases automatically.

- **Ask Family Assistant** — non-streaming `POST /api/v1/chat/send_message`; presents the reply as a
  dialog + snippet with a "Continue in app" deep link.
- **Quick Capture** — sends text/links wrapped in a filing instruction (also reachable from the
  Share Sheet via a Shortcut).
- **Create Note** — `POST /api/notes/`.
- **Open Chat** — `openAppWhenRun`; foregrounds the Chat tab and can auto-send a message.

If the user is signed out, intents throw a "sign in" error rather than attempting interactive login.
See [docs/design/ios-app-intents.md](../../docs/design/ios-app-intents.md) for the full design.

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
├── Chat/
│   ├── ChatModels.swift           # Chat/profile/message/tool/attachment models
│   ├── ChatAPIClient.swift        # Authenticated chat, SSE, approval, and attachment API calls
│   ├── SSEParser.swift            # Server-Sent Events parser
│   ├── ChatViewModel.swift        # Single owner of native chat state
│   └── ChatViews.swift            # Native chat SwiftUI screens
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
│   ├── NotesAPIClient.swift       # Authenticated Notes API calls
│   ├── NotesRootView.swift        # Notes route container
│   ├── NotesListView.swift        # Searchable native notes list
│   ├── NoteDetailView.swift       # Native note reader
│   └── NoteEditorView.swift       # Native create/edit form
└── Intents/
    ├── IntentSupport.swift              # Auth helper, deep links, IntentNavigationCenter
    ├── AskAssistantIntent.swift         # "Ask Family Assistant"
    ├── QuickCaptureIntent.swift         # "Capture this in Family Assistant"
    ├── CreateNoteIntent.swift           # "Add a note to Family Assistant"
    ├── OpenConversationIntent.swift     # "Open Family Assistant chat"
    ├── AssistantReplySnippet.swift      # Inline reply + "Continue in app" snippet
    └── FamilyAssistantAppShortcuts.swift # Siri phrase registration
```

## Dependencies

- **SwiftUI** - UI
- **WebKit** - WKWebView
- **Security** - Keychain
- **AuthenticationServices** - ASWebAuthenticationSession
- **CryptoKit** - SHA-256 for PKCE
- **UserNotifications** - APNs permission, notification presentation, actions
- **PhotosUI** - Native image picker
- **UniformTypeIdentifiers** - Attachment MIME/type validation
- **Swift Markdown 0.8.0** - GitHub-flavored Markdown parsing support

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

Native chat UI tests retain XCTest screenshots with `.keepAlways` for the key states reviewers need
to inspect:

- `native-chat-history`
- `native-chat-streamed-tool-call`
- `native-chat-initial-prompt`

When opening a PR for native chat changes, include the iOS test result bundle or CI artifact that
contains these attachments, and link it from the PR body or a PR comment.

## Local Development

To test against a local server over HTTP, the app's Info.plist includes
`NSAllowsLocalNetworking = true`. For remote servers, HTTPS is required (iOS default).

APNs delivery requires a signed device build with the Push Notifications entitlement. The simulator
can exercise notification response handling with local test payloads, but it cannot validate real
APNs token delivery.
