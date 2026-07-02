# Native iOS Voice Mode

Status: Implemented (in-app screen; CallKit/CarPlay remain a fast-follow, Milestone 7)

This document plans a native voice-conversation screen for the iOS app, replacing the current
web-backed `/voice` destination with a first-class native experience. It supersedes the placeholder
"Phase 3: CallKit + Voice Mode" stub in [ios-app.md](ios-app.md).

## Goals

- A native, full-screen, hands-free voice conversation with the assistant.
- Realtime, low-latency, barge-in-capable audio (speak over the assistant to interrupt).
- Continues running while backgrounded / screen-locked (audio background mode).
- Same assistant tools as text chat (minus confirmation-gated tools, which voice cannot approve).
- Voice sessions are captured as their own conversation in chat history (transcript), so the user
  can review what was said.
- Designed so CallKit + CarPlay can be layered on later without re-architecting.

## Non-goals (first version)

- CallKit lock-screen call UI and CarPlay (fast-follow, see Milestone 7).
- Merging a voice session into an existing text conversation thread.
- On-device speech recognition (`Speech.framework`) — Gemini Live provides transcription.

## Decisions (confirmed)

1. **Transport: Direct-to-Gemini**, mirroring the web frontend. The app fetches an ephemeral token
   from the backend and opens a WebSocket **directly to Google's Gemini Live API**. The backend is
   used only for the token and for tool execution. Rationale: lowest latency, no server audio load,
   reuses the already-proven `POST /api/gemini/ephemeral-token` contract and the web client's exact
   message flow.
2. **In-app screen first; CallKit/CarPlay later.** Build the audio + UI on `AVAudioEngine` /
   `AVAudioSession` with the `audio` background mode now. Defer `CXProvider`/`CXCallController` and
   the CarPlay entitlement to a fast-follow.
3. **Persist transcripts as a separate conversation.** A finished voice session is written to a new
   chat conversation (not appended to an existing text thread), so it shows up in the conversation
   list and can be reviewed/continued in text.

## What already exists (reuse, don't rebuild)

Backend:

- `POST /api/gemini/ephemeral-token` (`web/routers/gemini_live_api.py`) → returns
  `{token, expires_at, model, tools, system_instruction, config{voice, vad, transcription, session.max_duration_minutes, greeting, ...}}`.
  Token is single-use, ~30 min validity, 15 min session cap. Tools are pre-filtered to exclude
  confirmation-required tools and pre-converted to Gemini `FunctionDeclaration` format. System
  instruction already has user context injected.
- `GET /api/gemini/status` → availability + feature flags + limits.
- `POST /api/tools/execute/{tool_name}` (`web/routers/tools_api.py`) with body
  `{"arguments": {...}}` → executes a tool with full backend context and returns its result.
- Auth: session-based (the app already establishes a session cookie via `/api/auth/token-session`,
  and sends `Authorization: Bearer` on API calls via `AuthManager`).

Web reference implementation (the contract to mirror in Swift) — `frontend/src/voice/`:

- `useGeminiLive.ts`: fetches the token, then
  `new GoogleGenAI({apiKey: token, httpOptions: {apiVersion: 'v1alpha', baseUrl: 'https://generativelanguage.googleapis.com'}})`
  and `client.live.connect({model, config})`. Tool calls → `POST /api/tools/execute/{name}` →
  `session.sendToolResponse({functionResponses})`.
- `useAudioCapture.ts`: mic → **16 kHz mono PCM16**, `audio/pcm;rate=16000`.
- `useAudioPlayback.ts`: plays Gemini's **24 kHz mono PCM16** with gapless queueing.

iOS app (current state):

- No audio/voice code at all. `MoreTabView` has a web-backed
  `MoreDestination(title: "Voice", path: "/voice")` — this will be replaced by a native route.
- Chat: `ChatAPIClient` (REST + SSE), `AuthManager` (PKCE OAuth, Keychain tokens,
  `authorizedRequest`), `AppRouter` navigation, APNs already wired in
  `AppDelegate`/`NotificationManager`.
- Deployment target iOS 17, SwiftUI, `@Observable`. App-hosted unit tests (see Testing).

## Architecture

```
┌─────────────────────────── iOS app ───────────────────────────┐
│  VoiceView (SwiftUI, full screen)                              │
│        │ binds                                                 │
│  VoiceSessionViewModel  @MainActor @Observable                │
│    ├─ GeminiLiveClient   (URLSessionWebSocketTask to Google)  │
│    ├─ VoiceAudioEngine   (AVAudioEngine capture+playback, AEC)│
│    ├─ ToolCallRunner     (POST /api/tools/execute/{name})     │
│    └─ TranscriptRecorder (accumulates in/out transcripts)     │
└───────────────────────────────────────────────────────────────┘
   │ 1. POST /api/gemini/ephemeral-token (Bearer)   │ tools
   ▼                                                ▼
 Backend (token + system_instruction + tools + config)   /api/tools/execute
   │ token
   ▼
 wss://generativelanguage.googleapis.com  (direct, ephemeral token)
```

### Components

- **`GeminiLiveClient`** — owns a `URLSessionWebSocketTask` to Google's Live endpoint. There is **no
  official Swift SDK for the Live API**, so this hand-rolls the BidiGenerateContent JSON protocol
  (the same wire protocol `@google/genai` speaks). Responsibilities: connect with the ephemeral
  token, send the setup message, send realtime audio chunks, receive/parse server messages, surface
  tool calls, send tool responses, detect `turnComplete`/`interrupted`. Pure protocol — no audio, no
  UI — so it is unit-testable with a fake socket.
- **`VoiceAudioEngine`** — `AVAudioEngine` graph. Capture: `inputNode` tap → `AVAudioConverter` to
  16 kHz mono Int16 → base64 → client. Playback: incoming 24 kHz PCM16 → `AVAudioPlayerNode`
  (scheduled buffers / jitter queue). Uses hardware **acoustic echo cancellation** so the assistant
  doesn't hear itself (see Audio Session below). Exposes interrupt/flush for barge-in.
- **`ToolCallRunner`** — bridges Gemini function calls to `POST /api/tools/execute/{name}` via the
  existing `AuthManager.authorizedRequest`, returns results to the client.
- **`TranscriptRecorder`** — accumulates Gemini input/output transcription segments into an ordered
  list of `(role, text)` turns for persistence.
- **`VoiceSessionViewModel`** — state machine wiring the above:
  `idle → requestingToken → connecting → listening ⇄ assistantSpeaking → ending → ended/error`.

### Gemini Live wire protocol (to implement in Swift)

Endpoint:
`wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent`
authenticated with the ephemeral token. **Spike item:** confirm exactly how the ephemeral token is
presented on a raw connection (query param `access_token=<token>` vs `key=`/header) by inspecting
what `@google/genai` sends; this is the single biggest unknown and is settled in Milestone 1.

- **Setup (client→server, first frame):** `BidiGenerateContentSetup` with `model`,
  `generationConfig.responseModalities=["AUDIO"]`,
  `speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName` (from token `config.voice.name`),
  `systemInstruction` (token `system_instruction`), `tools` (token `tools`),
  `inputAudioTranscription`, `outputAudioTranscription`, and `realtimeInputConfig` (VAD from token
  `config.vad`). Mirror the field set used in `useGeminiLive.ts`.
- **Realtime audio (client→server):**
  `realtimeInput.audio={mimeType:"audio/pcm;rate=16000", data:<base64 PCM16>}`, ~tens-of-ms frames.
  The native client intentionally uses the current Live API shape and does not send the older
  `realtimeInput.mediaChunks` wrapper.
- **Server→client:** `setupComplete`; `serverContent` with `modelTurn.parts[].inlineData` (24 kHz
  PCM16 audio), `inputTranscription`, `outputTranscription`, `turnComplete`, `interrupted`;
  `toolCall.functionCalls[]`; `toolCallCancellation`.
- **Tool response (client→server):** `toolResponse.functionResponses=[{id, name, response}]` after
  calling the backend executor.
- **Barge-in:** on `interrupted` (or local VAD detecting user speech), flush the playback queue
  immediately.

### Audio session

- `AVAudioSession` category `.playAndRecord`, mode `.voiceChat`, options
  `[.allowBluetooth, .allowBluetoothA2DP, .defaultToSpeaker]`. `.voiceChat` engages the system
  **AEC** path; we will use the engine's voice-processing IO so the mic does not pick up assistant
  playback. (Alternative: `AVAudioEngine.inputNode.setVoiceProcessingEnabled(true)`.)
- Handle `AVAudioSession.interruptionNotification` (calls, Siri), `routeChangeNotification`
  (headphones/Bluetooth/CarPlay), and media-services-reset by tearing down and rebuilding the graph.
- `UIBackgroundModes = ["audio"]` so the session continues when backgrounded/locked. This must be
  justified to App Review as an active voice-assistant conversation (it is).

### Permissions / Info.plist / entitlements

- `NSMicrophoneUsageDescription` (required). Request via
  `AVAudioApplication.requestRecordPermission` (iOS 17). No `NSSpeechRecognitionUsageDescription`
  needed (transcription is server-side via Gemini).
- `UIBackgroundModes` += `audio`.
- No new entitlements for the in-app version. (CarPlay entitlement is a Milestone 7 concern.)

### Transcript persistence (separate conversation)

There is **no existing HTTP endpoint that writes a finished transcript**. The existing chat write
endpoints (`POST /v1/chat/turns`, `POST /v1/chat/send_message`) *execute* a turn through the LLM —
they re-run the assistant, which is wrong for a conversation that already happened with Gemini.

What does exist is a **storage-layer primitive**:
`storage/message_history.add_message_to_history( db_context, interface_type, conversation_id, role, content, turn_id, timestamp, ...)`.
This writes a single message into the `message_history` table that the chat UI reads from. The
Asterisk voice path already uses exactly this to record voice turns (`interface_type="telephone"`),
and `get_conversations` / `get_conversation_messages` read straight from that table.

Plan: add a thin endpoint `POST /api/v1/chat/voice-sessions` that loops the accumulated transcript
turns through `add_message_to_history` under a fresh `conversation_id` (title prefix "Voice ·
{date}", new `interface_type="voice"`). No LLM re-run, no new storage layer.

- **Ownership gate (important):** `get_conversations` and per-conversation reads are filtered by an
  identity-aware owner predicate (`_ensure_user_owns_conversation`). The voice rows must be written
  with the authenticated iOS user's identity so the new conversation actually surfaces in their list
  and opens without a 404 — verify the resolver maps the message's identity fields to the caller.
- Client: `ChatAPIClient.saveVoiceSession(turns:)` posts on end; `ChatViewModel` refreshes the list.
- If the app is killed mid-session, persistence is best-effort (post incrementally or on `ending`).

## Navigation / entry points

- Replace the web `MoreDestination(path:"/voice")` with a native route that presents `VoiceView`
  full-screen (`AppRouter` + a `.fullScreenCover` or pushed route).
- Add a microphone affordance in the chat composer (`ChatComposerView`) to launch voice mode.
- Wire the existing `AskAssistantIntent` / Siri path to optionally open voice mode (later).

## Milestones

Each milestone is independently testable and committable.

1. **Connectivity spike (text-only).** `ChatAPIClient.fetchEphemeralToken()`; minimal
   `GeminiLiveClient` that connects, sends setup, and proves `setupComplete` + a text response over
   the raw WebSocket. Resolves the ephemeral-token-auth unknown. No audio, no UI yet. Unit tests for
   message encode/decode against a fake socket.
2. **Audio I/O.** `VoiceAudioEngine`: 16 kHz capture with AEC, 24 kHz gapless playback, barge-in
   flush. Verify a full spoken round-trip end-to-end (hard-coded launch, debug screen).
3. **Voice UI.** `VoiceView` + `VoiceSessionViewModel` state machine: connection states, mic/agent
   activity indicator, live captions (from transcription), mute, end. Native navigation entry
   (replace web `/voice`, add composer mic button).
4. **Tool calls.** `ToolCallRunner` → `/api/tools/execute/{name}` → `sendToolResponse`. Verify a
   tool-using voice request (e.g. "what's on my calendar today").
5. **Resilience.** Background `audio` mode; interruption/route-change handling; 15-min session cap
   and graceful re-token; error reporting via `ErrorReporter`; reconnect/teardown.
6. **Transcript persistence.** Backend `POST /api/v1/chat/voice-sessions` + client save-on-end;
   conversation appears in the list.
7. **Fast-follow: CallKit + CarPlay.** `CXProvider`/`CXCallController` for lock-screen call UI and
   system audio routing; request the CarPlay communication/audio entitlement from Apple; CarPlay
   scene. (Separate design doc when we get here.)

## Testing

- **Unit-testable seams:** `GeminiLiveClient` protocol codec (fake `WebSocketTask`), audio format
  conversion, `TranscriptRecorder` assembly, `ToolCallRunner` (fake API client). These run in the
  app-hosted unit bundle.
- **Caution (from prior iOS work):** unit tests are app-hosted — the host app must **not** boot its
  UI under XCTest. Keep `VoiceView`/engine out of the launch path under test. Reproduce CI by
  running the full bundle, and build to local disk (not the shared mount) when validating.
- Avoid real microphone/network in tests; inject fakes for the socket, audio engine, and API.

## Risks / open questions

- **No official Swift Live SDK.** Google ships official `genai` SDKs for Python, JS/TS, Go and Java
  — but not Swift, and none of them speak the Live API from Swift. The one real library option is
  the **Firebase AI Logic** Swift SDK (`FirebaseAI`), which does expose a Live/bidirectional model —
  but it authenticates through a Firebase/Vertex project, not our ephemeral-token →
  `generativelanguage.googleapis.com` flow. Adopting it would mean a different auth/transport model
  (Firebase project + App Check) and would not reuse `/api/gemini/ephemeral-token`. Community Swift
  packages exist but are immature. **Recommendation: DIY the raw WebSocket** — the protocol is plain
  JSON frames and we already know the exact config from the web client; this keeps the proven
  ephemeral-token architecture. De-risked by the Milestone-1 spike. If it proves too fiddly, the
  fallbacks (in order) are: (a) evaluate the Firebase AI Logic SDK, or (b) the backend-bridge
  transport (mirror `asterisk_live_api.py`) — a larger backend change but a trivial client protocol.
- **Ephemeral-token connection auth** detail on a raw socket — settled in Milestone 1.
- **AEC quality** with `.voiceChat` / voice-processing IO across speaker vs Bluetooth routes.
- **App Review** justification for the `audio` background mode.
- **CarPlay** requires Apple entitlement approval (Milestone 7 gating).
