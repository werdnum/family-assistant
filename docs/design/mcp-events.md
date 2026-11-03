# MCP server events as an automation source

## Goal

Let event automations react to changes announced by MCP servers that implement the
[Events extension](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415)
(SEP-3415, `io.modelcontextprotocol/events`) on protocol revision 2026-07-28. Tuit is the first
producer: "tell me when something lands in my Tuit inbox" becomes an ordinary automation.

## The extension, briefly

A server lists event types (`events/list`), each with a subscription `inputSchema`, a
`payloadSchema` and the delivery modes it offers: **poll** (`events/poll` with an opaque cursor),
**push** (`events/stream`, a long-lived SSE request) and **webhook** (`events/subscribe`, signed
callbacks to a public HTTPS URL). None is mandatory. The 2026-07-28 revision is stateless: every
request carries its protocol version, so a poll is one self-contained JSON-RPC POST. The extension
is still a draft.

## Approach

- **One `mcp` event source, poll delivery only.** Polling connects outbound, so nothing has to be
  reachable from the internet and no path is exempted from the edge's authentication. Webhook
  delivery would need exactly such an exemption, and push is not offered by Tuit; either can be
  added behind the same source later.
- **The automation is the subscription.** A listener on `mcp` names what it watches in its match
  conditions (`server_id`, `event`, `arguments.<name>`); any other condition filters delivered
  events as on every other source. The source derives its set of subscriptions from the enabled
  listeners on a short reconcile loop, so creating, disabling or deleting an automation starts or
  stops polling wherever that change was made (tool, web UI, script) with no hook at each creation
  path. The agent's part is discovery (`list_mcp_events`) and an ordinary `create_automation`.
- **Polled as the creator.** A subscription is keyed by server, event, arguments and the listener's
  creator, and polls with that person's credential for the server (their `user_auth` token, identity
  check included, or the server's shared token). The processor delivers each polled event only to
  the listeners that own its subscription, so two people watching the same event never see each
  other's.
- **Resumable.** Each subscription's cursor is stored after its page is processed, so a restart
  resumes where it stopped. A new subscription starts from "now", and so does one whose automations
  were all disabled or deleted and later come back: a cursor no enabled listener names is forgotten.
- **Validated at creation against the live server.** `create_automation` and `update_automation`
  check that the server is an HTTP server the user has a credential for, that the event exists and
  is pollable, and that the arguments satisfy its `inputSchema`.
- **Own client.** The pinned `mcp` SDK predates 2026-07-28; a small httpx client speaks
  `server/discover`, `events/list` and `events/poll` until the SDK does.

## Deliberate simplifications

- **At-least-once.** A crash between processing a page and storing its cursor replays that page.
  Duplicate wakes are rare and harmless next to the machinery exactly-once would need.
- **Lost events are logged, not surfaced.** A `truncated` page (the server dropped history) logs a
  warning rather than notifying anyone.
- **Arguments are scalars.** Match conditions hold scalar values, so subscription arguments are too.
  Every event type Tuit offers fits.
- **Event payloads are trusted only by operator declaration.** A woken turn treats the payload as
  untrusted, exactly like Home Assistant or webhook events, and runs as `event_handler`, unless the
  server is configured with `trusted_events`. Then a listener's `wake_llm` runs under its creating
  profile and the payload adds no taint, matching how the server's `output_trusted` tool results are
  graded. The flag is per server, not per event type or per payload author. A script action's
  `wake_llm()` still runs as `event_handler` because nothing has needed it.
- **Who may watch what is decided by credentials.** Any profile that can create automations may
  watch any server its user has a credential for; there is no per-profile server allowlist for
  events.
- **Strict where data could be lost, tolerant elsewhere.** A malformed poll page is refused so its
  cursor never moves past events FA did not deliver. A malformed `events/list` entry (or schema) is
  skipped or defaulted instead, because nothing is lost: the server's bug shows up as an event that
  cannot be watched, which validation reports by name.
- **Errors surface in logs.** A subscription whose poll fails (revoked token, server down) backs off
  and retries, and the failure is logged; the automation is not disabled.

## Milestones

1. Poll source, cursor storage, `list_mcp_events`, trigger validation, docs. Verified by functional
   tests against a fake Events server: delivery, no loss or replay across a restart, isolation
   between users, filtering, and starting/stopping with the automation.
2. Only if a server needs it: push or webhook delivery, the latter behind a hardened edge component.
