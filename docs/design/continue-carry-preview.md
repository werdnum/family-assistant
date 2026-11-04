# What "Continue in a thread" brings along

## Status

Implemented. Extends [home-send-starts-thread.md](home-send-starts-thread.md) and
[one-thread-chat-ux.md](one-thread-chat-ux.md), "Starting a thread".

## Problem

"Bring the conversation along" copied the anchor's turn and the four Home turns before it, chosen by
position alone, and the dialog said only that. Since sending in Home starts a thread, Home holds
almost nothing but assistant-started turns (briefs, reminders, wakes) and the echoes of finished
threads, so the four turns before a message are usually unrelated to it. The option always did
something, but what it did was invisible and mostly noise, except when the message being continued
happened to sit after related proactive messages.

## Design

**The dialog previews the candidates.** A read-only endpoint returns the same window the start would
copy, oldest first, each turn with the user's text (if they wrote any) and its final reply,
shortened. The dialog lists them with a tick each; the anchor's turn is always ticked.

**Jev picks the starting ticks.** Compaction already asks Jev, per candidate turn, whether a new
message continues it (`processing/history_relevance.py`). The preview asks the same question with
the anchor's exchange as the message and the earlier turns as candidates. The user sees and edits
the result, so it is active by default; a timeout, an error, `shadow` or no Jev at all ticks
everything, which is what the option did before.

**The start names the turns.** The start request takes the ticked turn ids. The server recomputes
the window and refuses a pick that drops the anchor or names a turn outside it, so the window stays
the only thing that decides what may be carried; omitting the pick copies the whole window, which
keeps older clients (iOS) working unchanged.

## Deliberate simplifications

- **The window is unchanged.** Jev narrows the five-turn window; it does not reach further back. A
  related exchange six turns up still needs "Move to thread" or the thread's own Home lookup.
- **The preview is fetched once per dialog.** Typing an opening message does not re-ask Jev; the
  anchor alone decides the suggestion.
- **Untrusted text reaches Jev's state.** Home turns can carry tainted content, which can move every
  answer in the request. The answers only set ticks the user reviews, so the worst case is a wrong
  default.
- **iOS keeps the old behaviour** until it adopts the preview.
