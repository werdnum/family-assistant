# Conversation List Summaries

## Status

Implemented. Approach-level; construction detail lives in the code
(`src/family_assistant/conversation_summaries.py`).

## Problem

The web sidebar, the History page and the iOS conversation list labelled each conversation with the
first 100 characters of its latest message. The latest message is usually the least informative one
— "Thanks!", "Done.", the tail of a long answer — so the list did not say what a conversation was
about.

## Approach

**A generated one-line summary per conversation, shown in place of the preview.** The list API
returns `summary` beside `last_message`; clients show the summary where there is one and the latest
message otherwise. Keeping `last_message` means a conversation that has not been summarized yet, or
could not be, still reads as it did before.

**Summaries are scheduled from state, not from events.** A recurring sweep selects conversations
whose latest visible message is newer than the stored summary's watermark and that have been quiet
for a short settle period, and summarizes them. One query covers every way a conversation gains
messages — web and iOS turns, Telegram, email, delegated and scheduled replies — so there is no list
of producers to keep in step. The settle period keeps a turn in progress from being summarized
halfway and gives an active conversation one summary per pause rather than one per message.

**Cost is bounded by configuration, not by history.** The summarizer runs on a cheap model of its
own, reads only the opening and latest messages of a conversation with each one length-bounded, and
the sweep caps both how many it does per run and how far back it looks. The lookback also means a
first deployment does not summarize years of old conversations; those keep their latest-message
preview.

**A failed attempt advances the watermark.** Retrying on every sweep would spend a model call per
sweep on a conversation that cannot be summarized. The previous summary, if any, is kept; the next
message makes the conversation due again.

**A changed summary pings the owner's activity stream**, which is what open web and iOS lists
already refetch on, so a new label appears without a manual refresh.

## Security

The summarizer may read untrusted content (a forwarded email in the conversation) and sensitive data
(the conversation itself), so it holds no tools: it cannot change state or communicate. Its output
is shown only to the conversation's owner, through the ownership-filtered list endpoint, in the same
place the latest-message preview already showed that conversation's own content.

**A summary must never be fed back into a prompt.** It is derived from whatever the conversation
contained and carries none of that content's taint, so reading it into a model would launder
untrusted text into a trusted-looking channel. Anything that wants to use summaries as model input
has to carry taint through first.

## Deliberate simplifications

- Summaries are keyed by conversation and not regenerated when a message is edited or deleted
  without a newer one arriving. Rare, and the next message corrects it.
- A Telegram-only owner whose stored user id differs from their web identity gets no live activity
  ping for a changed summary; their list picks it up on its next fetch.
- There is no user-editable title. A summary that reads wrongly is replaced as soon as the
  conversation moves on; a manual rename can be added if the generated labels prove not to be
  enough.
