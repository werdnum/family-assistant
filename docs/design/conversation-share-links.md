# Conversation Share Links

## Goal

Let a conversation owner give another authorized Family Assistant user a link to a read-only view.
The link is a privacy speedbump between household members, not an authorization substitute: every
request still requires an authenticated, allowlisted user, or a coding agent holding the read-only
diagnostics token.

## User experience

- A persisted conversation has a **Share** action in the chat header.
- Creating or explicitly replacing a link rotates the conversation's one active link, even if
  copying fails. The web header opens a dialog with separate creation/replacement, copy, and revoke
  actions. The dialog keeps the returned URL in component memory for selectable text and copy retry,
  never in persistent storage; after leaving the conversation, the URL cannot be retrieved. Copies
  reuse that URL without rotation, and other windows or devices can invalidate it. The native iOS
  app opens the system share sheet with each replacement URL.
- The owner can stop sharing, immediately invalidating the link.
- A recipient opens `/shared/conversations/{token}` and sees a dedicated read-only transcript.
- On `assistant.andrewgarrett.dev`, the iOS Associated Domains entitlement and AASA file route that
  link to a native read-only transcript. The custom scene delegate forwards both cold- and
  warm-launch Universal Link activities before the app router selects the native Chat destination.
- Shared conversations never appear in the recipient's conversation list and cannot be continued,
  steered, or used to approve tool calls.
- The transcript is live at read time: later persisted messages appear after a refresh.

## Sharing from Home

Home is one conversation that holds everything and keeps growing, so a link to it is rarely what an
owner means. Two narrower actions sit where the content appears, on web and iOS:

- **A thread** is a conversation, so its link is an ordinary conversation link. The action is on the
  thread's card in Home and in the thread itself, and it changes no behaviour on the server.
- **A message** gets its own link, because a conversation link cannot be narrowed. It is a separate
  share scope with its own token, rotated and revoked independently of any other link.

A message link shows what the owner picked: the row's text and its attachments. It leaves out the
rest of the conversation, including the question being answered, and the row's tool calls, reasoning
and traceback. Tool arguments and results can quote anything the turn read, and a person pressing
share on a reply is looking at the reply. A wider "share this exchange" is a different promise and
is not offered.

The attachment route applies the same scope: under a message link it serves only attachments named
on that row, not every attachment in the conversation.

Only a row a reader would take for a message can be shared: a user or assistant row in the main
history, with something to show, that is not internal, in a subconversation, or one of the markers
Home draws as a thread card or a moved turn. This check runs when a link is created and again on
every read.

The URL stays `/shared/conversations/{token}`, whichever the scope. The token alone says which it
is, so Universal Links, the AASA file, the edge routes and the diagnostics token needed no change,
and the response's `share_scope` tells a client how to title the page.

### Deliberate simplifications

- **A moved turn's links stop working.** Moving a turn deletes its rows and writes new ones, so a
  message link's row is gone and it reads as unavailable. Moves are rare, and the alternative is
  rewriting links inside the move for a link the owner can recreate in two taps.
- **Shared messages are not listed.** An owner finds a message link from the message it shares, by
  opening the share action there. Home can hold many, and there is no "everything I've shared" page.
  A link stays valid until it is replaced or revoked, as conversation links do.
- **The web offers this on assistant replies, iOS on any message.** The web has no action bar on the
  user's own messages, and a new one is not justified by this feature. The server treats the two
  roles alike.
- **The Home header still shares all of Home.** It is an existing capability, now labelled as such,
  so nothing changes for a link already in use. The dialog points to the narrower actions.

## Authorization model

The URL carries a 256-bit random bearer token. The database stores only its SHA-256 digest. A
successful shared read requires both:

1. normal Family Assistant authentication through OIDC or another accepted application identity;
2. an active share row whose digest matches the supplied token.

Creating or revoking a link uses the existing sole-canonical-owner conversation check. Shared read
endpoints do not call the owner-only message endpoint and do not weaken its invariant.

Attachment downloads use a share-scoped endpoint. It serves a file only when the active share
matches and the attachment registry row names the shared conversation. This avoids granting the
recipient general access to the owner's attachment registry.

The shared read endpoints also accept the read-only diagnostics token in an `X-Diagnostics-Token`
header, so a conversation can be handed to a coding agent without being exposed publicly. Public
access stays gated at the edge: Envoy Gateway still requires a JWT on these routes unless that
header is present, and then verifies the header against its own copy of the diagnostics token before
the backend checks it again. The agent needs both secrets, the share token picking the conversation
and the diagnostics token proving it is an agent the household runs.

The token is intentionally sufficient for any authenticated household user. Shares do not target a
specific recipient, expire automatically, or provide an audit trail; those controls would add
machinery beyond the stated household privacy boundary. Rotating or revoking the link is the
recovery mechanism if it is exposed accidentally.

## Storage and API

`conversation_shares` contains one row per conversation:

- `conversation_id` (primary key)
- `owner_user_id`
- `token_hash` (unique SHA-256 hex digest)
- `created_at`

`conversation_message_shares` has the same shape keyed on `message_id`, the row's `internal_id`,
with `conversation_id` recording where the row must still be. There is no foreign key: a link to a
row that has gone is an unavailable link, not an integrity error.

Endpoints:

- `POST /api/v1/chat/conversations/{conversation_id}/share` rotates and returns the link.
- `GET /api/v1/chat/conversations/{conversation_id}/share` reports whether a share is active.
- `DELETE /api/v1/chat/conversations/{conversation_id}/share` revokes it.
- `POST`, `GET` and
  `DELETE /api/v1/chat/conversations/{conversation_id}/messages/{message_id}/share` do the same for
  one message.
- `GET /api/v1/shared-conversations/{token}/messages` returns what the link shares, and a
  `share_scope` of `conversation` or `message`.
- `GET /api/v1/shared-conversations/{token}/attachments/{attachment_id}` serves a scoped file.

The owner endpoints declare the normal current-user dependency; the shared read endpoints declare
the diagnostics-reader dependency, which is a normal user or the diagnostics token. Invalid,
revoked, foreign, and malformed tokens return 404 so the API does not distinguish why access failed.
