# Conversation Share Links

## Goal

Let the owner of a web conversation give another authorized Family Assistant user a link to a
read-only view. The link is a privacy speedbump between household members, not an authorization
substitute: every request still requires an authenticated, allowlisted user.

## User experience

- A persisted conversation has a **Share** action in the chat header.
- Sharing rotates the conversation's one active link and copies the replacement URL.
- The owner can stop sharing, immediately invalidating the link.
- A recipient opens `/shared/conversations/{token}` and sees a dedicated read-only transcript.
- Shared conversations never appear in the recipient's conversation list and cannot be continued,
  steered, or used to approve tool calls.
- The transcript is live at read time: later persisted messages appear after a refresh.

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

Endpoints:

- `POST /api/v1/chat/conversations/{conversation_id}/share` rotates and returns the link.
- `GET /api/v1/chat/conversations/{conversation_id}/share` reports whether a share is active.
- `DELETE /api/v1/chat/conversations/{conversation_id}/share` revokes it.
- `GET /api/v1/shared-conversations/{token}/messages` returns the visible main-conversation rows.
- `GET /api/v1/shared-conversations/{token}/attachments/{attachment_id}` serves a scoped file.

All endpoints declare the normal current-user dependency. Invalid, revoked, foreign, and malformed
tokens return 404 so the API does not distinguish why access failed.
