# iOS App Intents / Siri / Shortcuts

## Overview

The native iOS app exposes its core actions to **Siri** and the **Shortcuts app** through
[App Intents](https://developer.apple.com/documentation/appintents). Users can say "Ask Family
Assistant…", build Shortcuts, and (by wrapping the Quick Capture intent in a Shortcut with *Show in
Share Sheet*) send content from other apps.

All intents run **in the app's own process**, so they reuse the existing `AuthManager`,
`ChatAPIClient`, and `NotesAPIClient` unchanged. No App Group, Keychain access group, new target, or
new entitlement is required. The only backend change is that the non-streaming
`POST /api/v1/chat/send_message` endpoint now records **durable tool confirmations** (see
[Tool confirmations](#tool-confirmations)) — it previously could not request approval at all.

> A native Share Extension and Widgets are intentionally out of scope. Those run out-of-process and
> would require sharing credentials via an App Group + Keychain access group. The deep-link
> construction (`IntentSupport.chatDeepLink`) is centralized so such a target can reuse it later.

## Intents

All files live in `ios/FamilyAssistant/FamilyAssistant/Intents/`.

| Intent                   | Action            | Behavior                                                                                                                                      |
| ------------------------ | ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `AskAssistantIntent`     | Ask the assistant | Non-streaming `POST /api/v1/chat/send_message`; shows the reply as a Siri dialog + snippet with a "Continue in app" link.                     |
| `QuickCaptureIntent`     | Capture text/URL  | Sends untrusted shared content (constrained `email_intake` profile) wrapped in a "file this for me" instruction; same inline reply + snippet. |
| `CreateNoteIntent`       | Create a note     | `POST /api/notes/` via `NotesAPIClient`; confirmation dialog.                                                                                 |
| `OpenConversationIntent` | Open chat         | `openAppWhenRun`; foregrounds the app and (optionally) starts a new chat that auto-sends the message.                                         |

`FamilyAssistantAppShortcuts` (an `AppShortcutsProvider`) registers Siri phrases for each intent —
every phrase contains the required `\(.applicationName)` token.

## Auth & process model

- Intents call `IntentSupport.makeAuthenticatedManager()`, which builds an `AuthManager` (reads
  server URL from `UserDefaults`, token from Keychain) and throws `AssistantIntentError.needsSignIn`
  if the user is signed out. A background intent cannot run the interactive
  `ASWebAuthenticationSession` login, so it asks the user to open the app instead.
- Token refresh happens transparently via `AuthManager.authorizedRequest`.
- Intent turns use `interface_type = "web"` (`ChatConstants.interfaceType`) and a
  `web_conv_`-prefixed conversation ID, so they appear in the same conversation list as the native
  Chat tab.

### Trust profile for Quick Capture

`AskAssistantIntent` sends user-typed prompts, so it uses the trusted `default_assistant` profile
(`IntentSupport.defaultProfileID`). `QuickCaptureIntent` forwards content the user did **not**
author (a web page, an email), which is untrusted under the project's
[Rule of Two](../../AGENTS.md). It therefore routes through the constrained `email_intake` profile
(`IntentSupport.captureProfileID`), which denies destructive/browser/delegation tools, gates writes
behind confirmation, and treats the supplied content as untrusted. This keeps untrusted input from
reaching a profile that has both private-data access and state-changing actions.

## Inline reply + "Continue in app"

`AskAssistantIntent` / `QuickCaptureIntent` return `.result(dialog:view:)` with
`AssistantReplySnippet`. The snippet renders the reply and a `Link` to
`familyassistant://chat?conversation_id=<id>`, which the app already routes (`onOpenURL` →
`NotificationManager.handleDeepLink` → `AppRouter`). This is the path for any turn that needs
interactive tool approval — approvals are handled in the app, not inside a Siri snippet.

## Tool confirmations

A headless intent cannot wait on a live confirmation channel the way the streaming chat UI does, so
the non-streaming `POST /api/v1/chat/send_message` endpoint now installs a confirmation callback
that records a **durable** pending confirmation (`ConfirmationService.create_request`) and returns a
deferred "awaiting approval" tool result. The confirmation service push-notifies the user, and the
request is later approvable from any client via `GET /api/v1/chat/confirmations/pending` +
`POST /api/v1/chat/confirm_tool` — including the native Chat tab the "Continue in app" link opens.

The durable-confirmation logic is factored into
`family_assistant.services.confirmation_service.create_durable_confirmation`, shared by the
non-streaming endpoint, the email-intake flow, and the durable half of the streaming endpoint.

## Opening the app from an intent

`OpenURLIntent` is iOS 18+, but the app targets iOS 17, so `OpenConversationIntent` uses an
in-process `@Observable` singleton, `IntentNavigationCenter`. The intent records an app-relative
path (`/chat?q=<prompt>`); `ContentView` drains it on `.task` and on change, routing through the
same `AppRouter.navigate` flow as notification deep links. A non-empty `q` is auto-sent by
`ChatViewModel`.

## Key reused code

- `ChatAPIClient.sendMessage(prompt:conversationID:profileID:)` — new non-streaming send returning
  `ChatSendResult`.
- `NotesAPIClient.saveNote(_:)`, `AuthManager`, `KeychainHelper`.
- Deep-link routing: `AppRouting.swift`, `NotificationManager.handleDeepLink`,
  `ContentView.navigateToPending*`.

## Testing

`FamilyAssistantTests/AppIntentsTests.swift` exercises each intent's `perform()` with a
`URLProtocol` stub (`ChatMockBackendURLProtocol`) and the Keychain UserDefaults test fallback:

- `sendMessage` posts the web payload and decodes the reply.
- Ask / Quick Capture / Create Note hit the right endpoints with the right body; Quick Capture wraps
  content with a filing instruction.
- Missing credentials → `AssistantIntentError.needsSignIn`.
- Deep-link construction and `AppShortcutsProvider` registration.

Runs in CI via `.github/workflows/ios-tests.yml` (`xcodebuild test`).

## Verification (manual, signed in)

1. Shortcuts app shows all four actions with parameters.
2. "Ask the assistant" → inline reply → "Continue in app" opens the same thread.
3. "Create a note" → appears in the Notes tab and web UI.
4. "Quick capture" a URL → assistant files it; reply confirms.
5. "Open chat" with a message → app foregrounds, Chat tab auto-sends.
6. "Hey Siri, ask Family Assistant…" triggers the intent via the registered phrase.
