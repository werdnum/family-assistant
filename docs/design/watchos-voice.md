# watchOS voice mode

Add a companion watchOS app to the existing Xcode project, with a WidgetKit complication that opens
voice mode. Voice runs on the watch using its microphone, audio output and internet connection. The
iPhone does not relay audio or need to remain reachable during a session.

## Approach

Reuse the native session orchestration, audio conversion, transcripts, backend tools and
authentication. Setup uses the signed-in paired iPhone through WatchConnectivity. The phone obtains
a separate credential pair from the backend and returns it directly to the requesting watch, which
stores it in its Keychain. The phone's refresh token never leaves the phone/backend boundary.
Issuance requires both the phone's access and matching refresh credential, and does not extend the
refresh credential's lifetime. Voice and subsequent token refreshes run directly on the watch.

The watch offers “Set up with iPhone” instead of a URL field or browser. Authentication expiry sends
the user back to that flow. A non-secret phone-session identity is synchronized to detect phone
sign-out and account changes; credentials only travel in an interactive reply, never queued context.
Late replies must not install credentials after the phone or watch setup session has changed.

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
4. Replace watch-side login with paired-iPhone setup. Verify independent credential issuance with
   both token formats and database engines, rejection of mismatched phone credentials, watch
   Keychain installation and account-change handling. Check the setup screen in a watch simulator;
   real WatchConnectivity delivery requires a paired-device smoke test.

## Deliberate simplifications

- Setup requires the paired phone to be reachable and signed in. Phone sign-out/account changes
  clear the watch when connectivity delivers the update, not immediately while disconnected.
  Previously issued watch credentials remain independently revocable through token settings.
- Preserve the existing access/refresh expiration policy: the watch inherits the phone refresh
  deadline, but access tokens issued before that deadline retain the server's normal lifetime and
  may outlive it, just as phone access tokens do. This is not a strict coupling of the two devices'
  access lifetimes. Changing that server-wide policy is outside the paired-setup feature.
- No new Apple portal capabilities or shared Keychain/App Group entitlements are needed.
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
