# Collaborative Chats

## Status

Product requirements approved; implementation not started.

## Summary

Family Assistant will support conversations containing multiple authenticated application users on
the web and native iOS chat surfaces. A conversation has a binary access model: a user is either a
member or a non-member. Every member may read the conversation and send prompts and attachments.

Every accepted member message is delivered to the assistant. The assistant may eventually choose to
finish a turn without posting a reply through a separate `end_turn_without_reply` tool. That tool is
a related future feature, not part of this design's implementation scope.

Each assistant turn runs with the identity and permissions of the member who submitted the prompt:

- data access is scoped to the prompting user;
- state-changing tools execute as the prompting user;
- confirmation requests are addressed only to the prompting user; and
- another conversation member cannot approve or reject that user's request.

Telegram group conversations remain a separate surface. Making a Telegram conversation visible in,
or synchronized with, web or iOS is explicitly out of scope. There are no channel bindings, mirrored
messages, or cross-channel conversation identifiers in this design.

## Problem

The existing chat model assumes a conversation has exactly one canonical owner. Ownership is not a
stored conversation property; it is inferred from the distinct `user_id` values on user messages.
The web API then requires the caller to be the sole canonical owner. A conversation with more than
one author, including a Telegram group, is omitted from the conversation list and returns `404` from
the read, write, stream, and acknowledgement APIs.

That invariant made the in-memory stream hub safe without subscriber-level filtering, but it also
prevents intentional collaboration. Removing the sole-owner check alone would not be sufficient:

- there is no conversation or membership record;
- an invited user cannot be authorized until after posting, but cannot post until authorized;
- API and client message models do not identify the human author;
- LLM history does not distinguish messages written by different people;
- notifications resolve one owner from recent history;
- activity events are routed to one exact user id;
- attachment access assumes a single user or existing conversation owner;
- confirmation ownership and turn-control behavior are not expressed as collaborative invariants;
- concurrent member prompts can load inconsistent history and produce replies out of order; and
- web/iOS and Telegram histories are partitioned by `interface_type`, so sharing a raw
  `conversation_id` would not create a coherent cross-channel conversation anyway.

Collaborative chat therefore needs a first-class conversation and membership model rather than an
authorization exception around the existing message-derived ownership rule.

## Confirmed Product Requirements

### Authentication and eligibility

- Web and iOS participants must be authenticated.
- A participant must resolve to a canonical user in the configured top-level `users` list.
- API tokens remain eligible only when their owner resolves to a configured canonical user.
- There are no anonymous users, guest links, public invite links, or email-only invitees.
- A Telegram participant must resolve through `users[].telegram.user_ids`; an update from an unknown
  Telegram user is ignored or rejected at the interface boundary as it is today.

### Membership

- Authorization is binary: `member` or `non-member`.
- A member may list, open, read, stream, and post to the conversation.
- A member may send text prompts and supported attachments.
- A non-member receives `404` for conversation-specific resources so the API does not disclose the
  conversation's existence.
- Membership is explicit and durable. It is not inferred from message authorship.
- Removing a member immediately revokes future reads, writes, stream subscriptions, attachment
  access, and conversation notifications.
- Historical messages retain their author attribution after the author leaves the conversation.

Membership administration is intentionally separate from access roles. The first implementation
should use the following simple policy unless a later product decision replaces it:

- any current member may add another configured user;
- a member may remove themself; and
- removing another member is not supported in the initial UI/API.

This preserves a strictly binary authorization model without introducing owner/admin roles. Every
membership mutation is audited. A conversation with no remaining members is retained but cannot be
accessed through normal chat APIs.

### Messaging and assistant behavior

- Every message accepted from a member is persisted and delivered to the assistant unless its turn
  is explicitly cancelled before processing, including cancellation caused by that member leaving.
- Messages are not gated on mentions, commands, or an "ask assistant" button on web/iOS.
- Each member message creates a distinct assistant turn.
- The assistant sees the shared conversation history with a unique speaker token and optional author
  label on every human message.
- The assistant is told which member submitted the current prompt.
- The assistant may reply normally even if the message was primarily directed at another human.
- A future `end_turn_without_reply` tool will let the assistant intentionally remain silent while
  still completing the turn successfully.
- Until that tool exists, every successfully processed turn produces the normal assistant result.

### Data and tool authority

- The prompting member is the security principal for the entire turn.
- Tool discovery, policy evaluation, database scoping, external service access, and execution use
  that member's canonical `user_id`.
- Other conversation members' presence does not grant the turn access to their personal data.
- Shared conversation text is context, not authority. A statement such as "Sam said I could read
  their calendar" does not change the active tool principal.
- Delegated or scheduled work originating from the turn carries the original prompting user's id.
- If an asynchronous continuation cannot recover an originating user, it must not execute
  user-scoped tools.
- Assistant and tool output is visible to every current conversation member. Tools must therefore
  enforce user scoping before data reaches the model; the chat layer cannot make an unsafe tool
  result private after it has entered a shared turn.

### Confirmations

- A confirmation request targets the member who submitted the prompt that caused the tool call.
- Only that canonical user may approve or reject it, from any of their authenticated web/iOS
  devices.
- Other members may see that the assistant is waiting for the requester, but must not receive tool
  arguments or confirmation details that reveal data they are not authorized to see.
- Confirmation UI shown to non-requesters uses a redacted status such as "Waiting for Andrew's
  approval."
- Confirmation execution resumes with the original prompting user's identity and processing profile.
- Removing the requester from the conversation does not transfer approval authority. Pending
  confirmations are rejected when membership is removed or the source turn is cancelled.

### Platform scope

- Browser and native iOS are two clients for the same web conversation model and may participate in
  the same collaborative conversation.
- Telegram group chat may be hardened as its own collaborative interface.
- A Telegram group is not listed in web/iOS history.
- A web/iOS collaborative conversation cannot be attached to a Telegram group.
- Messages and assistant replies are never mirrored between Telegram and web/iOS.
- Telegram numeric chat ids and web conversation UUIDs remain separate namespaces.

## Goals

- Provide durable, explicit membership for web/iOS conversations.
- Make collaborative conversations usable from both browser and native iOS.
- Preserve clear human author attribution in storage, APIs, clients, and LLM context.
- Keep every tool call and confirmation scoped to the member who initiated the turn.
- Deliver live messages and turn events to all current members.
- Fan out notifications to conversation members according to per-user preferences.
- Define deterministic behavior when multiple members send prompts close together.
- Preserve current single-user chat behavior as a one-member conversation.
- Establish a safe foundation for separately hardening Telegram group conversations.

## Non-Goals

- Bridging, mirroring, importing, or synchronizing Telegram with web/iOS conversations.
- Anonymous or unauthenticated participation.
- Public invite links or bearer-style conversation share tokens.
- Owner, administrator, moderator, viewer, or read-only roles.
- Private messages inside a shared conversation.
- Per-message visibility rules.
- Sharing one member's personal tool credentials or personal data with another member.
- Multi-party approvals.
- End-to-end encryption beyond the application's existing transport and storage protections.
- Presence indicators, typing indicators, reactions, edits, deletion, read receipts, or mentions in
  the first implementation.
- Implementing `end_turn_without_reply`; this design only defines the chat contract it will need.
- Replacing the single-process in-memory stream hub with a distributed broker.

## User Experience

### Conversation creation

An authenticated configured user starts a new conversation and chooses one or more configured users
as initial members. The creator is always included. The server creates the conversation and all
initial membership rows atomically, then returns the conversation summary.

A new one-person conversation uses the same model. "Private chat" is simply a conversation with one
member rather than a separate conversation type.

### Conversation list

The web sidebar and iOS conversation list show only conversations where the caller has an active
membership. A summary includes:

- conversation id;
- title;
- latest visible message preview and timestamp;
- message count;
- member summaries; and
- whether a turn is running or waiting for the caller's approval.

The UI may render a compact participant label, for example "Andrew, Sam + 1," but must not infer
membership from message authors.

### Conversation thread

Human messages show the configured user label and a deterministic avatar/initial. The current user's
messages may retain the existing visual alignment, while messages from other members must be
visually distinguishable. Author identity must also be available to assistive technology and not
communicated by color alone.

Assistant messages remain attributed to Family Assistant. Tool cards identify the requesting member
when relevant. A member who cannot resolve a confirmation sees only redacted waiting/resolved state.

### Membership changes

The initial member picker lists configured users only. A current member may later add another
configured user. A member can leave a conversation after an explicit confirmation. Membership
changes appear promptly on every connected client through a content-free activity event followed by
an authoritative refetch.

### Notifications

When a human posts, notify other members according to their preferences; do not notify the author on
their own devices. When the assistant completes a turn, notify members who did not acknowledge the
completion on an active stream. Confirmation notifications go only to the requesting member.

Notification bodies must avoid including sensitive tool arguments. Deep links open the conversation
only after the normal membership check.

## Data Model

### `conversations`

Add a first-class conversation table:

| Column                     | Type                    | Constraints / meaning                                             |
| -------------------------- | ----------------------- | ----------------------------------------------------------------- |
| `id`                       | string/UUID             | Primary key; server-generated                                     |
| `title`                    | text                    | Non-empty display title supplied when the conversation is created |
| `created_by_user_id`       | string                  | Canonical configured user; audit metadata, not an access role     |
| `creation_idempotency_key` | UUID                    | Unique with `created_by_user_id`; supplied by the creating client |
| `creation_request_hash`    | string                  | Immutable hash of the normalized original create payload          |
| `processing_profile_id`    | string                  | Required and fixed for the lifetime of the conversation           |
| `created_at`               | timezone-aware datetime | Required                                                          |
| `updated_at`               | timezone-aware datetime | Required; bumped on visible activity                              |
| `archived_at`              | datetime, nullable      | Set when the conversation has no members or is archived later     |

`created_by_user_id` records provenance only. It does not grant permissions beyond an active
membership.

### `conversation_members`

| Column               | Type                              | Constraints / meaning                                           |
| -------------------- | --------------------------------- | --------------------------------------------------------------- |
| `conversation_id`    | FK                                | Part of composite primary key; cascade on conversation deletion |
| `user_id`            | string                            | Part of composite primary key; canonical configured user        |
| `joined_at`          | timezone-aware datetime           | Required                                                        |
| `left_at`            | timezone-aware datetime, nullable | `NULL` means active member                                      |
| `added_by_user_id`   | string                            | Canonical member that added this user                           |
| `membership_version` | UUID                              | Regenerated for each join/re-add; identifies that membership    |
| `speaker_token`      | string                            | Immutable, conversation-unique LLM/UI disambiguator             |
| `notification_level` | enum/string                       | Initially `all` or `muted`                                      |
| `last_ack_seq`       | integer, nullable                 | Optional durable notification/read cursor                       |

Re-adding a former member clears `left_at` and records a new audited membership event. Repository
queries expose active membership through one shared predicate rather than open-coding
`left_at IS NULL` throughout the application.

### `conversation_membership_mutations`

Persist member-add request outcomes so a lost response can be retried without unintentionally
rejoining a user who subsequently left:

| Column               | Purpose                                                                    |
| -------------------- | -------------------------------------------------------------------------- |
| `conversation_id`    | Owning conversation                                                        |
| `acting_user_id`     | Authenticated member that submitted the mutation                           |
| `idempotency_key`    | Client-supplied UUID; unique with conversation and acting user             |
| `request_hash`       | Immutable hash of the normalized target user and operation                 |
| `resulting_event_id` | Durable reference to the membership event produced by the original request |
| `created_at`         | Timestamp for retention and diagnostics                                    |

An identical retry returns the original result without changing current membership. Reusing a key
with a different target or operation returns `409`, even if conversation membership has changed
since the original request.

### `conversation_membership_events`

Append-only audit records capture:

- conversation id;
- affected user id;
- action (`added`, `left`, `removed_by_system`);
- acting user id when applicable; and
- timestamp.

This table is operational/audit history and is not used as the primary authorization source.

### `message_history` changes

Retain `message_history` as the turn transcript, with these changes:

| Change                                            | Purpose                                                           |
| ------------------------------------------------- | ----------------------------------------------------------------- |
| Add nullable FK `collaborative_conversation_id`   | Link only migrated/new web/iOS rows to `conversations.id`         |
| Keep legacy `conversation_id` without a global FK | Preserve Telegram and other interface-specific transcripts        |
| Rename or redefine `user_id` as `author_user_id`  | Make human authorship explicit                                    |
| Add `author_label` snapshot                       | Stable display and LLM attribution after config label changes     |
| Keep `interface_type`                             | Origin/diagnostics, not an authorization partition within web/iOS |
| Add/retain `turn_id`                              | Relate user, assistant, and tool rows to one initiating turn      |
| Add `client_message_id` where needed              | Idempotent member message submission                              |

An implementation may keep the physical `user_id` column name to reduce migration risk, but all
models and repository APIs should call the concept `author_user_id`. Compatibility aliases should
not be preserved indefinitely; internal callers should migrate together.

Assistant, tool, system, and error rows have no human `author_user_id`. Their initiating principal
comes from the mandatory durable `conversation_turns.initiating_user_id`, not from message
authorship. This matters for scheduled callbacks and delegation wakeups whose trigger is a system
message rather than a human message. Every turn delivered into a collaborative conversation must
carry its original canonical user; an ownerless system trigger cannot create such a turn.

### `conversation_turns`

Add a durable turn table:

| Column                  | Purpose                                                          |
| ----------------------- | ---------------------------------------------------------------- |
| `id`                    | Server-generated primary key                                     |
| `client_request_id`     | Nullable client-supplied idempotency key for direct client turns |
| `client_request_hash`   | Nullable immutable hash of normalized prompt and attachments     |
| `parent_turn_id`        | Nullable FK to the causal turn for a continuation                |
| `conversation_id`       | Owning conversation                                              |
| `initiating_user_id`    | Tool/data/confirmation principal                                 |
| `status`                | `queued`, `running`, `complete`, `failed`, `cancelled`, `silent` |
| `processing_profile_id` | Profile selected for this turn                                   |
| `lease_owner_id`        | Nullable worker identity while running                           |
| `lease_expires_at`      | Nullable expiry used to recover an abandoned running turn        |
| timestamps              | Queue/start/end ordering and diagnostics                         |

Today turn state is partly inferred from message rows and partly retained in memory. Collaboration
introduces a queue, system-triggered continuations, and an intentional no-reply terminal state, so
durable turn state is required. `initiating_user_id` is non-null and immutable. Scheduled,
delegated, and other system-triggered continuations copy the originating turn's user id into their
own durable turn row before processing begins. The server generates `id` for every turn. Direct
client submissions additionally require `client_request_id`, unique within the conversation;
scheduled or delegated continuations leave it `NULL` and retain their causal parent turn id.

## Membership Service and Authorization

Introduce a single service/repository boundary used by all chat features:

```python
await conversation_access.require_member(conversation_id, user_id)
await conversation_access.is_member(conversation_id, user_id)
await conversation_access.list_member_ids(conversation_id)
await conversation_access.add_member(conversation_id, acting_user_id, new_user_id)
await conversation_access.leave(conversation_id, user_id)
```

`require_member` returns the authoritative conversation/member context or raises the same `404` used
for missing conversations. Do not scatter membership SQL across routers and services.

Membership checks are required for:

- conversation summaries and search;
- full and paginated message history;
- creating, stopping, steering, and resuming turns;
- SSE subscribe and acknowledgement;
- activity-stream fan-out;
- attachment claim, read, preview, and delete;
- pending confirmation status displayed in a conversation;
- voice-session append/create behavior;
- scheduled/delegated completions delivered into the conversation;
- notification deep links; and
- member listing and mutation.

The caller must remain a member for the duration of long-lived delivery. When membership is removed,
the backend should disconnect that user's conversation stream promptly. Every subsequent reconnect
or history fetch rechecks durable membership.

### Conversation creation

Client-generated conversation ids are replaced by a server-side creation endpoint. This avoids the
current empty-conversation rule where any authenticated caller may claim an unused id. Creation and
initial membership rows commit atomically.

Clients optimistically allocate a local UUID as `creation_idempotency_key`, but it never becomes an
authorization identifier. Retrying creation with the same
`(authenticated user, creation_idempotency_key)` and identical request returns the existing
conversation. Before the original transaction commits, the server normalizes the title, sorted
member ids, and profile id and stores their immutable request hash. Retries compare against that
stored hash rather than mutable conversation or membership rows. Reusing the key with a different
original payload returns `409`, even if the title or membership changed later. The server returns
the durable conversation id before the first turn starts.

## API Requirements

Exact URL naming may follow existing conventions, but the API needs these capabilities.

### Conversation management

```http
GET  /api/v1/users
POST /api/v1/chat/conversations
GET  /api/v1/chat/conversations
GET  /api/v1/chat/conversations/{conversation_id}
GET  /api/v1/chat/conversations/{conversation_id}/members
POST /api/v1/chat/conversations/{conversation_id}/members
PATCH /api/v1/chat/conversations/{conversation_id}/members/me
DELETE /api/v1/chat/conversations/{conversation_id}/members/me
```

`GET /api/v1/users` is an authenticated directory of configured users for member pickers. It returns
only canonical `user_id` and configured display `label`; it does not expose OIDC subjects, email
aliases, Telegram ids, developer flags, or other identity mappings. All configured users, including
the caller, are returned because any configured user is eligible to be added.

Create request:

```json
{
  "creation_idempotency_key": "6509d912-a6f8-44f5-9c67-c26832be8962",
  "title": "Summer trip",
  "member_user_ids": ["andrew@example.com", "sam@example.com"],
  "profile_id": "trusted"
}
```

`title` is required and non-empty at creation, before any turn exists. Web and iOS may prefill a
localized "New conversation" value for the user to accept or edit, but the server does not derive a
title from an unavailable first prompt. Automatic prompt-based renaming would be a separate later
mutation with its own authorization and idempotency contract.

The server ignores any attempt to omit the authenticated creator and rejects unknown/unconfigured
user ids. It also rejects an unknown profile, a profile that is disabled, or a profile restricted to
delegation/remote invocation rather than direct chat. It must not apply the current turn endpoint's
unknown-profile fallback because the selected profile is immutable.

Member addition accepts exactly one configured user per request and requires a client-supplied
`idempotency_key`. Its durable result follows `conversation_membership_mutations`; a retry returns
the original outcome and never re-adds someone who left after the first request.
`PATCH .../members/me` accepts `{"notification_level": "all"}` or `{"notification_level": "muted"}`
and may update only the caller's active membership.

Leaving targets the caller's current `membership_version`, supplied as a request precondition. A
successful leave invalidates that version. Retrying it is harmless, while retrying an old leave
after the user has been re-added cannot remove the new membership because its version differs. A
conversation detail response exposes `my_membership_version`, and leave supplies that value in an
`If-Match` header. A client must refetch before leaving again from a new membership instance.

### Message and turn APIs

Existing endpoints remain structurally useful:

```http
POST /api/v1/chat/turns
GET  /api/v1/chat/conversations/{conversation_id}/messages
GET  /api/v1/chat/conversations/{conversation_id}/stream
POST /api/v1/chat/ack
POST /api/v1/chat/turns/{turn_id}/cancel
POST /api/v1/chat/turns/{turn_id}/steer
```

They change from sole-owner authorization to membership authorization. `POST /turns` derives
`initiating_user_id` exclusively from the authenticated principal, never from request JSON.

Conversation messages include:

```json
{
  "internal_id": 123,
  "turn_id": "...",
  "initiating_member": {
    "user_id": "andrew@example.com",
    "label": "Andrew"
  },
  "role": "user",
  "author": {
    "user_id": "andrew@example.com",
    "label": "Andrew"
  },
  "content": "Can everyone do the 18th?",
  "timestamp": "...",
  "attachments": []
}
```

Non-human rows return `"author": null`, but every row returns the immutable `initiating_member`
summary joined from its durable turn. This lets a paginated reload attribute or redact assistant,
tool, confirmation, and system-continuation rows even when the page contains no human message.
Clients must not derive the current user from label text; the current authenticated canonical user
id must be available in session/bootstrap data.

### Stream and activity events

Per-conversation SSE remains the live delivery mechanism. Membership is checked before subscribing.
The hub may fan out human and assistant transcript events to every authorized subscriber because all
members have the same transcript visibility. Raw existing `tool_call` events are not safe for shared
fan-out because they contain function arguments. Shared tool events contain only a safe display
name, lifecycle status, requester label, and turn id; they never contain arguments or raw/private
results. Requester-only details use an authenticated target-user endpoint rather than the shared
stream.

Confirmation events follow the same rule: the shared stream contains only a redacted status event
with the requester label and turn id. It never contains tool arguments, confirmation prompts, policy
data, or private results. The requester reacts to that event or their targeted push by fetching the
existing requester-authorized pending/detail endpoint. This keeps shared payloads safe without
requiring subscriber-specific transformation.

Add or standardize these content-free/rich event types:

- `message_committed`: authoritative message id, conversation id, author summary, and turn id;
- `turn_queued`: turn id and initiating member summary;
- `turn_started`: turn id and initiating member summary;
- existing text and attachment events, plus redacted tool lifecycle events only;
- redacted confirmation status only; requester details come from target-user APIs;
- `turn_ended`: status including future `silent` completion;
- `membership_changed`: content-free refetch nudge; and
- existing heartbeat/drop control frames.

The activity stream must subscribe by canonical user and publish to every active member rather than
to one inferred owner. It remains advisory: clients refetch the membership-filtered list.

### Confirmation APIs

The durable confirmation record remains targeted to one `target_user_id`. Existing confirmation
approval authorization already enforces exact target-user equality and should remain unchanged.

Conversation APIs need a redacted status projection so other members can render waiting state
without accessing tool name, arguments, policy fingerprint, or private result. Full pending/detail
endpoints remain visible only to the target user.

## Turn Ordering and Concurrency

Collaborative chat permits multiple people to send at nearly the same time. Running those turns in
parallel would let each model invocation load a different history snapshot, produce out-of-order
answers, and invoke conflicting tools. The initial implementation therefore serializes assistant
turns per conversation.

1. In one database transaction, persist the member message and its turn in `queued` state. Neither
   row may exist without the other.
2. After that transaction commits, publish the committed human message to every member.
3. If no other turn is running, atomically claim the oldest queued turn and establish a renewable
   worker lease.
4. Recheck that the initiating user is still an active member; cancel the turn if not.
5. Build its LLM context from committed conversation history through that turn's user message.
6. Run tools with the queued turn's `initiating_user_id`, rechecking membership at every tool and
   confirmation boundary.
7. Persist and publish the terminal result only while the initiating user remains a member.
8. Release the lease and claim the next queued turn.

Workers renew the running-turn lease while processing. At startup and before claiming queued work,
the queue service atomically detects expired leases, marks those turns `failed` with an explicit
abandoned-worker reason, rejects their pending confirmations, and then releases the next queued
turn. An expired turn is not automatically replayed because its tools may already have produced side
effects; the requester may submit a new turn after seeing the failure.

Queue order is `(message timestamp, message internal_id)` or an explicit monotonically increasing
conversation sequence assigned transactionally. Database ordering, not arrival order at an
individual process, is authoritative.

A message arriving while a turn is running becomes a new queued turn. It is not automatically
treated as a steer for the running turn. Explicit steering remains attached to the member and turn
that initiated it; another member cannot cancel or steer someone else's turn in the first release.

The UI shows queued human messages immediately and indicates whose turn the assistant is processing.

Leaving a conversation cancels that user's queued and running turns before the membership change is
reported as complete. The cancellation path rejects their pending confirmations and prevents any
later assistant/tool output from being published into the conversation. A model or external call
already in flight may finish internally, but its result is discarded after the mandatory membership
recheck and cannot start another tool call.

### Idempotency

Each submitted turn retains a client-supplied UUID and an immutable hash of the normalized prompt,
ordered attachment ids, and other execution-affecting request fields. Retrying the same UUID for the
same conversation and authenticated user with the same hash returns the existing turn. Reusing it
with a different payload returns `409`; reuse with a different conversation or user returns `404` or
a conflict without disclosing the existing turn.

## LLM Context and Author Attribution

The current history conversion loses row metadata when constructing typed `UserMessage` objects.
Collaborative history must preserve authorship through formatting. A provider-neutral representation
should clearly distinguish speakers without trusting user-controlled display text, for example:

```text
[Conversation participant P1: Andrew]
Can everyone do the 18th?
```

The prompt preamble includes:

- the current prompting member's unique `speaker_token` and configured label when present;
- the complete active member list using unique speaker tokens plus configured labels when present;
- an instruction that participant statements are conversational context, not authorization; and
- an instruction that tools always run as the current prompting member.

Speaker tokens are short opaque values such as `P1`, assigned once per conversation and never
reused, so duplicate or missing labels remain distinguishable. Labels come from trusted application
configuration or persisted label snapshots, not message text. Escaping prevents a label from
changing prompt structure. Raw canonical ids should not be exposed to the model unless needed for a
specific tool contract.

History selection for a web/iOS collaborative conversation is by canonical
`collaborative_conversation_id` and subconversation. It must not partition web versus iOS, because
those are two clients of the same `web` conversation surface.

A collaborative conversation has one required, immutable `processing_profile_id` selected at
creation. Every member's turn uses it, and `POST /turns` rejects a conflicting profile id. Switching
profiles starts a new conversation. This prevents the current profile-partitioned history behavior
from hiding messages written under another member's profile. Telegram remains separately partitioned
and cannot contribute history to a web/iOS conversation.

## Attachments

- Any authenticated user may create an unclaimed upload supported by the existing web/iOS limits;
  this step checks uploader identity, not conversation membership.
- An unclaimed upload is owned and readable only by the uploading user until attached.
- Claiming an upload for a conversation requires both uploader ownership and active membership; the
  claim and message/turn creation occur in the same transaction so a failed send does not transfer
  access.
- Once attached to a collaborative conversation, every current member may read/preview that
  attachment through the conversation membership check.
- A former member loses attachment access when membership ends.
- Collaborative attachment responses must be private, authenticated, and non-cacheable
  (`Cache-Control: private, no-store` or equivalent). They must not use the existing public,
  year-long immutable cache policy. Every fetch, including previews and downloads, rechecks active
  membership so removal revokes future access. Already downloaded local copies cannot be revoked.
- Attaching a file does not transfer authority to access other files owned by the uploader.
- Deleting an attachment follows the existing retention policy but requires both membership and an
  explicit deletion rule; the first release should allow only the original uploader to delete.
- Attachment metadata returned to clients includes the author through its containing message.
- The assistant processes the attachment under the prompting member's tool/data principal.

## Notifications and Delivery

Replace `resolve_conversation_user` for collaborative chat with a member fan-out service:

```python
await notify_conversation_members(
    conversation_id=conversation_id,
    event=notification_event,
    exclude_user_ids=event_specific_exclusions,
    ...,
)
```

It loads active members and their notification preferences, then sends through Web Push and APNs.
For a human-message event, `event_specific_exclusions` contains the message author. For an assistant
or background completion it is empty; per-member acknowledgement/delivery state suppresses only
members who already received the completion on an active stream, including the requester when
appropriate. Confirmation notifications bypass member fan-out and target only the confirmation's
user.

Disconnect-push suppression can no longer be a single `TurnRecord.delivered` boolean: one member's
acknowledgement must not suppress another member's notification. Delivery/ack state must be tracked
per `(turn_id, user_id)` or compared with a durable per-member conversation cursor.

The in-memory implementation remains acceptable for the current single-worker deployment. The data
and API model should not assume it is permanent; a future shared broker must be able to reproduce
membership-aware fan-out and per-user acknowledgements.

## Web Client Requirements

- Create a conversation before starting its first turn.
- Add a configured-user member picker to new-chat creation.
- Display member summaries in the sidebar/header.
- Render human author label/avatar on every user message.
- Show queued/running member attribution.
- Apply live human messages from other members or refetch on `message_committed`.
- Add member-list, add-member, leave-conversation, and notification controls.
- Hide full confirmation detail/actions from non-requesters while showing redacted status.
- Remove a conversation immediately when the current user's membership ends.
- Treat a future `turn_ended(status="silent")` as successful completion with no assistant bubble.
- Preserve resumable streaming, history reconciliation, stop behavior, attachment previews, and
  conversation-level profile display; choosing a different profile starts a new conversation.

## Native iOS Requirements

Native iOS uses the same APIs and semantics as web:

- add conversation, member, author, queue, and redacted-confirmation models;
- create the server conversation before the first send;
- add configured-user member selection and conversation membership screens;
- render attributed human messages accessibly;
- update the conversation list from membership-aware activity events;
- refresh/remove open conversations when membership changes;
- fan out APNs deep links through the normal membership authorization path;
- preserve background/foreground stream resume and push acknowledgement; and
- treat future silent terminal turns as success rather than an interrupted/missing reply.

Browser and iOS remain interchangeable clients for the same canonical user and conversation.

## Telegram Group Requirements

Telegram collaboration is independent of web/iOS collaboration. The existing bot handler accepts
group messages without an explicit private-chat filter, but the behavior needs hardening before it
is documented as supported.

- Resolve and authorize the actual sender of every update through configured Telegram identities.
- Use the Telegram chat id as the Telegram-only conversation boundary.
- Never expose that conversation through web/iOS APIs.
- Ensure batching cannot combine messages from different human authors into one prompt attributed to
  the last sender. Batch keys must include sender or batches must flush on author change.
- Serialize assistant work per Telegram chat using the same queue semantics, preserving each
  message's initiating user.
- A member message that arrives during another member's turn creates a queued turn rather than a
  cross-user steer.
- Only the initiating member may interrupt, steer, approve, or reject their turn.
- Preserve Telegram reply/thread metadata and author labels in LLM context.
- Every authorized message received by the bot is delivered to the assistant. Telegram Bot privacy
  mode still controls which group messages Telegram delivers to the bot; the application cannot
  process updates it never receives.
- A future `end_turn_without_reply` result posts no Telegram message but records a successful
  terminal turn.

No Telegram work in this design introduces a channel binding, shared conversation id, mirrored
message, or web/iOS visibility.

## `end_turn_without_reply` Integration Contract

The separate tool design must satisfy these collaborative-chat invariants:

- it is available only during an assistant turn;
- invoking it ends the current turn successfully and stops further model/tool iterations;
- it does not send an assistant message to web, iOS, or Telegram;
- it persists an unambiguous durable terminal state (`silent` or equivalent);
- SSE emits `turn_ended` with that terminal state;
- clients remove loading/queued indicators without showing an error or an empty bubble;
- history reconciliation does not classify the turn as incomplete;
- the next queued turn proceeds normally; and
- diagnostics retain who initiated the turn and why it ended without a reply, without exposing
  hidden reasoning to users.

Until this contract is implemented, collaborative turns must use existing terminal reply behavior.

## Migration

### Existing web/iOS conversations

For every existing conversation whose user messages resolve to exactly one canonical user and whose
turns resolve to exactly one direct-chat processing profile:

1. create a `conversations` row using the existing id;
2. add that canonical user as its active member;
3. populate `created_by_user_id` with that user and assign a one-time migration idempotency key;
4. set `collaborative_conversation_id` on only that conversation's web/iOS message rows;
5. create durable turn rows with the initiating user recovered from each turn's user message or
   existing owner-bearing continuation metadata;
6. copy a configured label snapshot onto historical human messages where possible; and
7. retain timestamps, turn ids, interface types, and attachments unchanged.

Legacy owner ids are canonicalized through `UserIdentityResolver`. A row or system-triggered turn
whose initiating user cannot be recovered is not silently assigned; migration reports it for
operator action and leaves it outside the collaborative conversation model.

Existing conversations containing several canonical authors are not automatically imported into
web/iOS collaboration. They are expected to be Telegram groups or anomalous legacy data and remain
outside web/iOS listing until explicitly handled. This prevents the migration from turning an
accidental id collision into data sharing.

Likewise, a legacy conversation containing turns from more than one processing profile is not
automatically imported, because one collaborative conversation cannot preserve that history while
also enforcing an immutable profile. Migration reports those conversation ids and their profile sets
for operator handling. The operator may archive the legacy thread or explicitly split it at profile
boundaries into separate conversations; migration never silently chooses a profile or hides
mismatched turns.

### New writes during rollout

Prefer a maintenance migration or a single coordinated deployment over a long dual-write period.
Internal code has no backward-compatibility requirement: once the new repositories are active,
conversation authorization must use membership everywhere and the sole-owner path should be deleted.

### Rollback

The database migration must preserve existing message rows. A rollback may stop exposing
collaborative conversations, but must not delete their transcript. Because old code refuses
multi-owner conversations, rolling back application code will fail closed for shared conversations.

## Security Invariants

01. Authentication establishes a canonical configured user before any chat operation.
02. Membership authorizes transcript visibility; message authorship never grants membership.
03. Non-members receive `404`, including for attachments and live streams.
04. A turn's initiating user is derived from authentication and is immutable.
05. Tool and data access use only that initiating user.
06. Confirmations target and may be resolved only by that initiating user.
07. Shared transcript text cannot change identity, membership, tool principal, or approval
    authority.
08. Member fan-out never includes former or non-members.
09. Full confirmation details never fan out to other members.
10. Telegram data never enters a web/iOS collaborative conversation.
11. A missing/ambiguous identity or membership check fails closed.
12. Membership removal cancels that user's queued/running turns, rejects their pending
    confirmations, and invalidates long-lived delivery before completing.
13. Shared SSE events never contain requester-only confirmation details.
14. Every collaborative turn, including a system-triggered continuation, has a durable initiating
    user.

These invariants should be expressed in service APIs and tests, not only in prompts or client UI.

## Implementation Milestones

### Milestone 1: Conversation foundation

- Add conversation, membership, membership-audit, and durable turn tables/repositories.
- Add idempotent server-side conversation creation, configured-user directory, member management,
  and self notification-preference endpoints.
- Migrate existing single-user web/iOS histories.
- Replace message-derived sole-owner checks with centralized membership authorization.
- Add author metadata to storage and API responses.
- Fix one immutable processing profile per collaborative conversation.
- Keep existing one-member web/iOS UX functional.

### Milestone 2: Safe collaborative turns

- Add deterministic per-conversation turn serialization.
- Preserve initiating user throughout processing, tools, delegation, and confirmations.
- Cancel queued/running turns when their initiating user leaves.
- Add author-aware LLM history and participant/current-speaker prompt context.
- Keep requester-only full confirmations off the shared stream and add redacted member status.
- Add attachment membership authorization.
- Add backend functional and concurrency coverage.

### Milestone 3: Web collaboration UX

- Add member selection, member list, add, and leave flows.
- Render author identity and queued/running member state.
- Make per-conversation and activity SSE membership-aware.
- Implement per-member notification delivery/acknowledgement.
- Add two-user/two-browser functional tests.
- Update `docs/user/USER_GUIDE.md`, relevant tool descriptions, and system prompts as required for
  the user-visible feature.

### Milestone 4: Native iOS parity

- Extend models, API client, view model, and SwiftUI screens.
- Add member and author UI.
- Add membership-aware live updates, APNs behavior, and deep-link handling.
- Add unit and UI tests for two authenticated users.
- Update the native app documentation and user guide.

### Milestone 5: Telegram group hardening

- Fix author-safe batching and per-user turn controls.
- Serialize group turns while retaining the initiating user.
- Add attributed LLM context and requester-only confirmation behavior.
- Test Telegram privacy-mode expectations and multiple authorized senders.
- Document Telegram groups as a separate collaboration surface.

### Separate feature: silent turn completion

- Design and implement `end_turn_without_reply` against the integration contract above.
- Add backend, web, iOS, and Telegram tests for intentional no-reply completion.

## Testing Requirements

### Storage and migration

- Creating a conversation atomically creates initial memberships.
- Retrying creation with the same user/key returns one conversation; conflicting reuse returns
  `409`.
- Existing one-user conversations migrate to one-member conversations.
- Ambiguous/unconfigured legacy owners are reported and not assigned.
- Telegram/other legacy message rows remain valid without a collaborative-conversation FK.
- Every migrated collaborative turn, including eligible system triggers, has an initiating user.
- Historical authors retain labels after membership ends or config labels change.
- Re-add/leave operations produce correct audit events.

### Authorization

- A member can list, read, post, stream, acknowledge, and access conversation attachments.
- A non-member gets `404` from every conversation-specific surface.
- Adding a member grants access immediately.
- Leaving revokes reads, writes, streams, attachments, activity, and notifications.
- The authenticated user directory exposes only configured ids and labels.
- A member can update only their own notification preference.
- An API token and OIDC/iOS session for the same canonical user see the same memberships.
- A Telegram identity does not make a Telegram group visible on web/iOS.

### Tool and confirmation isolation

- A prompt from user A executes tools with user A's id even when user B is a member.
- A prompt from user B executes the same tool with user B's id.
- Shared history containing instructions from B cannot change A's tool principal.
- A confirmation created by A is fully visible and resolvable only by A.
- B sees at most redacted waiting state and receives authorization errors on direct approval calls.
- Removing A rejects A's pending confirmations for that conversation.
- Deferred/system-triggered work durably retains the originating user's id.

### Concurrency

- Near-simultaneous prompts are persisted once and processed in deterministic order.
- The second member's message appears live while the first turn is running.
- The second prompt is not injected as a steer into the first turn.
- Stop/steer by a different member is rejected.
- A member who leaves has queued/running turns cancelled before another tool or result publish.
- A failed/cancelled/silent first turn releases the next queued turn.
- Retried client turn ids do not duplicate messages, tools, or replies.
- Every turn uses the conversation's fixed profile and sees the full shared history.

### Streaming and notifications

- Two authenticated members receive committed messages and turn events live.
- A non-member cannot subscribe by guessing a conversation id.
- Leaving disconnects or invalidates an existing stream.
- Activity pings reach all active members and no non-members.
- One member's acknowledgement does not suppress another member's offline push.
- The initiating member does not receive a redundant notification for their own human message.
- Confirmation pushes go only to the requester.
- Shared conversation SSE exposes only redacted confirmation status; direct requester fetch returns
  full detail.

### Web and iOS

- Conversation creation with multiple configured members works end to end.
- Each client renders current-user and other-member messages with correct attribution.
- New members see existing history after joining.
- Former members lose the open conversation without leaking subsequent content.
- Attachments uploaded by either member render for all active members.
- Redacted confirmations never expose tool arguments to other members.
- Browser and iOS clients can participate concurrently in the same conversation.

### Telegram

- Two configured Telegram users in one group are attributed separately.
- Rapid messages by different users never merge under one author.
- Turns execute serially with their original sender's identity.
- One sender cannot interrupt, steer, or approve another sender's turn.
- Telegram group history remains absent from web/iOS lists and APIs.

## Acceptance Criteria

The feature is complete when:

- two configured, authenticated users can join one web/iOS conversation;
- both can send text and attachments and see each other's messages live;
- every human message is delivered to the assistant in deterministic order;
- the assistant can distinguish participants and the current prompting member;
- each turn can access only the prompting user's data and tools;
- only the prompting user receives and resolves full confirmations;
- non-members cannot discover or access the conversation or attachments;
- notifications and stream acknowledgements operate independently per member;
- existing one-user browser/iOS chats continue to work after migration;
- Telegram group collaboration is tested as a separate surface; and
- no Telegram message or conversation is bridged into web/iOS.

## Relevant Existing Components

- `src/family_assistant/web/routers/chat_api.py`: current sole-owner authorization and chat APIs.
- `src/family_assistant/web/conversation_stream_hub.py`: conversation and activity SSE.
- `src/family_assistant/storage/message_history.py` and repository: transcript and inferred owners.
- `src/family_assistant/services/user_identity.py`: canonical user resolution.
- `src/family_assistant/services/confirmation_service.py`: requester-targeted durable approvals.
- `src/family_assistant/services/notification_targets.py`: current single-owner notification lookup.
- `src/family_assistant/processing/service.py`: history, prompt construction, and turn setup.
- `frontend/src/chat/`: browser chat client.
- `ios/FamilyAssistant/FamilyAssistant/Chat/`: native iOS chat client.
- `src/family_assistant/telegram/`: Telegram updates, batching, delivery, and confirmation UI.
