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

Size is bounded by the existing inline cap (`MAX_INLINE_ATTACHMENT_BYTES`, 10 MB of base64). The
download URL is the fallback for exactly one case — an attachment the server verified but cannot
inline, where losing the whole answer over one oversized file would be worse. It is never a fallback
for an attachment that could not be read: the download endpoint applies the same ownership and
content checks, so a URL there would hand the peer a dangling reference on a task marked completed.
That fails the task with the reason instead. Outbound from the client the cap stays a hard,
deterministic error, as it was.

A response leaves by more than one path, and all of them carry its files. On `message/stream` the
queued files are not part of the text stream, so they are resolved once the stream closes and ride
out on the final artifact chunk — which also means the router follows the interaction to its end
rather than stopping at the first `done`, since `done` closes an agentic turn and a turn that called
a tool emits one and keeps going.

Inbound, the task id is claimed before any of the peer's files are registered. A retry that reuses a
task id is answered with the existing task, and registering first would leave a durable copy of
every file that no task will ever use.

## Deliberate simplifications

- **A remote `http(s)` file URI is not fetched.** It stays a reference (an `image_url` content part
  inbound to the server, a text reference in a client-side result). Fetching would mean issuing an
  authenticated request to a URL chosen by the peer, which is a credential-leak and SSRF surface for
  an uncommon case. The common case — a peer that inlines its bytes, which is what FA itself now
  does — is handled fully.
- **Attachments stored from a polled async delegation are ownerless.** `poll_async` carries no
  acting user (the worker polls on behalf of a persisted run), so files a remote agent returns on
  that path are registered without an owner, like tool-generated attachments. The synchronous and
  submit paths do know the acting user and record it.
