# Browser Credential Broker Design

## Status

Draft for review.

This document plans a human-in-the-loop credential autofill flow for browser automation tasks such
as "log into my supermarket account." The goal is to let Family Assistant obtain a scoped
authenticated browser session without exposing the password or Agent Access credential material to
the LLM.

The plan builds on three existing design directions:

- Processing profiles isolate tool surfaces for specialized work.
- The tool policy engine can allow, deny, or require confirmation by tool tags and names.
- Future taint tracking can dynamically restrict tools when a request or session contains untrusted
  or sensitive state.

## Problem

Some useful browser tasks require authentication. Today, browser automation can navigate, click,
fill fields, run page JavaScript, extract page content, and return screenshots. Giving the model a
generic password-fetching tool would be too broad, and filling a password into a page creates a
temporary high-risk state:

- The page's JavaScript can usually read a filled password field.
- A broader browser profile could accidentally inspect or expose the filled field through snapshots,
  screenshots, extraction, or arbitrary `browser_exec`.
- A partially completed login flow could leave the credential in the live DOM.

At the same time, a fully authenticated browser session is a different kind of state from a page
with a password field. Well-designed sites often rely on session cookies for normal actions and
require a fresh password prompt only for highly sensitive operations.

The clean boundary is therefore:

1. Use an isolated login broker profile to perform the credential fill and login submission.
2. Close the page/tab that ever held the password.
3. Retain only the browser context's authenticated storage, such as cookies and other normal site
   session state.
4. Open a fresh page in the same context and hand that scoped authenticated session to the ordinary
   browser workflow.

If the login broker cannot verify that authentication succeeded, it discards the entire browser
context.

## Threat Model

This design primarily protects against assistant-mediated exfiltration and accidental broad tool use
after a credential is injected.

It does not prevent a compromised or malicious approved origin from observing the credential during
login. Once a password is entered into a page, that page can normally read it. This is the same
class of risk as browser password-manager autofill, so the key control is strict origin matching
plus human approval before release.

Relevant risks:

- A prompt-injected page instructs the model to reveal or reuse a password.
- A login broker fills a password but fails to submit the form.
- A broader profile receives a live page whose DOM still contains the password.
- The assistant navigates an authenticated session to an unrelated origin.
- A password re-prompt appears later for a sensitive action.
- Agent Access credentials are not configured, but the assistant attempts a credential fill anyway.

## Browser Session States

The browser capability should distinguish these states:

```text
public_browser
  No credential has been injected and no authenticated session is known.

credential_in_field(origin, credential_id)
  A credential was injected into a live page for an approved origin.
  This state must never be handed to a general browser profile.

user_secret_in_field(origin, purpose)
  A user-supplied login challenge secret was injected into a live page
  or is awaiting external approval. This state must never be handed to
  a general browser profile.

authenticated_browser(origin)
  A fresh page is open in a context with authenticated storage for the
  approved origin. The page that held the password has been closed.

discarded
  Login failed, verification was inconclusive, or a policy invariant was
  violated. The whole browser context is closed.
```

`credential_in_field` and `user_secret_in_field` are not just tainted; they are non-transferable
states. The only allowed exits are promotion to `authenticated_browser(origin)` or discarding the
whole context.

## Login Broker Profile

Add a dedicated `browser_login_broker` processing profile. It receives only the site/origin to
authenticate to and a user-facing credential label or credential selector hint. It must not receive
arbitrary page content or a broad task description from the main assistant.

Example delegated request:

```json
{
  "origin": "https://www.supermarket.example",
  "credential_hint": "Supermarket"
}
```

The broker should not receive:

- the user's broader shopping request,
- page-extracted instructions,
- arbitrary HTML,
- password values,
- one-time codes,
- Agent Access tokens,
- a request to perform post-login shopping actions.

### Allowed Tools

The broker should have the smallest browser surface that can complete login:

- `browser_open`
- `browser_snapshot_redacted` or `browser_snapshot` only after the snapshot layer redacts all form
  values
- `browser_click`
- `browser_fill`
- `browser_wait`
- `browser_fill_credential`
- `browser_fill_user_supplied_secret`
- `browser_finalize_authenticated_session`
- `agent_access_status`

It may need a narrowly scoped close/discard tool:

- `browser_discard_login_context`

### Denied Tools

The broker should not have tools that can inspect, export, or communicate secrets:

- `browser_exec`
- `browser_extract`
- `browser_snapshot` while in `credential_in_field` or `user_secret_in_field`
- `browser_screenshot` while in `credential_in_field`
- `attach_to_response`
- code execution and worker tools
- general external communication tools
- delegation tools
- notes, documents, message history, calendar, and other sensitive family data

If screenshots are needed for human troubleshooting, they should be disabled after credential fill
and only re-enabled after the password-holding page is closed.

The current semantic DOM snapshot implementation copies form `value` fields from the page. The
broker therefore must not use the existing unredacted snapshot tool after any password, MFA code, or
other login challenge secret may have been entered. Before the broker profile is implemented, one of
these must be true:

- Add a broker-specific redacted snapshot tool that never returns form values.
- Change the common snapshot layer to redact password, one-time-code, backup-code, payment, and
  other sensitive field values before returning data to the model.

In either design, snapshots remain denied while the browser session is in `credential_in_field` or
`user_secret_in_field`. The broker can use redacted snapshots only before secret entry or after
finalization has closed the secret-holding page and opened a fresh authenticated page.

## Agent Access Gating

Credential fill must be blocked unless Agent Access is available and configured for the requested
origin.

Add a read-only setup/status tool:

```text
agent_access_status(origin: str) -> AgentAccessStatus
```

The tool should return only non-secret metadata:

```json
{
  "available": true,
  "paired": true,
  "credential_matches": [
    {
      "credential_id": "opaque-id",
      "label": "Supermarket",
      "origin": "https://www.supermarket.example",
      "fields": ["username", "password"]
    }
  ]
}
```

It must never return:

- password values,
- one-time codes,
- Agent Access pairing tokens,
- Bitwarden item raw fields,
- recovery codes,
- private keys.

If Agent Access is missing, unpaired, locked, or has no origin-matching credential,
`browser_fill_credential` is not advertised and cannot execute. The assistant should report that
credential autofill is not set up for that site.

Potential setup flow:

1. User asks to log into a site.
2. Broker calls `agent_access_status(origin)`.
3. If unavailable, broker explains the missing setup in final text and stops.
4. User separately pairs/configures Agent Access outside the LLM context.
5. The login request can be retried.

Pairing/setup tokens should not be exchanged through the assistant transcript unless the Agent
Access protocol explicitly supports a non-secret user-visible code. Even then, the code should be
handled by deterministic UI, not by an LLM tool result containing secret material.

## Credential Fill Tool

Add a broker-only tool:

```text
browser_fill_credential(
  origin: str,
  credential_id: str,
  username_ref: str | None,
  password_ref: str,
  submit_ref: str | None
) -> CredentialFillResult
```

Execution requirements:

- Current page origin must exactly match the approved origin or an explicit configured login origin.
- `credential_id` must come from `agent_access_status`; the model cannot provide arbitrary Bitwarden
  item IDs.
- Human approval is required at execution time. The prompt shows origin, credential label, and
  fields to fill, but not secret values.
- The tool retrieves the credential inside the tool process and fills the page directly through
  Playwright.
- The tool returns only metadata: status, origin, filled field kinds, and whether submit was
  attempted.
- The tool marks the browser context as `credential_in_field(origin, credential_id)`.

If `submit_ref` is provided, the tool can click it after filling. Otherwise the broker may click a
submit button as the next step, but the session remains `credential_in_field` until finalization
succeeds.

## User-Supplied Ephemeral Secrets

Not every login secret lives in Bitwarden or Agent Access. MFA flows often require the user to
provide a current TOTP, SMS code, email code, push approval, or backup code during the browser
session.

These should use the same core principle as password autofill: the LLM can ask for a code to be
collected and filled, but it must not receive the code value.

Add a broker-only tool:

```text
browser_fill_user_supplied_secret(
  origin: str,
  field_ref: str | None,
  purpose: Literal["totp", "sms_code", "email_code", "push_approval", "backup_code"],
  submit_ref: str | None,
  prompt: str
) -> UserSecretFillResult
```

Execution requirements:

- The browser must already be in an active broker login flow for the approved origin.
- The tool prompts the authenticated user through deterministic UI or a trusted confirmation
  channel, not through an LLM-visible chat message.
- The prompt must be constrained to the current origin and purpose, for example: "Enter the
  six-digit code for Supermarket."
- The collected value is passed directly from the trusted UI handler into the Playwright fill/click
  operation.
- The value is never returned to the LLM, written to logs, stored in message history, or included in
  tool result data.
- The result reports only metadata such as
  `{ "status": "filled", "purpose": "totp", "submitted": true }`.
- If the purpose is `push_approval`, the tool waits for user confirmation that they approved the
  prompt on their device rather than filling a field.
- The browser session remains non-transferable until finalization closes the page that received the
  secret and opens a fresh authenticated page.

This generalizes beyond Agent Access without creating a generic "ask the user for any secret" tool.
The broker can only ask for a login challenge secret for the currently approved origin. It cannot
ask for unrelated credentials, API keys, recovery material, or secrets for another site.

Backup codes deserve stricter UX than normal TOTP/SMS codes because they may be durable until used.
They should require explicit wording that a backup code is being consumed, and V1 may choose to keep
backup codes out of scope.

## Finalization Boundary

Add a broker-only tool:

```text
browser_finalize_authenticated_session(
  origin: str,
  auth_evidence: AuthEvidence
) -> AuthenticatedBrowserCapability
```

This tool is responsible for the hard boundary:

01. Verify the browser is in `credential_in_field(origin, credential_id)`.
02. Verify login was submitted.
03. Verify navigation, XHR auth flow, or visible authenticated state completed.
04. Verify the current URL remains inside the approved origin or an approved auth redirect chain.
05. Verify no visible/enabled password input remains populated.
06. Verify no visible/enabled MFA or backup-code input remains populated after any user-supplied
    secret was entered.
07. Verify positive authentication evidence, such as a logout link, account menu, account name, or
    site-specific authenticated-only selector.
08. Close the page/tab that held the password or any user-supplied login secret.
09. Open a fresh page in the same browser context at the approved origin.
10. Clear credential and user-secret fill metadata from the session object.
11. Return an `authenticated_browser(origin)` capability.

If any check fails, the tool closes the whole browser context and returns a failure without a
reusable session.

The returned capability should be scoped:

```json
{
  "kind": "authenticated_browser_session",
  "origin": "https://www.supermarket.example",
  "session_id": "opaque-session-id",
  "issued_by": "browser_login_broker",
  "secret_exposed_to_model": false
}
```

The main browser profile receives only this capability, never the original page object and never
credential metadata.

## Authenticated Browser Policy

An `authenticated_browser(origin)` session is still sensitive. Policy should enforce origin scoping
and action confirmation:

- Allow normal browsing and form filling only on the approved origin.
- Require confirmation for checkout, payment, order placement, address changes, account settings,
  password changes, and destructive actions.
- Deny or confirm cross-origin navigation.
- Deny `browser_exec` by default on authenticated sessions unless a later design proves a safe
  subset.
- Redact password and payment fields from snapshots.
- Treat password re-prompt as a transition back to `credential_in_field` and require a fresh
  broker-mediated fill.

This differs from `credential_in_field`: an authenticated session can be handed to a normal browser
profile, but only with origin-scoped policy.

## Relationship To Taint Tracking

This design should reuse the planned taint model but should not rely on generic taint alone.

Generic taint is useful for assembled LLM requests, tool outputs, and cross-turn history. Credential
handling needs a stricter browser-session capability model because a live page with a password field
is not safe to transfer even if policy would deny exfiltration tools.

Recommended additions to the deferred taint plan:

- Add browser-session sensitivity state to `ToolExecutionContext` or a browser session registry.
- Let policy rules match `browser_session_state` and `origin`.
- Treat `credential_in_field` as non-transferable.
- Treat `authenticated_browser(origin)` as transferable only to profiles with matching browser
  capability policy.
- Persist only opaque capability metadata, not credential IDs or secret values, in message history.

## Implementation Milestones

### Milestone 1: Design And Policy Model

- Add this design doc.
- Define the browser session state machine in code-level comments or a small internal model.
- Decide whether state lives on `BrowserSession`, `ToolExecutionContext`, or a separate browser
  capability registry.

### Milestone 2: Agent Access Status Gate

- Add `agent_access_status(origin)`.
- Add configuration for allowed origins and credential match metadata.
- Ensure no secret values appear in tool results or logs.
- Add tests for unavailable, unpaired, locked, no-match, and matched states.

### Milestone 3: Broker Profile And Policy

- Add `browser_login_broker` to `defaults.yaml`.
- Add tool policy tests enumerating allowed and denied tools.
- Ensure the profile is not exposed as a slash command unless explicitly enabled by the operator.
- Ensure delegation into this profile passes only origin and credential hint.

### Milestone 4: Credential Fill

- Implement `browser_fill_credential`.
- Require exact origin match and durable human approval.
- Fill credentials directly without returning secret values.
- Mark the session as `credential_in_field`.
- Deny snapshots, screenshots, extraction, JavaScript execution, and other sensitive browser tools
  while in this state.

### Milestone 5: Ephemeral User Secrets

- Implement `browser_fill_user_supplied_secret`.
- Collect TOTP/SMS/email codes through deterministic trusted UI, not model-visible chat.
- Fill or wait for the challenge directly without returning the secret value.
- Keep the broker session non-transferable until finalization.
- Deny snapshots, screenshots, extraction, JavaScript execution, and other sensitive browser tools
  while in this state.
- Decide whether backup codes are out of scope for V1.

### Milestone 6: Finalize Or Discard

- Implement `browser_finalize_authenticated_session`.
- Close the password-holding page on success.
- Open a fresh page in the retained browser context.
- Return an opaque authenticated browser capability.
- Discard the entire context on failure.

### Milestone 7: Authenticated Browser Handoff

- Allow the normal browser profile to accept `authenticated_browser(origin)` capabilities.
- Enforce origin scoping and confirmation for high-risk actions.
- Add tests for cross-origin navigation, password re-prompt, checkout-like confirmation, and context
  discard paths.

## Testing Strategy

Unit tests:

- Agent Access status never returns secret fields.
- Credential fill requires Agent Access availability and a matched origin.
- User-supplied MFA fill never returns the entered code in tool output, logs, or message history.
- Policy denies `browser_exec`, extraction, screenshots, delegation, external communication, and
  sensitive-data tools in the login broker.
- Policy denies snapshots while the broker session is in `credential_in_field` or
  `user_secret_in_field`.
- `credential_in_field` cannot be advertised or transferred as a browser capability.
- `user_secret_in_field` cannot be advertised or transferred as a browser capability.

Functional tests with local fixture pages:

- Successful login closes the original page and opens a fresh page in the same context with
  authenticated cookies retained.
- Failed login closes the whole context.
- Filled-but-not-submitted login cannot be finalized.
- A remaining populated password field prevents finalization.
- A remaining populated MFA field prevents finalization.
- Password re-prompt triggers a fresh broker flow.
- Cross-origin navigation from an authenticated browser is denied or requires confirmation.

Security regression tests:

- Tool outputs and logs do not contain username/password values.
- Tool outputs and logs do not contain user-supplied MFA or backup-code values.
- Broker-visible snapshots redact form values and are unavailable while a credential or user secret
  may be live in the page.
- The main assistant cannot call `browser_fill_credential`.
- The main assistant cannot call `browser_fill_user_supplied_secret`.
- Delegating from untrusted profiles to the broker is blocked unless explicitly allowed.

## Open Questions

- Should the broker support username-only fills, or should any credential fill require a password
  field?
- How should approved login redirect chains be configured for sites that use a separate identity
  provider?
- Should cookies and storage be exported/imported as a serialized capability, or should the live
  browser context remain in memory only?
- Should backup codes be explicitly out of scope for V1 even though TOTP/SMS/email codes are
  supported?
- Should `browser_exec` remain denied for all authenticated sessions, or can a limited read-only
  script subset be designed safely later?
