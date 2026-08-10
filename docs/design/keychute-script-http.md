# Keychute-Brokered HTTP in Scripts

## Goal

Allow a Monty script to call an authenticated HTTPS API without directly releasing the credential to
the script process, LLM context, tool arguments, or logs. The feature is operator opt-in.

## Design

Family Assistant exposes `keychute_http_request()` only when `keychute_config.enabled` is true. The
function launches the installed `keychute curl` command directly, without a shell. Keychute remains
the authority for request constraints, approval, grant validation, credential injection, proxy
header filtering, and redirect refusal.

The wrapper accepts a secret name, URL, method, headers, body, reason, TTL, use count, approval
timeout, and upstream timeout. It requests `--include` output and converts the byte-faithful result
into a script dictionary containing the status code, repeated headers, and body bytes. Upstream HTTP
error statuses are data; Keychute or transport failures raise an explicit script exception.

For every invocation, Family Assistant sets `KEYCHUTE_CONTEXT` to the exact executing script source.
The Keychute CLI adds that context to its access request, so an approving operator can review the
code that will consume the response. Inline, stored, and scheduled scripts all use the same wrapper.

## Boundaries

- The function does not exist when disabled; scripts retain their default no-network sandbox.
- The credential stays server-side in Keychute. Family Assistant holds only the client credential
  needed to authenticate to Keychute, through the CLI's existing environment contract.
- Keychute returns the upstream response verbatim. The operator must approve only trusted origins: a
  hostile upstream can reflect credential-bearing request data in its response.
- The wrapper never uses a shell and does not log the command arguments or body.
- Stdout, stderr, and the upstream response body are bounded before being accumulated in memory.
- A returned upstream response enters the turn's runtime-taint tracker as untrusted external
  content, so subsequent script tool calls remain subject to Rule of Two enforcement.
- A missing executable, invalid Keychute environment, denial, expiry, timeout, or malformed response
  fails the script rather than falling back to direct HTTP.
- Event condition scripts remain isolated because they have no execution context and never receive
  this API.
