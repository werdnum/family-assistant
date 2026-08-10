# Keychute-Brokered HTTP in Scripts

## Goal

Allow a Monty script to call an authenticated HTTPS API without directly releasing the credential to
the script process, LLM context, tool arguments, or logs. The feature is operator opt-in.

## Design

Family Assistant exposes `keychute_http_request()` only when `keychute_config.enabled` is true. The
function calls Keychute's client HTTP API directly. Keychute remains the authority for request
constraints, approval, grant validation, credential injection, proxy header filtering, and redirect
refusal.

The wrapper accepts a secret name, URL, method, headers, body, reason, TTL, use count, approval
timeout, and upstream timeout. The result is a script dictionary containing the status code,
repeated headers, and body bytes. Upstream HTTP error statuses are data; Keychute or transport
failures raise an explicit script exception.

Every access request carries the exact executing script source as structured context, so an
approving operator can review the code that will consume the response. Inline, stored, and scheduled
scripts all use the same client.

## Boundaries

- The function does not exist when disabled; scripts retain their default no-network sandbox.
- The credential stays server-side in Keychute. Family Assistant holds only the client credential
  needed to authenticate to Keychute. A configured token file is reread on every API request.
- Keychute returns the upstream response verbatim. The operator must approve only trusted origins: a
  hostile upstream can reflect credential-bearing request data in its response.
- Keychute API error bodies and the upstream response are bounded before being accumulated in
  memory.
- A returned upstream response enters the turn's runtime-taint tracker as untrusted external
  content, so subsequent script tool calls remain subject to Rule of Two enforcement.
- Before Family Assistant contacts Keychute, the current turn state is evaluated as a
  `sandbox_network` sink. Enforced deny or redaction outcomes stop the request before access-request
  creation; confirmation outcomes require the normal Family Assistant confirmation path in addition
  to any approval Keychute requires.
- The simulated-script harness does not expose brokered HTTP, so testing cannot reach an
  auto-approved grant or mutate an upstream service.
- Invalid Keychute configuration, denial, expiry, timeout, or a malformed response fails the script
  rather than falling back to direct HTTP.
- Event condition scripts remain isolated because they have no execution context and never receive
  this API.
