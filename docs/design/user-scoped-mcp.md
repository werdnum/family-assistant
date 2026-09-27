# User-scoped MCP connections

An HTTP MCP server can declare `user_auth` instead of a shared `token`. The trusted
`ToolExecutionContext.user_id` selects one configured environment-backed credential. Each canonical
user has a separate instance of the existing MCP connection lifecycle, including discovery, session,
reconnect backoff, and initialization instructions. Calls and health work are serialized within each
user's connection, while different users can run concurrently.

The first available connection supplies the common tool catalog. This mode is for servers such as
Tuit whose tool schemas and policy metadata are the same for all users. Initialization instructions
remain on the individual connection and are never published as global instructions. Execution always
dispatches to the acting user's connection, regardless of which connection supplied discovery.
Missing identity or credentials never fall back to another user.

An optional same-origin identity check verifies `user.id`, `agent`, and `can_write` before MCP
initialization, including on reconnect. Tuit already provides this at `/api/me`. Explicitly mapping
FA's canonical ID to the backend's expected ID supports differing identity namespaces and catches
swapped credentials before a tool is invoked. Redirects are not followed.

Credentials are environment references, not additional database records. Rotation in Kubernetes
replaces the Secret and restarts FA; reconnects also resolve the environment reference afresh. No
user-specific OAuth or identity-forwarding protocol is required for this integration.

## Deliberate scope and simplifications

- Reuse FA's existing user propagation, tool policy, taint handling, and downstream delivery.
  Household data is normally shared; private Tuit items are occasional exceptions. A broader privacy
  audit or shared-conversation redesign is explicitly outside this work.
- No notifications, change-feed consumers, or automatic task dispatch.
- No rewrite of shared-server reconnect/retry behavior. User-scoped calls have a bounded timeout; an
  expired call is not replayed automatically and reports that its outcome may be uncertain.
- Tool catalogs must be common across users; per-user tool availability is a separate feature.
- User credentials are operator-provisioned. A self-service OAuth connection UI can be added later
  without changing the execution-context identity boundary.
