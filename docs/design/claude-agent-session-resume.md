# Claude Agent Session Resume

## Context

The `claude` profile runs each turn as a fresh Claude Code session, with earlier conversation
rendered into the opening message as a transcript (see
[claude-agent-sdk-profile.md](claude-agent-sdk-profile.md), "Conversation history is rendered, not
resumed"). That is the right default for a new request. It is a poor fit for a **delegation wake**:
the profile was in the middle of a task, handed work to a background delegation, and is woken when
the work finishes. The turn boundary there is an artefact of the hand-off, not a new request, and
rebuilding loses what the model had: its own reasoning, the raw tool results it worked from, and the
structure of the calls it made, all flattened into a text transcript.

The goal is **fidelity**: a woken session continues the session that delegated, as though the
delegate call had simply taken a long time. Cost is secondary; measured, a resumed session reads its
whole prefix from the prompt cache (about 28k cached tokens read, about 100 written, per resumed
call in a probe), so resume is no more expensive than rebuilding and usually cheaper.

## Decision

### Family Assistant's history stays the authority

The saved message history remains the record of the conversation. A stored Claude Code session is a
**fidelity cache** over it: used only when it provably agrees with the history, and otherwise
ignored in favour of today's rebuild. The two ways to be wrong are not symmetric. Rebuilding when
the session could have been resumed only costs fidelity. Resuming a session that is incomplete,
altered, or from another conversation is a correctness failure: the model acts, tools included, on a
past that did not happen. Every rule below therefore requires positive evidence to resume, and the
verification in the milestones is aimed at ruling out that second kind of error.

A wake resumes the session of the turn that started its delegations when all of these hold, and
rebuilds otherwise:

- The session is **complete**: its turn ended cleanly and every transcript batch reached the store
  (see below).
- **Nothing has happened in the conversation since**, other than the rows the wake itself adds (the
  child results and the wake notice). A user message, another profile's turn, an edit or a
  compaction in between means the session no longer agrees with the history.
- The session ran with the **same profile, model, effort and system prompt** the wake would use. The
  system prompt is stable across turns except for the household context; when that has changed, the
  cached prefix would be invalid from its first token anyway.
- The **taint epoch is unchanged** since the session ran (see Taint).

A resumed session opens with the rows the wake adds — the child results as lower-priority data, the
wake notice, and the wake instruction — in place of the rendered transcript.

### The session store is ours

The Agent SDK mirrors a session's transcript to a pluggable `SessionStore` and, on `resume`,
materialises the session from that store into a temporary config directory before starting the CLI.
Family Assistant implements the store over its own database, so a session survives restarts and
redeploys, and nothing durable is left on the container's disk: the CLI's own local copy goes to a
per-session temporary config directory, removed when the session ends.

`--no-session-persistence`, passed today, suppresses mirroring entirely (verified: the store
receives nothing), so it is replaced by the temporary config directory as the means of keeping the
conversation off disk.

### Round-tripping precisely is the central risk

A resumed session is only as good as the transcript it is rebuilt from, and a transcript that is
subtly wrong (reordered, truncated, an entry altered) resumes into a session that is confidently
mistaken about its own past. Three separate properties are required, and each has its own evidence:

1. **The adapter returns exactly what it was given.** `load` returns entries deep-equal to those
   appended, in append order, across separate `append` calls. Entries are stored as their serialised
   JSON text, not as a JSON column: the SDK's contract is only deep-equality, but PostgreSQL's
   `JSONB` rejects a `\u0000` escape outright, and text storage gives the same exact round trip on
   SQLite and PostgreSQL.
2. **The store holds the whole session.** The SDK's mirror is best-effort: a batch that fails three
   times is dropped and reported as a `MirrorErrorMessage`, and the session carries on. A session is
   therefore resumable only with positive evidence that its stored transcript is exactly what was
   mirrored: its turn ended cleanly, the final flush completed, and **every append for it succeeded
   the first time**. A failed append disqualifies the session even if the SDK's retry then succeeds,
   because an append that committed but reported failure is retried in full, and entries without a
   `uuid` cannot be told apart from their duplicates. Retries are rare, so a rebuild is the right
   answer for them, not deduplication machinery.
3. **Resuming from our store is the same as resuming from the CLI's own file.** The CLI does not
   mirror everything it writes locally: request diagnostics and cost records (`api-request`,
   `api-request-blob`, `api-request-shape`, `cost-state`) stay local, while every conversational
   entry is mirrored. A probe found resume from the store recalls the earlier conversation and reads
   the prompt cache identically to resume from disk; this is kept as a test rather than assumed.

### Taint

Today a turn's taint comes from the rows rendered into its prompt. A resumed session carries more
than that: everything its original turn saw, including tool results that may since have left the
history window. The session therefore records the turn's final taint state when it completes, and a
resumed wake starts from the union of that state and the taint of the rows the wake adds — never
less than the rebuild path would compute. The operator's taint epoch exists to invalidate taint
judgements on old history; a session that ran under an earlier epoch is not resumed.

## Deliberate simplifications

- **Only delegation wakes resume.** An ordinary follow-up from the user still starts a fresh session
  with the rendered transcript. Extending resume to follow-ups would make the stored session a
  second long-lived copy of the conversation, subject to windowing and compaction it does not share;
  that is a separate decision, to be taken with evidence from wakes.
- **Divergence is not reconciled.** If anything happened in the conversation since the session, the
  wake rebuilds; it does not try to append the difference to the stored session.
- **No subagent transcripts.** The session has no subagents, so the store need not enumerate subpath
  keys for resume. It still stores subpath keys correctly, because the SDK's conformance suite
  requires it.
- **Retention is by age.** Stored sessions are deleted after a fixed age. A wake that arrives after
  its session has been deleted rebuilds, as any ineligible wake does.

## Milestones

1. **A session store that round-trips.** The database-backed adapter and its schema, with no change
   in behaviour. Verified by the SDK's own `run_session_store_conformance` suite on SQLite and
   PostgreSQL; property tests over arbitrary JSON entries (Unicode outside the BMP, `\u0000`
   escapes, large and deeply nested values, extreme numbers) asserting deep-equal round trips on
   both backends; and a corpus of transcripts captured from real Claude Code sessions, loaded back
   deep-equal.
2. **Sessions are mirrored and their completeness recorded.** Turns on the `claude` profile mirror
   to the store with the CLI's local copy in a temporary config directory, and each session records
   its scope, the last history row it covers, its final taint state and whether it is complete.
   Verified by tests that a clean session is marked complete, that a failed append, a mirror error
   or an interrupted turn leaves it incomplete, that nothing is left under the container's Claude
   config directory, and by a real session whose stored transcript loads back deep-equal to what was
   mirrored.
3. **Wakes resume.** The eligibility rules above, the resumed opening message and the taint
   inheritance. Verified by tests that each ineligibility rebuilds; that a resumed wake's taint is
   at least the rebuild path's; a test with a real Claude Code session that resuming from the store
   and from the CLI's local file produce the same first resumed request and the same cache reads;
   and an end-to-end run of a real background delegation on the `claude` profile that resumes and
   answers from its pre-delegation context.

## Not in this design

- Resuming ordinary follow-up turns (see Deliberate simplifications).
- Running delegated profiles inside the Claude Code session. Delegation to another profile goes
  through Family Assistant so the target profile's own tool policy applies.
