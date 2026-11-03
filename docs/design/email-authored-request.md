# The sender's own words as an email turn's request

## Problem

Every email-intake turn reached the model as one user message tagged `unknown_external`, even when
it came from a household member's own address with DMARC passing. Tool-call review judges a call
against the human request a turn answers, and it only sees a request carrying the `trusted_user`
stamp. Email turns had none, so the reviewer could only escalate. The email profile therefore gated
every write behind a durable confirmation, including the common case of "add this to the calendar"
typed above a forwarded invitation.

## Approach

Treat the text the authenticated household sender typed as their request, and everything their mail
client added (the quoted thread, a forwarded message, their signature) plus the attachments as
untrusted material.

- **Who qualifies.** The intake runs in strict mode (`require_authenticated_sender`), where the
  webhook binds the DMARC pass to the envelope sender. The envelope sender must be explicitly mapped
  to the user the email is for. Mail accepted through a recipient-only mapping, or in permissive
  mode where a stored DMARC result is only telemetry, keeps the old single untrusted row.
- **What counts as authored.** Gmail's own markup decides: `div.gmail_quote` (reply quotes and
  forwarded messages) and `div.gmail_signature` are removed from the HTML part. Without an HTML
  part, the plain body is cut at the `On … wrote:` attribution, a `>` quote, the forwarded-message
  banner, or the `-- ` signature separator. A subject counts unless it carries a reply or forward
  prefix.
- **How the turn is shaped.** The whole email, framed exactly as before inside
  `<untrusted_email_evidence>`, is written first, as a row of the same turn stamped with the email's
  taint. The authored text then becomes the turn's trigger, stamped `trusted_user`. The turn still
  inherits the email's taint from its window, so every taint-policy decision is unchanged. What
  changes is that the reviewer sees the sender's request and gets a stub for the evidence row.
- **Policy.** The email profile's write rules move from `confirm` to `review`. A bare forward has no
  trusted request, so its calls escalate to confirmation as before.

## Deliberate simplifications

- **Gmail's default formatting only.** Inline replies interleaved with quotes, other mail clients'
  quoting, and hand-edited markup are not handled: text that survives extraction is treated as
  authored. The sender is a DMARC-authenticated household member, so the adversary who could exploit
  a parsing gap is the household member's own mail client. The reviewer still judges every write
  against the request, with confirmation as its fallback.
- **Mailgun's `stripped-text` is not used.** The intake route asks Mailgun for raw MIME, which DKIM
  and DMARC verification need. On that route Mailgun posts `body-mime` instead of its parsed fields,
  so `stripped-text` is a copy of the full body. Extraction runs at prompt-build time from the
  stored `body_html` / `body_plain`, so no schema change is needed.
- **A retried intake task writes a second evidence row,** as it already wrote a second trigger row.
