# Starting a voice session from Siri, as a call

**What's here:** how "Hey Siri, call Family Assistant" reaches the existing
Gemini Live voice session, and why the session is presented to the system as a call.

## Why a call

Voice mode already works, but only from a foreground tap: open the app, go to the Voice tab,
wait for the session to connect. That is the wrong shape for the situation voice is most
useful in — driving — where the phone is locked, in a pocket or a cradle, and the user cannot
look at it.

Presenting the session as a call is what closes that gap, and it does so for reasons that are
mostly about what the platform will let a third-party app do rather than about the metaphor:

- **Siri will start it hands-free.** The calling domain is one of the intent domains Siri
  resolves against an app name and continues into the app while the device is locked. Custom
  intents that need the app opened do not reliably survive that path, and in CarPlay
  specifically they are reported to fail where the calling intent succeeds.
- **CarPlay renders it with no entitlement.** An app implementing CallKit gets the system
  calling interface on a CarPlay head unit for free. That is a different thing from a
  dedicated CarPlay app, which needs an entitlement Apple grants case by case; this design
  deliberately depends on neither.
- **The system arbitrates audio.** Mute, hang up, route selection, ducking other audio, and
  yielding to a real phone call all become the system's job rather than ours.

The cost is that the assistant conversation borrows a UI built for talking to a person. That
is a fair trade for a session that is, in substance, a two-way voice conversation the user
started on purpose.

## Shape

Nothing about the voice session itself changes. The token fetch, the Gemini Live socket, the
tool runner and the transcript all stay exactly as they are. Two things are added around them,
and one existing thing is inverted.

**Added: a call surface.** A coordinator owns the `CXProvider` and `CXCallController`, turns a
request to talk into an outgoing call, and maps the call's lifecycle onto the session's — the
call reports connected when the Live session completes setup, and ending either one ends the
other. It is the only place that knows CallKit exists.

**Added: a way in from Siri.** Two things, because Siri needs both a declaration and something to
resolve a spoken name against.

The declaration lives in an Intents app extension. SiriKit's calling domain is reached through the
extension point, not through the app: an app that declares nothing there is not a call provider as
far as Siri is concerned, and "call using Family Assistant" is refused outright, before any activity
continuation is considered. The extension does exactly two things — declare the intent and resolve
the destination — and it must do no more, because a CallKit transaction issued from an extension
fails on entitlements the extension cannot hold. It answers *continue in app*, which is what turns a
resolved intent into a user activity the app receives, and the app places the call.

The donation is what makes the app's own name resolvable as a destination, and it prompts for
nothing, so it happens on the only condition the user is told about — being signed in — from app
scope rather than from a view; the one part that does prompt, Siri authorization, stays where the
user has shown an interest in talking.

The destination is checked, because Siri resolves "call someone using Family Assistant" to this app
too, and answering that with an assistant conversation would discard the destination the user asked
for. An assistant call has exactly one destination, so the request is ours only when the single name
it carries is the assistant's; naming anyone else, or naming the assistant alongside somebody else,
is refused rather than narrowed. An intent that names no destination at all is ours, because Siri
resolved the app itself. That rule is one predicate compiled into both the extension, which refuses
the intent, and the app, which refuses the activity — the two cannot be allowed to disagree about
who the assistant is. The extension answers Siri with one resolution result per name the intent
carried, as SiriKit reads that array positionally.

**Inverted: who owns the audio session.** Today `VoiceAudioEngine` configures *and activates*
`AVAudioSession` itself. Under CallKit that is wrong: CallKit activates the session, and audio
started before it does will be silent or fail. The engine therefore gains an activation policy.
Self-managed is what the in-app Voice tab keeps doing. Externally-managed splits the engine's two
jobs and hands the first to whoever activates the session: configuring the category is the call
owner's, done before the call's audio session can be activated, and the engine only waits to be
told the session is live and does not deactivate it on teardown. The wait is what CallKit's
activation callback releases.

The split matters because activation is not something the coordinator waits for but something it
causes: fulfilling the start action is what invites CallKit to activate the session, and a session
still on its default category at that moment brings the call up with no audio. Everything the audio
session must be true of therefore happens before the action is fulfilled, and nothing that can
block — permission, a token, a socket — may sit in front of it.

That seam is why the view model needs no changes at all: it already starts audio before opening
the socket, so blocking inside `start()` until CallKit is ready sequences the whole thing
correctly, and the session's existing connection timeout already bounds the wait.

## The paths in

Every request passes through the extension first, which resolves it and hands it back to the system
as an activity to continue in the app. From there, two paths, and they must both work, because they
are the same feature from the user's point of view and they arrive through different system hooks.

- **Warm**, with a scene already connected: the activity arrives at the scene delegate.
- **Cold and locked**, with the app not running: the system launches the app in the background
  and delivers through the application delegate.

The activity's type is what the app matches an arrival on, so the extension sets it rather than
letting SiriKit invent one: SiriKit makes an activity when handed none, but the type it picks is not
documented, and a match against an assumed string is a match that can quietly stop matching. One
constant is compiled into both sides, and it is the same string the app's `NSUserActivityTypes`
declares — a type the app does not declare is never delivered to it at all.

Every step between Siri and a running call records that it was reached and what it decided, into the
telemetry lane under one `Voice.call.` prefix. This path runs on a locked device, through hooks a
simulator never exercises, and has no channel back to the user: without the records, a request
refused by the destination rule, a request arriving under an unexpected type, a request buffered for
a handler that never installed, and a request that was never made all look identical afterwards.
Arrivals are recorded before anything about them is judged, since the unexpected activity is exactly
the one a predicate would discard, and a call that starts records too — a path that only reports
refusals cannot tell success from silence. The records carry decisions, types and counts, never a
spoken word or a contact name.

Both forward to one place, and the call starts there, at the point of delivery: the scenario the
whole feature exists for has no scene, so anything that waits for the view layer waits until the
user picks up the phone — the interaction being avoided. What acts on a request is therefore
owned at process scope and installed before any activity can arrive, which also means it owns the
call for as long as the call lasts. The app installs its own scene delegate for home-screen quick
actions, which is why neither delivery hook can be left to SwiftUI's built-in ones — the same trap
that once broke `.onOpenURL`.

## Who may talk to the assistant hands-free

Making voice reachable from a locked phone is the point of this design, and it is also its
only new exposure: anyone holding the locked phone could otherwise start an open-ended
conversation with the household's assistant and its tools. The calling domain makes this
sharper than it is for the one-shot ask intent, because iOS permits a start-call intent from a
locked device without consulting anything of ours.

The rule is therefore that assistant voice may start without an unlock only when the phone is
connected to CarPlay. Being plugged into the car is the authentication: someone driving the
car with the phone connected to it is already past every boundary this could protect.

One predicate answers that question for every entry point — the ask intent and the call
coordinator both consult it, so a future entry point that forgets to is the thing that looks
wrong rather than the thing that quietly works. It reads two probes, both injected so the rule
is testable without a locked device or a car:

- Whether the device is unlocked. There is no API for this; protected-data availability is the
  conventional proxy and is accurate apart from a short grace period after locking.
- Whether an output route is CarPlay. This is an audio-route property, readable by any app, and
  needs no CarPlay entitlement.

Consequently the ask intent declares that it is always allowed to run, rather than letting the
system gate it. Whether the system evaluates an intent's authentication policy per invocation
or treats it as build-time metadata is not something this design wants to depend on, so the
policy stops arbitrating and the app decides in code that can be tested.

**Bluetooth hands-free does not count.** Only CarPlay does. An ordinary Bluetooth car pairing
is a much weaker claim about who is in the car, and the stricter reading is the safe default.

**A refusal on the call path is quiet.** The start-call intent arrives as a user activity, which
has no channel back to Siri, so a refused call means Siri says "calling…" and nothing happens.
That is the correct outcome and a poor explanation of it; it is accepted rather than solved,
because the alternative is showing a failed call on the lock screen of a phone whose holder we
have just decided not to talk to.

## What this does not do

- **No custom CarPlay interface.** CarPlay shows the system call screen. A Home Screen icon and
  a purpose-built conversational screen need `com.apple.developer.carplay-voice-based-conversation`,
  which is a separate, reviewed request. This design is the thing worth having before deciding
  whether to make that request.
- **No incoming calls.** The call is always outgoing and always user-started, so there is no
  PushKit and no `voip` background mode. The existing `audio` background mode is what keeps the
  session alive; nothing here uses an API reserved for VoIP apps.
- **No new confirmation path.** Tools that are confirmation-gated still fail closed in a voice
  session, as they do today. Review policies cover the cases that matter, and adding spoken
  confirmation is independent of how the session was started.

## Deliberate simplifications

- **One conversation at a time.** The provider advertises a single call group of one, and the
  in-app Voice tab stands aside while a call is running rather than starting a session of its
  own. Two simultaneous assistant conversations have no meaning, and refusing them is cheaper
  than reasoning about the audio consequences — there is one process-wide `AVAudioSession`, and
  a second session would both compete for the microphone and deactivate it out from under the
  call when it closed. Standing aside is visible, not silent: the tab says the call is where the
  conversation is, and starts a session of its own once the call ends. The system is told the same
  thing about the call itself, which is declared to support none of the multi-call operations — no
  holding, grouping, ungrouping or tones. That is what makes a real phone call end the conversation
  instead of leaving CallKit waiting out an action nothing here answers.
- **The handle is a constant, not a contact.** The assistant is donated as one generic handle
  rather than written into the user's contacts. It keeps Siri resolution working without the
  app taking a write dependency on the address book.
- **The extension is tested through what it shares, not through itself.** An Intents extension is a
  separate bundle with no test host, so the app-hosted unit tests cannot load it. The decision it
  makes — whether a spoken destination is the assistant — therefore lives in the file both targets
  compile and is tested there. What remains in the extension is the plumbing that hands that
  decision to SiriKit, which only a device exercises.
- **The Siri path is unverified until it runs on real hardware.** Locked-phone cold start,
  CarPlay Siri, and the intent-to-app handoff are exactly the things a simulator cannot
  demonstrate. The unit tests cover the coordinator's state machine and the activation seam;
  the rest is a device test, and the design accepts that the first real answer comes from
  TestFlight.

## Work plan

1. **Audio activation policy.** `VoiceAudioEngine` gains the externally-managed mode, in which
   configuration is separable from startup, and the awaited activation signal. Verified by unit
   tests that a self-managed engine activates as before, that an externally-managed one does not
   start until signalled, and that its category is configured before the start action is fulfilled.
2. **The call surface.** The coordinator, behind protocols for the CallKit provider and call
   controller so the state machine is testable without a real call. Verified by unit tests
   driving start, connect, remote-end and reset.
3. **The hands-free access rule.** The shared predicate and its two injected probes, applied at
   both entry points. Verified by unit tests over the truth table, and by the ask intent and the
   coordinator refusing when it says no.
4. **The way in from Siri.** The Intents extension that declares and resolves the intent, the
   donation, both delivery hooks, and the wiring that turns a delivered activity into a call.
   Verified by unit tests over the shared destination rule and the activity-to-request translation;
   end-to-end behaviour is a device test.
