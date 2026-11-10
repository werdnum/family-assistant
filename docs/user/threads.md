# Home and Threads

**What's here:** how **Home** and **threads** organize your conversations in the web and iOS apps,
how to start, continue, move, share, or close threads, and how unread counts and cross-conversation
notes work.

## How Home and threads work

When threads are turned on for your household, Chat opens on **Home** in both the web app and the
iOS app (otherwise Chat works as a simple list of separate conversations; see
[interfaces.md](interfaces.md)).

**Home** shows what's going on: reminders, your morning brief, automation results, and a card for
each thread. Type in Home's message box and press Send to start a **thread** with the main
assistant. The thread opens and the assistant answers there, so carry on the conversation in the
thread. Files you attach in Home go with the message (a file needs a message with it), and so does
the intelligence level you picked if that assistant offers it (in Home on iOS, a picked intelligence
level applies to that one message).

A thread starts without seeing the rest of Home, but the assistant can look back at Home's latest
messages when you mention something it can't see, so *"snooze that"* after a reminder works. A
thread keeps its assistant for as long as it runs, so its profile picker is locked.

## Thread cards and states

Every thread has a card in Home where it was started. The card updates in place rather than adding a
new message for every step, showing:

- **State** (coloured dot): **Working**, **Waiting on you**, **Done**, or **Failed**
- **Title** and which assistant is running it
- **Latest message**, such as a question it needs you to answer
- **Unread message count**

When the thread's assistant finishes the job, it marks the thread **Done** (greyed out with a tick
and the outcome) or **Failed** (outlined in red). Its final message — the outcome — also appears in
Home under a **From thread:** label naming the thread; click or tap that label to open the thread. A
thread that ends on a question is **Waiting on you**, not done. If you close a thread yourself, the
card says **You marked this done** instead.

A finished thread reopens automatically when something new arrives in it (your reply, a reminder it
set, or a late result), returning to **Working** or **Waiting on you**. When it finishes again, the
new outcome appears in Home.

To catch up without opening each thread, ask in Home — for example, *"How are the quotes going?"*
The assistant answers from each thread's title, state, and reported outcome (it does not read the
threads' full transcripts; open the thread for details).

## Opening, navigating, and closing threads

- **Web app:** Click a thread card, sidebar entry, or **From thread:** line to open it. On a wide
  screen the thread opens in a right-hand panel while Home stays in the middle with any draft you
  were typing intact; you can chat in either. Use the panel's buttons to expand the thread to fill
  the middle (**← Home** returns to Home; the collapse button restores the panel) or close it. On a
  narrower window or phone, a thread opens in place of Home with **← Home** to go back. The URL
  tracks the open thread across reloads and browser back/forward. Conversations under **Earlier**
  open in the middle, not the panel.
- **iOS app:** Tap a card or **From thread:** label to open the thread; Back returns to Home. While
  one or more threads are working, a bar at the top of Home shows them — tap it to open the thread
  (or the chat list when several are running).

Each open thread shows a bar at the top with the request that started it and its current state
(**Working…** while busy).

**Closing a thread yourself:**

- **Web:** Select **Mark done** in the bar above the thread (available while **Waiting on you** or
  **Failed**, or once a working thread stops).
- **iOS:** Choose **Mark Done** from the **…** menu at the top of the thread, long-press its card in
  Home, or swipe the thread left in the chat list (available while **Waiting on you**, **Quiet**, or
  **Failed**).

Closing a thread yourself does not post an outcome to Home. If a thread cannot be marked done, the
bar or an alert says why.

## Starting, continuing, and moving exchanges

Besides sending a message directly in Home, you can direct work into threads in several ways (where
the chosen assistant offers intelligence levels, you can pick one alongside the assistant):

- **Pick a different assistant from Home:**
  - *Web:* Use the arrow button beside Send in Home.
  - *iOS:* Long-press the Send button (**Send To**).
- **New thread:**
  - *Web:* Select **New thread** in the sidebar under Home, choose an assistant, and write the
    opening message.
  - *iOS:* Tap the pencil icon (**New Thread**) at the top of Home or the chat list.
- **Continue in a thread:**
  - *Web:* Point at a reply in Home and select **Continue in a thread**. With **Bring the
    conversation along** ticked, the dialog lists that exchange and up to four preceding Home
    messages (related ones are pre-ticked so unrelated reminders or briefings are left behind; tick
    or untick any before starting).
  - *iOS:* Long-press a message in Home and choose **Continue in Thread** (with **Bring this
    conversation** on, it copies that exchange and up to four before it).
  - Adding a new opening message is optional when bringing context along. Assistants that do not
    receive household context (such as the browser profile) start from your opening message alone,
    and the dialog/sheet says so. Either way, the thread's top bar quotes the Home reply it started
    from, and Home keeps the original exchange.
- **Move to thread:**
  - *Web:* Select **Move to thread…** beside **Continue in a thread** on a Home reply.
  - *iOS:* Long-press a message in Home and choose **Move to Thread**.
  - Moves the whole exchange (your message, tool calls, and reply) into an existing thread, leaving
    a clickable **Moved to …** link in Home. Only exchanges you started can move (not thread
    outcomes or reminders), nothing can be mid-reply, and target threads that are busy or whose
    assistant does not receive household context are listed greyed out with the reason.

If a thread cannot be started or an exchange cannot be moved, the reason is shown and your draft is
preserved.

## Passing something to another conversation

Ask the assistant in one conversation to tell another of your conversations something — for example,
in a thread, *"Tell Home the quotes are in"*, or in Home, *"Let the plumbing thread know I prefer
Acme"*. The assistant posts a note there and wakes the assistant in that target conversation to
reply directly. This only reaches your own conversations (to message another person, see
[Messaging other people in your household](interfaces.md#messaging-other-people-in-your-household)).
The note carries the trust level of what the sending assistant had read, so external content is
treated with the same care in the target conversation.

## Sharing from Home

Share a specific piece of Home rather than all of it:

- **Share a thread:** Select the share icon on the thread's card in Home or in the thread header (on
  iOS, long-press the card and choose **Share Thread**, or use the **⋯** menu inside the thread).
  Anyone with the link who is an authorized household user can read the whole thread, including
  messages added later.
- **Share a single message:** Select **Share message** on a reply in Home (on iOS, long-press the
  message and choose **Share Message**). The link shows only that reply and its attachments — not
  your prompt, the rest of Home, or the tools used. Moving that exchange to a thread invalidates its
  message link.

Open the share action again to replace the link or stop sharing. The share icon in Home's own header
shares all of Home (including future messages) and warns you before creating a link. See
[Share a conversation](interfaces.md#chat) for link permissions and how iOS opens shared links.

## Unread counts, sidebar groups, and search

Opening Home or a thread marks visible messages as read once you have scrolled to the newest message
with the window or app in the foreground (on wide web screens, both Home and a side-panel thread
count as visible; Home hidden behind an expanded thread or in a background tab does not). Unread
counts are per user.

Both the web sidebar and the iOS **Chats** list (whose badge counts threads waiting on you) list
**Home** first, followed by threads grouped as **Working**, **Waiting on you**, **Failed**,
**Quiet** (threads waiting on you that you have already read, until something new arrives), and
**Done**, then pre-thread or standalone conversations under **Earlier**. Search matches prefix words
anywhere in a conversation and shows a matching snippet.
