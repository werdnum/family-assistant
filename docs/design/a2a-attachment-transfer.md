# A2A Attachment Transfer

## Problem

The A2A layer converted attachments into parts a peer cannot use.

- **Outbound.** An `attachment` content part became `FilePart(FileWithUri(uri=<attachment_id>))` — a
  bare FA-internal identifier with a placeholder `application/octet-stream` type. Nothing on the
  other side can resolve it. The server's response artifacts were slightly better (an absolute
  `/api/attachments/<id>` URL) but that endpoint is authenticated against FA's own users, so a
  remote peer gets a 401 rather than the file.
- **Inbound.** A `FilePart` arriving from a peer became an `image_url` content part — a data URI for
  inline bytes, whatever the real type was — so a PDF or a spreadsheet reached the model as a
  malformed image and was never registered as an attachment. On the client side it was worse: an A2A
  task's file parts were rendered as the literal text `[File: <uri>]`, so a file produced by a
  remote agent could not be delivered to the user at all.

## Approach

Attachment bytes cross the A2A boundary **inline**, and every crossing goes through one chokepoint:
`A2AAttachmentTransfer` (`src/family_assistant/a2a/attachments.py`). It owns the
`AttachmentRegistry` and a `Database` handle, so it is the only place that can turn an FA attachment
id into wire bytes or wire bytes into a registered FA attachment. The pure converters in
`converters.py` no longer touch attachments at all; handed an attachment part they raise, so a
missed call site fails loudly instead of putting an identifier on the wire.

Both directions, on both the client and the server:

- **FA attachment → A2A.** The registry is asked for the attachment's bytes, MIME type and original
  filename, which become a `FileWithBytes` part. An attachment the acting user cannot read, or whose
  bytes are missing, is an error — never a placeholder.
- **A2A → FA attachment.** Inline bytes (a `FileWithBytes`, or a `data:` URI) are decoded and
  registered with the registry, and the resulting attachment id becomes an `attachment` content part
  (server inbound) or an entry in `ChatInteractionResult.attachment_ids` (client inbound).

Inline bytes are the *only* transfer this boundary has, and there is no URL fallback. FA's own
download URL needs an FA credential the peer does not hold, and a peer's URI is not fetched either
(below), so offering one would hand back a dangling reference on a task reported completed. An
attachment that cannot be sent — unreadable, or past the inline cap (`MAX_INLINE_ATTACHMENT_BYTES`,
10 MB of base64) — therefore fails the task with the reason, in both directions. A signed,
peer-usable transfer URL is what would lift the size ceiling; until there is one, the ceiling is
reported rather than papered over.

A response leaves by more than one path, and all of them carry its files. On `message/stream` the
queued files are not part of the text stream, so they are resolved once the stream closes and ride
out on the final artifact chunk — which also means the router follows the interaction to its end
rather than stopping at the first `done`, since `done` closes an agentic turn and a turn that called
a tool emits one and keeps going.

Inbound, the task id is claimed before any of the peer's files are registered. A retry that reuses a
task id is answered with the existing task, and registering first would leave a durable copy of
every file that no task will ever use.

A file registered from a peer carries the taint of the message that brought it, in the shape
`artifact_taint_sources` reads back. A stored artifact with no provenance reads as untainted, so
without this a peer's file would re-enter a later turn — opened or summarized — as trusted content.
Where the peer declares no taint of its own it gets the tier the A2A endpoints already give a peer's
text: a file in a message is no less trusted than the words around it. A file coming *back* from a
remote agent carries the taint of the turn that was sent to it as well, since the agent worked on
what it was given — and where the caller cannot say what that was (the polled path holds only a
remote task id) the file is recorded as unknown-external rather than assumed to be peer-trusted.

A file arriving *in a message* is also held to the registry's limit for its own type, not just the
inline cap: a deployment sets `max_multimodal_size` because providers reject larger media, and that
file is there to be read, so it is bounded like a user's upload of that type. A file a remote agent
*returns* is not — that is output the user asked for rather than input to a model, and the registry
stores tool-produced media unbounded by the same limit for the same reason.

## Deliberate simplifications

- **A remote `http(s)` file URI is not fetched.** It stays a reference (an `image_url` content part
  inbound to the server, a text reference in a client-side result). Fetching would mean issuing an
  authenticated request to a URL chosen by the peer, which is a credential-leak and SSRF surface for
  an uncommon case. The common case — a peer that inlines its bytes, which is what FA itself now
  does — is handled fully.
- **A file registered for work that then does not happen is not collected automatically.** The
  registry's reaper only collects `source_type="user"` rows, and files received over A2A register as
  tool attachments, whose references live in places the reaper deliberately does not read. So a
  registration for work that then does not happen has to be avoided rather than cleaned up after: a
  message's files are registered as a set, and if one fails the ones already written are removed.
  What is left uncovered is a completed remote task polled twice after a crash between the poll and
  the run being finalized, which registers a second copy; keying registration to remote task and
  part identity would close it, at the cost of a dedup index and identity plumbing on every inbound
  path for a crash in one narrow window. Reclassifying these as user attachments so the reaper does
  cover them is the better long-term answer, and needs the reaper's reference scan to learn about
  content-part and delegation-run references first — otherwise it would collect live files. What is
  *not* left to any of this is a claimed A2A task row: any failure after the claim finalizes it as
  failed, because a row stuck `working` makes every retry with that task id hand back a task that
  never progresses.
- **Attachments stored from a polled async delegation are ownerless.** `poll_async` carries no
  acting user (the worker polls on behalf of a persisted run), so files a remote agent returns on
  that path are registered without an owner, like tool-generated attachments. The synchronous and
  submit paths do know the acting user and record it.
