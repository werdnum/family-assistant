# Notes between a user's own conversations

## Status

Implemented: `post_to_conversation` (`src/family_assistant/tools/cross_conversation.py`).

## Problem

Results flow from a thread to Home only when the thread finishes (`finish_thread`'s echo), and
nothing at all flows into an arbitrary other conversation. A thread that learns something Home is
waiting for mid-way, or a Home turn that has news for one of its threads, has no way to hand it over
except asking the user to copy it. [One thread](one-thread-chat-ux.md) keeps conversations apart on
purpose, so the hand-over has to keep the guarantees that separation provides: whose words are
whose, and which profile sees what with how much taint.

## Approach

`post_to_conversation(target_conversation_id, message_content, attachment_ids?)` posts a note into
another web conversation the acting user owns and **wakes that conversation's assistant** to act on
it. `"home"` names the user's Home.

- **Who can be reached.** The target must be a known conversation (`send_message_to_user`'s
  `resolve_send_message_target`) whose every owner is the acting user, identities canonicalized.
  The current conversation is refused, as is anything not on the web surface, where Home and
  threads live. Other people are `send_message_to_user`'s job.
- **What crosses.** The note is an *assistant request* through the boundary module
  (`threads/boundary.py`): an internal, machine-authored user row in the target, framed by fixed
  text saying it came from another conversation. It is never stored as the user's words. Like any
  assistant request it is not subject to the context gate, since only the assistant's own text
  crosses.
- **Which profile wakes.** The profile the user talks to in the target (its latest user row, or the
  thread's own profile before anyone replied). History is profile-scoped, so this is also the only
  profile whose turns would see the note. Waking it hands it this turn's work, so it is admitted as
  a delegation to that profile is (`allowed_delegation_sources`), the tool carries the `delegation`
  tag and is classified as a delegation sink, and a profile that may not wake the LLM cannot post.
- **How it wakes.** The run machinery a thread's first turn uses: a delegation run of kind `thread`
  — one turn at the top level of the target, which wakes nobody when it ends — whose seed is the
  note. On the web it runs on the thread surface, so the user watches it stream in that
  conversation, and it waits for a turn already running there.
- **Taint.** The woken turn starts with the posting turn's taint, as a delegated turn does. Origin
  is turn-relative: what the posting turn inherited from its own window is not in the target's, and
  the target's later turns take from history only what a row introduced. So the boundary writes
  every assistant request with the writer's whole taint marked introduced, and the target stays
  tainted for as long as the note is in its window. This also fixes the same laundering for a
  thread's seed request from `start_thread`, which went through the same path.

## Deliberate simplifications

- **No hop limit or loop guard.** A woken assistant can post back, and two conversations could in
  principle keep waking each other. That needs the assistants to keep choosing to call the tool,
  each wake is a visible turn in the user's own conversation, and each carries the accumulated
  taint, so we accept it rather than add hop counters or rate limits.
- **Per-target delegation rules do not apply.** Those rules match a `target_service_id` argument.
  This tool has none: it wakes the profile the user already talks to in a conversation they own.
  The target profile's own `allowed_delegation_sources` still applies.
- **Web only.** Telegram conversations are refused rather than given a second wake path.
- **No reply routing.** The woken turn replies in its own conversation. The source learns nothing
  back unless the woken assistant posts a note of its own.
