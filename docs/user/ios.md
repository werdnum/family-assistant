# iOS App, Apple Watch, and Siri

**What's here:** using the native iOS app on iPhone and iPad, setting up Apple Watch, and running
Family Assistant actions from Siri, the Share Sheet, and the Shortcuts app.

For spoken conversations and CarPlay, see [voice.md](voice.md). For how **Home** and **threads**
work, see [threads.md](threads.md).

## iOS app

The native app signs you in securely and opens on five tabs: **Chat**, **Voice**, **Notes**,
**Documents**, and **More**. Chat, Voice, and Notes are native screens; Documents and everything
under More open the corresponding web pages in-app using your iOS sign-in (including away from your
home network). Each tab remembers where you were.

**Links open in the app.** With the app installed, tapping a link to an assistant page — in a
message, email, notification, or a shared conversation link — opens it in the app on the matching
tab rather than in Safari. Links inside chat replies do the same. On iPhone or iPad, shared
conversation links from `assistant.andrewgarrett.dev` open in a native read-only transcript view
(pull down to refresh). To view a link in Safari instead, long-press it and choose **Open in
Safari**.

### Chat

Chat shares the same conversation history as the web app:

- Streams replies live, with **Stop** and **Steer** controls while a reply is running (see
  [interfaces.md](interfaces.md#chat)).
- Switches profiles: picking a profile starts a fresh conversation carrying over any draft text and
  attachments (on an empty chat it switches in place; reopening an older chat resumes its original
  profile).
- Picks an [intelligence level](intelligence-levels.md) via the gauge button in the toolbar where
  the profile supports it.
- Renders Markdown, collapsible tool call groups with status indicators, and inline images. Very
  long messages render a section at a time behind **Show more**.
- Rate finished replies with thumbs up/down (with an optional feedback box on thumbs down), and tap
  the eye button (**Which model answered**) to see the profile, tier, model, provider, and any
  experiment details.
- Handles approve/reject confirmations in-app or directly from lock-screen notifications.
- Where threads are enabled, Chat opens on **Home** with full thread support (**New Thread**, **Send
  To** on long-pressing Send, **Continue in Thread**, **Move to Thread**, **Mark Done**, and **Share
  Message** / **Share Thread**); see [threads.md](threads.md).

### Getting content in

- **Share Sheet / Files:** Share a supported file (images, PDFs, plain text, Markdown, or other
  files up to 100 MB; 20 MB for media read directly by a model) from the iOS Share Sheet or Files
  "Open In" to open a new Chat draft (or Home's composer when threads are on) with the file
  attached.
- **Photos and clipboard:** The Photos share sheet does not list Family Assistant — copy the photo
  and tap the paste button beside the composer, use the composer's photo picker, or share from
  Files.
- **Home Screen quick actions:** Long-pressing the app icon offers **New Chat** and **Voice**.

### Notes, More, and notifications

- **Notes:** Search, read, create, edit, and delete notes, and toggle whether a note is included in
  the assistant's context automatically.
- **More:** Access Events, History, Automations, Tools, and **Settings** (notification controls and
  sign-out).
- **Push notifications:** Off until enabled under **More → Settings → Enable Notifications**. See
  [Push notifications](interfaces.md#push-notifications).

### Connection indicator and troubleshooting

A small indicator in the Chat toolbar shows the live-update state and offers a one-tap fix:
*degraded* (updates lagging — tap to reconnect), *offline* (tap to reconnect), or *sign-in required*
(tap to sign in). Background sync issues appear there or as a "Couldn't refresh — last updated …"
banner at the top of the conversation list rather than pop-up alerts, and the list refreshes
automatically when the app returns to the foreground. A failed message send shows a safe **Retry**
button that never sends twice.

If Chat reports **"authentication wall detected"**, open your Family Assistant web address in
Safari, complete the sign-in or network access page, then return to the app and tap the connection
indicator to reconnect.

## Apple Watch

Install Family Assistant from the Watch app on your iPhone (watchOS 10 or later). Open the watch app
and tap **Set up with iPhone** while your paired iPhone is nearby and signed in. There is no server
address or password to enter on the watch; if setup cannot connect, open Family Assistant on the
iPhone first and try again.

After setup, the watch works over its own Wi-Fi or cellular connection when your iPhone is away, or
via the paired iPhone when nearby. If sign-in expires, tap **Set up with iPhone** again. Signing out
or changing accounts on the phone clears the watch's sign-in when the devices next exchange updates;
you can also revoke the watch's credentials in **Settings → API Tokens**.

For using voice mode and watch face complications on Apple Watch, see
[voice.md](voice.md#apple-watch).

## Siri and Shortcuts

Once signed in, the iOS app provides actions in Siri and the **Shortcuts** app with no extra setup:

- **"Ask Family Assistant…"** — ask a question and get the reply inline (requires unlocking the
  phone unless connected to CarPlay). **Continue in app** opens the full conversation when an action
  needs approval.
- **"Capture this in Family Assistant"** — send text or a link for the assistant to save and file.
  Add it to a Shortcut with *Show in Share Sheet* enabled to capture from Safari, Notes, and other
  apps. Captured pages and emails are untrusted external content, so the assistant runs in a
  restricted mode: it can read your notes to file things sensibly, but asks for approval before
  making changes.
- **"Add a note to Family Assistant"** — create a note by title and content.
- **"Open Family Assistant chat"** — open the Chat tab, optionally starting a conversation with a
  message.
- **"Call Family Assistant"** — start a spoken voice session handled as a system call, including
  hands-free on CarPlay. See [voice.md](voice.md#hands-free-and-in-the-car).
