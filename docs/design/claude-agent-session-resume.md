# Claude Agent Session Resume

## Context

A turn on a `claude_code` tier runs as a fresh Claude Code session, with earlier conversation
rendered into the opening message as a transcript (see
[claude-agent-sdk-profile.md](claude-agent-sdk-profile.md)). That suits a new request. It suits a
**delegation wake** badly: the profile was mid-task, handed work to a background delegation, and is
woken when it finishes. The turn boundary is an artefact of the hand-off, and rebuilding flattens
the model's own reasoning, the raw tool results it worked from and the structure of its calls into
text.

## Decision

A wake resumes the Claude Code session of the turn it continues, using Claude Code's own session
resume. Family Assistant's message history stays the record of the conversation; the stored session
is used only when it agrees with that history, and the wake rebuilds as before otherwise.

### Sessions are stored in the database, as opaque transcript entries

The Agent SDK mirrors a session's transcript to a pluggable `SessionStore`, and on `resume`
materialises it from the store into a temporary config directory before starting the CLI. Family
Assistant implements that store over its own database, in tables of its own, so a session survives a
restart or redeploy between the delegation and its wake. Entries are Claude Code's own transcript
lines, kept as opaque JSON text in append order and never interpreted; the CLI's resume code is the
only reader. Besides the conversation, they carry what Claude Code records about the session: the
subscription account's email address and account and organisation ids among it. That is the same
material Claude Code writes to its own local files, now held with the rest of the household's data.

The CLI's local copy goes to the same place it does today, the session's working directory under the
CLI's config directory, and is removed when the session ends, so transcripts are not left on the
container's disk. `--no-session-persistence` suppresses mirroring entirely, so it is dropped. The
working directory becomes a fixed, empty directory rather than a fresh one per turn, because the
store key the SDK derives from it has to match when the session is resumed.

### When a wake resumes

A wake resumes the latest stored session for its conversation and profile when:

- **The session is whole.** It ended cleanly and every write of its transcript to the store
  succeeded. The SDK drops a batch it cannot write after three attempts and the session carries on;
  a retried batch can repeat entries. Either way the session is not resumed.
- **Nothing has happened since.** The newest history row before the wake's own rows belongs to the
  session's last turn. A user message, another turn or anything else in between means the session
  does not know the conversation as it is.
- **The setup is unchanged.** Same profile, model, effort and system prompt.

A resumed session opens with the wake's own rows (the child results and the wake notice) followed by
the wake instruction, in place of the rendered transcript.

### Taint

A resumed session holds everything its earlier turns saw, including tool results that may since have
left the history window. A turn on the profile records its final taint state with the session, and a
resumed wake starts from the union of that and the taint of the rows it renders, which is never less
than a rebuild would start from.

## Deliberate simplifications

- **Only wakes resume.** An ordinary follow-up starts a fresh session with the rendered transcript,
  so the stored session never becomes a long-lived second copy of the conversation.
- **Divergence is not reconciled.** Anything in between means rebuild, not appending the difference.
- **Edits to earlier history are not detected.** The check is on what has been added since the
  session; a row edited or deleted in the meantime is not noticed.
- **Retention is by age.** Stored sessions older than a fixed limit are deleted as new ones are
  recorded; a wake whose session has gone rebuilds.

## Verification

- The SDK's own `run_session_store_conformance` suite against the database store, on SQLite and
  PostgreSQL, and a transcript captured from a real session stored and loaded back unchanged.
- Loop tests for each ineligibility rebuilding, for a resumed wake's opening message and taint, and
  that a failed store write leaves the session unresumable.
- A real Claude Code session that delegates, is resumed from the database store, and answers from
  what it knew before the delegation.
