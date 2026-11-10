# Voice Mode, Apple Watch Voice, and CarPlay

**What's here:** spoken conversations with the assistant in the web app, on iPhone and iPad, on
Apple Watch, and hands-free via Siri and CarPlay.

## How voice conversations work

Both the web app (**Voice** page) and the iOS app (**Voice** tab) hold live spoken conversations
with the assistant:

- **Always runs on the default assistant**, regardless of which profile was last selected in Chat.
- **Saved to Chat when the call ends:** The running transcript — what you said, what the assistant
  replied, and every tool call and result — is saved as a Chat conversation so you can pick it up in
  Chat where the call left off. (In the web app, if saving the transcript fails, an error is shown
  so you can copy anything you need before leaving the page.)
- **Status indicators:** On iOS and Apple Watch, the screen shows a microphone level meter,
  **Working…** while the assistant is running a tool or preparing its answer, and **Listening…**
  when it is ready for you to speak.
- **Quiet lookups:** When a question needs tool calls, the assistant says one brief acknowledgement
  ("let me check") and works quietly so you don't talk over each other. If a lookup runs long, it
  lets you know it is still working, and you can ask "are you still there?" without restarting the
  work.
- **Delegated tasks:** Quick delegated results are spoken in the live call. Longer delegations post
  their result directly into the saved Chat conversation when they finish (and can send a push
  notification) without interrupting the live call.

## Tool access and approvals in voice

A voice session starts with the assistant's everyday tools ready immediately. Any other tool your
profile allows — smart home scenes, image generation, scripts, automations — is loaded on demand via
`search_tools` followed by `call_tool` (which appears in the transcript and adds only a brief
pause).

**Actions that require confirmation cannot run in voice:** because a voice call cannot display an
approval prompt, the assistant will ask you to run confirmation-gated actions in Chat instead.

## Sending links and text to your phone ("send it to my Chat")

While talking on iOS or Apple Watch, ask the assistant to **send it to my Chat**. It saves the text
in that call's Chat conversation and sends a push notification to your registered devices:

- For an **HTTPS link** (such as map directions), tapping the notification opens the link directly,
  and the link also stays in the Chat conversation for later.
- Other handoffs open the conversation in Chat when tapped.

Push notifications must be enabled on the device (see
[Push notifications](interfaces.md#push-notifications)).

## Apple Watch

After completing one-time setup with your iPhone (see [ios.md](ios.md#apple-watch)):

- Tap **Start Voice**, allow microphone access, and speak. The watch shows microphone activity, the
  latest transcription, and **Working…** / **Listening…** status.
- Use **Mute** to pause the microphone or **End** to finish. Leaving the watch app ends the session,
  and the transcript (with any handoffs you asked for) is saved to Chat.
- **Watch face complication:** Edit your watch face, choose a circular, corner, inline, or
  rectangular complication slot, and select **Family Assistant Voice**. Tapping the microphone icon
  opens the app and starts voice mode immediately.

## Hands-free and in the car

Say **"Hey Siri, call Family Assistant"** to start a voice conversation without touching your phone.
It works with the app closed as long as you have signed in at least once since the phone was last
restarted.

- **Phone lock and CarPlay:** When the phone is locked, the assistant answers **only** when the
  phone is connected to **CarPlay** (an ordinary Bluetooth car connection does not count). Away from
  CarPlay, unlock the phone first; otherwise the call briefly appears and fails.
- **Call controls:** The conversation runs as a system call on the lock screen and CarPlay display.
  Mute or hang up with the standard call controls or from inside the app. An incoming phone call
  takes over the audio and ends the assistant session.

## Troubleshooting voice connections

Connection startup times out after 30 seconds rather than hanging indefinitely. If voice cannot
connect on your iPhone or Apple Watch, tell the assistant in Chat approximately when you tried and
which device you used. The app uploads connection-stage diagnostics automatically (excluding audio,
transcripts, instructions, tool arguments, and credentials); if upload fails while offline, reports
are queued and retried the next time you open voice mode or relaunch the app.
