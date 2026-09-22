# On-demand browser credential requests

## Decision

A browser agent may navigate to a URL, encounter a login wall, and request a Keychute secret by
name. The user can supply that name in the task or answer when the agent asks. Neither an
`authenticated_sites` entry nor a standing grant is a prerequisite. Keychute decides each release; a
standing grant can later make the same workflow unattended and restrict the credential to
appropriate origins.

This replaces the assumption in `authenticated-site-capabilities.md` that every autofill request
must originate from an operator-configured account capability. Existing configured sites remain
optional presets with their account and origin bindings intact. They do not define the authority of
ordinary browsing.

## Approach

The ordinary semantic and visual browser profiles expose `browser_autofill` with a `secret_name`.
Browser-server receives the name and resolves the actual current HTTPS document origin, requests the
credential from Keychute, checks the granted destination and the still-current form, and fills it
directly. The agent submits the form and continues browsing. FA receives outcomes, never credential
bytes.

Remote FA browsers are credential-protected from creation. Existing masking and input protections
apply; arbitrary JavaScript and raw DOM extraction are denied before any fill, so the agent cannot
install a listener first. Public navigation and cross-site browsing remain available. Local
Playwright browsers cannot fill Keychute credentials.

Approval that outlasts a tool call returns the existing pending result and its approval link. The
conversation keeps the browser session and request key so a user can approve and ask the agent to
continue. There is no new background approval service: expiry or restart requires opening the task
again. Configured site runs retain their existing parked-run handling.

## Deliberate simplifications and accepted trade-offs

- Runtime Keychute approval replaces mandatory FA site provisioning. A model can request a different
  named secret; that request is not itself authorization.
- Standing grants authorize the browser-server Keychute client, not an individual FA user.
  Acting-user context is advisory audit information. Household users able to browse share this
  client's standing-grant authority. Do not describe this path as enforcing per-user account
  isolation. Deployments needing that isolation should use separate credential clients or the
  configured-site path.
- Origin constraints govern credential release, not all subsequent browser activity. The agent may
  navigate elsewhere and may act on authenticated pages. Same-site errors, prompt-injected actions
  and cross-site disclosure of account content are accepted residuals of general browsing, not
  promises to solve in this change. Existing action-review and taint mechanisms remain applicable.
- Read-back protection is best effort. An approved site necessarily receives its own password and
  could echo it; masking known credential controls does not claim to make a hostile approved site
  safe.
- No vault enumeration, automatic secret-name discovery, new MFA system, or automatic session
  persistence is required. Ask for a name when it is unknown; use human handoff for challenges the
  agent cannot complete.
- Existing bad-password and per-session fill limits remain bounded failure handling. Reporting a
  rejected password discards an ordinary browser session; a later user-requested task can open a
  fresh session in the same conversation after the stored secret is corrected.

These are explicit product choices for the requested workflow, not missing static-site validation to
add during review.

## Milestones and verification

1. Browser-server accepts per-request secrets in credential-protected ordinary sessions. Verify
   pending approval and retry, granted-origin mismatch, navigation during approval, and denial of
   raw readback before the first fill.
2. FA exposes on-demand requests in both browser profiles and preserves pending request identity
   across turns. Verify against the actual browser-server ASGI application with a fake Keychute
   transport, and verify policy availability.
3. Publish user/operator guidance and matching PRs. Pin FA's integration-test dependency to the
   browser-server change and check both current-head CI runs.
