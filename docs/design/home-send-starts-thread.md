# Sending in Home starts a thread

## Status

Approved direction; revises [one-thread-chat-ux.md](one-thread-chat-ux.md), where this doc wins.

## Problem

Home's composer asks the user to route every message before sending it: send "directly", or send as
a thread. That is the choice Home was built to remove, and nothing tells the user what the
difference is.

Fourteen days of production use (one person, one Home measured):

- The model called `start_thread` **once**. Routing work into threads was meant to be its job.
- `delegate_to_service` ran once from Home and 34 times from threads, so work already lives in
  threads.
- The user started 29 threads against about 30 standalone Home messages, 17 of them on an assistant
  other than the default.
- Of 39 Home messages, 9 were follow-ups within ten minutes.
- Home held 49 scheduled turns against those 39 messages. It reads as a feed more than a chat.
- 4 of 31 proactive messages drew a reply within an hour, all on topic.
- Half of the 12 user-started `default_assistant` threads are still open, and 9 of them had one or
  two user messages.

## The experience

**Send in Home starts a thread on the default assistant.** One rule, which needs one sentence to
explain. The thread opens as it does today, so follow-ups happen there. A control beside Send picks
another assistant for that message; it chooses who, never whether.

**Home is the feed.** Proactive messages, thread cards and outcomes live there. Every thread the
user starts leaves a card at the point it was started, so Home shows the whole story.

**A thread can look back at Home.** A reply to a reminder lands in a thread that cannot otherwise
see the reminder, so a thread's assistant can read recent Home messages when the user refers to
something it doesn't have. It reads on demand rather than being seeded, so an unrelated send carries
nothing along.

**A thread you have read stops asking.** "Waiting" is a catch-all, and threading every message would
otherwise fill the count. A waiting thread with nothing unread is listed under Quiet at once, not
after three days.

**Nothing else changes.** "Continue in Thread", "Move to Thread" and "New Thread" stay. Telegram's
private chat stays a single stream, and threads being off leaves Home out of it altogether.

## Design

**The marker follows the moved-turn precedent.** Starting a thread writes a Home row naming it, in
one transaction with the thread, and clients draw the card from the row. A row rather than a
client-side merge of the thread list, so a card keeps its place in history, pages with it and syncs
like any other row. A synthetic `start_thread` tool call would have reused the existing card, but it
would record the model doing something the user did, which the authorship rule exists to prevent.
The row holds fixed text, so it carries no taint, and is not counted as unread: the user just did
it. It has no profile, so no model's history window reads it; Home's assistant learns of threads
from `list_threads`, and a line per message would be paid for on every Home turn.

**Reading Home passes the context gate.** Home turns may enter only a profile that receives
household context, so the tool is refused elsewhere, and is granted to `default_assistant` and
`complex_tasks`. Each row read brings its taint into the turn. It reads what a reader of Home sees:
user and assistant messages, not tool rows, thread cards or internal rows.

**The client decides, in one place on each client.** Home's send path already has a thread branch;
it becomes the only branch. The server's turn endpoint keeps accepting Home turns, because a client
older than this change still sends them, and wakes run there regardless.

## Deliberate simplifications

- **A running Home turn is still steered.** While a morning brief or other wake runs in Home, the
  composer steers it as it does today, rather than starting a thread. It is short and rare, and
  steering it is not wrong.
- **Quick questions cost a navigation.** That is the price of the single rule, and the data says
  most new topics were being threaded by hand anyway.
- **A question you have read can go quiet.** It leaves the waiting count but stays in the list. The
  alternative is a count that means nothing.
- **Outcomes can appear twice.** A finished thread shows on its card and in the echo below it, as
  assistant-started threads do today. Worth revisiting once Home has run this way for a while.
- **`start_thread` stays.** Scheduled Home turns can still reach it, and the Telegram Home streams.
- **Older clients keep sending Home turns.** Their messages behave as they always have.
- **An attachment needs a message with it.** The server takes attachments only with an opening
  message, so a file sent alone is refused and stays in the composer. A thread titled from nothing
  would be worse.

## Work plan

1. **Backend.** Marker row and its API field, immediate Quiet, and the Home-reading tool. Verified
   by functional tests: a start writes one marker, a retry writes none, a refused start leaves none,
   the marker is neither unread nor carried by "Continue in Thread", a read waiting thread is quiet,
   and the tool refuses outside a thread, refuses a no-context profile and merges taint.
2. **Web.** Send starts a thread, cards draw from markers, the menu picks an assistant. Verified by
   component tests and a Playwright flow.
3. **iOS.** The same. Verified by unit and UI tests.
4. **Docs.** User guide and design status, alongside each milestone.
