# watchOS voice mode

Add a companion watchOS app to the existing Xcode project, with a WidgetKit complication that opens
voice mode. Voice runs on the watch using its microphone, audio output and internet connection. The
iPhone does not relay audio or need to remain reachable during a session.

## Approach

Reuse the native session orchestration, audio conversion, transcripts, backend tools and
authentication. The watch signs in through the existing PKCE flow and stores its own credentials in
its Keychain. It has a small setup screen for the server URL and sign-in, avoiding a second
credential-transfer protocol or sharing rotating refresh tokens with the phone.

watchOS needs asynchronous audio-session activation before opening a streaming connection. Use
Network framework WebSockets on watchOS, retaining the existing injectable socket boundary. The
watch UI shows actual microphone activity, connection failures, mute and end controls, and the
latest transcript. Watch transcripts are live captions only; they are not saved to conversation
history.

The complication is a launcher. Tapping it opens the app and requests a voice session; microphone
permission and sign-in still apply. Merely displaying the complication never starts recording. An
ordinary app launch shows a Start button. Leaving the app ends the session; background calling and
automatic reconnection are outside this change.

## Milestones and verification

1. Add watch app and complication targets, embedded in the iOS distribution. Build both simulator
   and device targets without signing and validate their bundle relationships.
2. Share the native voice and auth code, with watch-specific audio activation and transport. Run the
   native voice and authentication regression tests and exercise cancellation and startup ordering
   with fake dependencies.
3. Add watch controls, complication launch routing and user documentation. Verify launch routing and
   build the complication families; live microphone, speaker, login and out-of-range calling require
   a physical Apple Watch smoke test.

## Deliberate simplifications

- The watch has its own sign-in and sign-out. Signing out of the phone does not sign out the watch.
- Watch voice provides live conversation and captions without transcript persistence. Reliable
  history across watch suspension requires durable delivery, which is outside the requested voice
  launcher feature. The watch therefore does not initiate best-effort transcript uploads.
- Use the default voice profile. Profile selection and browsing conversation history remain in the
  phone and web apps.
- Request a normal audio route; system routing and hardware determine speaker or Bluetooth output.
- Support watchOS 10 and later, matching the shared code's Observation and microphone APIs.

## Platform references

- [Low-level networking on watchOS](https://developer.apple.com/documentation/technotes/tn3135-low-level-networking-on-watchos)
- [Apple DTS guidance on audio activation and watch WebSockets](https://developer.apple.com/forums/thread/773362)
