# Local DKIM/SPF/DMARC Verification for Inbound Email

## Status

Proposed / In Progress (April 2026).

Replaces the Mailgun-header-based email authentication checks previously described in
[Secure Email Intake Actions](secure-email-intake-actions.md).

## Background

The inbound email webhook (`POST /webhook/mail`) previously trusted Mailgun's parsed
`dmarc`/`spf`/`dkim` form fields and the `Authentication-Results` header embedded in
`message-headers` to decide whether a sender was authenticated. In practice Mailgun does not
reliably populate these fields, so messages that should fail DMARC can slip past as "missing" rather
than "fail" and any strict policy either lets everything through or rejects everything.

## Goals

- Verify DKIM signatures cryptographically against DNS-published public keys.
- Evaluate DMARC policy for the visible `From:` domain using DNS.
- Make the verification deterministic and testable without live DNS.
- Drop the dependency on Mailgun-emitted authentication results.
- Preserve existing allowlisting, user-mapping, and indexing behaviour.

## Non-Goals

- Implementing our own DKIM/ARC/DMARC from scratch — use maintained libraries.
- Performing live DNS lookups during the HTTP request path in tests — inject a resolver.
- Replacing Mailgun for message delivery; only the authentication evaluation moves in-house.
- Fully featured SPF semantics. We support SPF opportunistically via `authheaders` when a client IP
  is discoverable from `Received` headers, but DMARC can pass on aligned DKIM alone.

## Mailgun Configuration Change

Mailgun must be configured to forward the raw RFC 822 message. This is done by using the "forward to
URL" route action with the **MIME-type forwarding option** (or by calling the Mailgun routes API
with `action = "forward('https://.../webhook/mail', 'mime')"`). With this option the webhook body
includes a `body-mime` multipart field containing the original raw MIME bytes. Without `body-mime`
we cannot verify DKIM.

The migration procedure is:

1. Update Mailgun routes to include the MIME-type forwarding option.
2. Deploy this change (the code tolerates both, logging a warning if `body-mime` is missing).
3. Once the warning is no longer observed, enable `require_authenticated_sender`.

## Implementation

### Dependencies

- `dkimpy` — DKIM signature verification (pure Python, permits an injectable DNS resolver).
- `authheaders` — wrapper that runs DKIM + SPF + DMARC and returns a structured result.
- `pyspf` — SPF evaluation used by `authheaders`.
- `dnspython` — DNS lookups (already a transitive dependency but declared explicitly).

### New module: `src/family_assistant/email_intake/authentication.py`

Provides:

- `EmailAuthenticationResult` dataclass with `dkim`, `spf`, `dmarc` fields (each a normalised
  string: `"pass"`, `"fail"`, `"none"`, `"neutral"`, `"temperror"`, `"permerror"`). Includes
  `dmarc_aligned_with_from` and details for logging.
- `verify_email_authentication(raw_mime, *, envelope_from, client_ip, helo, dns_resolver=None)` runs
  DKIM and DMARC on the supplied raw MIME bytes. SPF is run only when a client IP is available.
- `DnsResolver` protocol so tests can inject a fake resolver (maps `(name, rdtype)` to TXT records).

### Updated module: `src/family_assistant/email_intake/security.py`

- The old `SenderAuthentication` dataclass and `extract_sender_authentication` helper (which read
  Mailgun form fields / `Authentication-Results`) are removed.
- `verify_sender_authorization(form_data, config, *, raw_mime=None, dns_resolver=None)` now
  evaluates DMARC locally by delegating to `verify_email_authentication` when
  `require_authenticated_sender` is set.
- A new `extract_raw_mime(form_data)` helper pulls the raw message bytes out of the `body-mime` form
  field (handling `str`/`bytes` surrogateescape conversion).
- The old config options `require_dmarc_pass` and `allow_spf_or_dkim_fallback_when_dmarc_missing`
  are removed; if `require_authenticated_sender` is set we require a DMARC `pass` (aligned DKIM or
  aligned SPF) and otherwise we skip verification.

### Updated webhook handler: `src/family_assistant/web/routers/webhooks.py`

- After `verify_mailgun_signature`, read `body-mime` from the form.
- If absent and `require_authenticated_sender` is set, return HTTP 401 with a clear error that names
  the missing field.
- If absent and authentication is not required, log a warning and skip verification (preserves
  indexing-only deployments).
- Pass the raw MIME bytes to `verify_sender_authorization` and `resolve_target_user_id`.

### Client IP / envelope-from extraction

- `envelope_from` is Mailgun's `sender` form field.
- `client_ip` and `helo` come from the topmost third-party `Received` header in the raw MIME (i.e.
  the `Received` added by Mailgun, which records the peer SMTP client). The
  `email_intake.authentication` helper parses this with regex but prefers the Mailgun-provided
  `X-Mailgun-Sending-Ip` / `X-Envelope-From` headers when present.

### Storage

Add columns to `received_emails` to preserve the verification outcome:

- `dkim_result TEXT NULL`
- `spf_result TEXT NULL`
- `dmarc_result TEXT NULL`
- `auth_details_json JSONB NULL`

Alembic migration: `2026_04_21-add_email_auth_results.py`.

### Tests

Rewrite `tests/functional/web/api/test_mail_webhook_security.py` to build real DKIM-signed messages
using `dkimpy.sign` with a test RSA key, and inject a fake DNS resolver that serves the matching
`selector._domainkey.<domain>` record. DMARC is tested via matching `_dmarc.<domain>` records. A
small helper in `tests/mocks/email_auth.py` provides `build_signed_message(...)` and
`FakeDnsResolver`.

The test matrix covers:

- DKIM-signed, DMARC-aligned message → accepted.
- DKIM signature invalid (body tampered) → rejected.
- DMARC policy missing → rejected when authentication required.
- DMARC aligned via SPF only (no DKIM) → accepted when client IP matches SPF record.
- Missing `body-mime` with `require_authenticated_sender` → rejected with a helpful error.
- Missing `body-mime` without `require_authenticated_sender` → accepted (indexing path preserved,
  rejection logged).

## Rollout

1. Merge the library change with `require_authenticated_sender: false` (default).
2. Update Mailgun routes to forward MIME.
3. Observe logs for "missing body-mime" warnings; once they stop, flip
   `require_authenticated_sender: true` in deployment config.
