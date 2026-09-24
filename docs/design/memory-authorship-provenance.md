# Memory Provenance by Authorship

## Status

Proposed. Amends [conversation-memory.md](conversation-memory.md): builds its first future-hardening
item, "per-evidence provenance for the taint skip", and changes what the memory write floor is
checked against. Companion to [runtime-taint-machinery.md](runtime-taint-machinery.md), whose sink
policy this design leaves untouched.

## Problem

Memory does not work in production. Nearly every review stretch is skipped for external taint, and a
foreground "remember this" is refused in most turns. The cause is not that the household talks about
outside content all the time; it is that memory asks the wrong provenance question.

The sink gates need to know **what could have steered this turn**, and turn taint answers that. A
turn that has read an email must not send a message on the email's say-so, and anything the model
writes afterwards may carry the email's instructions. Memory needs to know **who said this**. The
household's own words are the household's whatever the turn around them read.

Three properties of the current machinery make turn taint an unusable answer to the memory question:

- **Every row carries the whole turn's state, history included.** A user message is stamped with the
  merged taint of the prompt history and context it arrived into, not with who wrote it. A stretch
  with no tool calls at all is skipped because the person's first message inherited taint from
  earlier turns.
- **A conversation never heals.** Each new row re-bakes the merged state it saw, so one web search
  in a Telegram chat, which is a single endless conversation, stamps every later row
  `unknown_external`.
- **Everyday reads are untrusted.** Document search, calendar search (subscribed calendars are
  externally authored), mail, maps, transit, shopping and web tools all resolve above
  `machine_reviewed`, and so does listing notes once any note carries external provenance. That is
  correct for sinks. For memory it means that any stretch doing useful work is excluded.

Retagging tools does not help memory: `recognized_machine` and `known_contact` are also above the
reuse boundary, and most of those tools really do return text the household did not write.

## Requirement

**Memory holds what the household said, never what outside content said.** The v1 provenance
invariant stands. What changes is the evidence it is judged on: the provenance of the rows the
curator actually reads, rather than the turn state those rows were written under.

Sink policy is unchanged. Turn taint, history carry-over and every sink gate behave exactly as
today, so nothing here adds friction to switching `taint_policy.mode` to `enforce`.

## Design

### A person's message records its author's provenance

A user row's provenance is the provenance of what its sender contributed: the trigger's own sources
(an authenticated household member, a forwarded email, a delegation's caller, an A2A peer), not the
history or ambient context the turn was assembled from. Rows the turn writes after that (assistant
replies, tool calls and results) keep the full turn snapshot they carry today.

Sink enforcement loses nothing. The next turn's taint is still seeded from the history window, and
the assistant and tool rows in that window carry the state that the user row no longer repeats. The
user row was never the only carrier of that state; it was a duplicate.

### The curator reads only admissible rows, and its taint is theirs

The review task renders a stretch's rows as it does today, with one filter: **a row whose own
provenance is not admissible for reuse is left out of the transcript.** In practice that keeps every
household message and drops the assistant replies of turns that read outside content. The curator's
taint tracker is seeded from the rows it was shown, so a review whose transcript is all household
words runs at the trusted pole and may write. The evidence a review may cite is the same set: only
rendered rows are acceptable citations, while the watermark still advances over the whole stretch.

The whole-stretch skip survives as the degenerate case: a stretch in which no person's message is
admissible (an email-intake conversation, say) has nothing to curate and is skipped and audited as
it is now.

The curator prompt says that assistant turns may be missing from the transcript, so a reply such as
"yes, the second one" has no referent and records nothing.

### Foreground "remember" defers to review in a tainted turn

The notes-repository write floor keeps checking the writing turn's taint: a foreground assistant
that has read an email is exactly the writer the floor exists to stop. What changes is the refusal.
In a turn above the reuse boundary where this conversation will be reviewed (contribution is on for
the profile and the interface), a memory write returns a result telling the model that memory is
written from the household's own words at the next review, so it can tell the person their request
will be picked up rather than report an error. Where no review will run, the refusal stays, so a
request is never reported as deferred to a review that will not happen. The person's "remember that
Teija likes…" is then in a user row the curator can read.

## Deliberate simplifications

- **What the assistant said in a tainted turn is not curated.** Its replies may paraphrase injected
  text, and telling a paraphrase from an original is what the reuse boundary refuses to guess. The
  cost is context: the person's side of an exchange about a hotel search survives, the assistant's
  summary of the hotels does not. A preference the person states is kept; a choice they make by
  reference ("book that one") is not.
- **A person may repeat outside content in their own words, and it will be remembered.** Someone who
  types an email's contents into the chat has authored that message. That is the same trust the
  household already has when it asks the assistant to do anything.
- **Conversation taint still never decays.** Re-baking history into every row keeps chats tainted
  indefinitely for sink purposes, and that is a real obstacle to enforcement. It is a separate
  change with its own trade-off (a decaying window lets a paraphrase outlive its source) and is not
  needed for memory once memory reads authorship.
- **Rows written before this change keep their re-baked stamps.** Their user rows still read as
  tainted, so old stretches stay skipped. Memory review only ever looks at rows written after
  contribution was enabled, and new rows are stamped correctly from the release on.

## Work plan

1. **User rows carry authorship provenance.** Every interface that persists a person's message
   stamps it with the trigger's own sources. Verified by tests that a user row written into a
   tainted conversation reads back at the sender's tier, and that the following turn's seeded taint
   is unchanged.
2. **Review filters by row provenance.** The transcript excludes inadmissible rows, the curator is
   seeded from what it was shown, and the skip counters record excluded rows alongside skipped
   stretches. Verified by the hotel example: in a stretch where a person states a preference and the
   assistant then searches the web, the preference is curated and the search results are not.
3. **Foreground deferral.** A memory write in a tainted turn returns the deferral result, and the
   system prompt says what it means. Verified by a functional test in which a turn that has read an
   untrusted tool result is asked to remember something and the later review stores it.
