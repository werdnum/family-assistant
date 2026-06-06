# Unified User Identities

## Problem

Durable tool confirmations are authorized by `target_user_id`, but each interface currently chooses
that value independently:

- Web auth uses the OIDC subject or API token owner.
- Telegram uses the numeric Telegram user id.
- Email intake has a separate sender/recipient mapping.

That means a confirmation created from Telegram is invisible in the web UI unless the OIDC user id
happens to equal the Telegram id. It also leaves operators configuring the same human user in
several places.

## Design

Add a top-level `users` config block as the source of truth for application users:

```yaml
users:
  - id: "andrew@example.com"
    label: "Andrew"
    oidc:
      emails: ["andrew@example.com"]
      subjects: []
    telegram:
      user_ids: [123456789]
      developer: true
    email_intake:
      sender_addresses: ["andrew@gmail.com"]
      recipient_addresses: ["assistant+andrew@example.net"]
```

The `id` is the canonical user identifier stored on durable confirmations, API tokens, push
subscriptions, conversation ownership fields, and other user-scoped rows. For the current single
installation, using the normalized Keycloak email as the canonical id is the lowest-friction
operator model and aligns with existing OIDC/email allowlists.

When `users` is configured:

- OIDC sessions resolve by configured email or subject.
- Telegram messages resolve by configured Telegram user id.
- Email intake resolves by configured sender or recipient addresses.
- Unknown OIDC, Telegram, or email identities are rejected at the interface boundary.

When `users` is empty, legacy behavior remains for local development and existing tests:

- OIDC/API tokens keep using the existing subject/email value.
- Telegram uses `allowed_user_ids` and numeric Telegram ids.
- Email intake uses `email_intake.user_mappings`.

This keeps the migration incremental without preserving two production identity systems once
operators move to `users`.

## Confirmation Flow

Durable confirmations continue to use the existing `confirmation_requests.target_user_id` column.
The important change is only which value is written:

1. Telegram receives an authorized update from Telegram user `123456789`.
2. The identity resolver maps it to canonical user `andrew@example.com`.
3. Tool confirmation is stored with `target_user_id = "andrew@example.com"`.
4. Web auth maps the Keycloak session for `andrew@example.com` to the same canonical user.
5. The web pending-confirmations endpoint lists and approves the Telegram-created confirmation.

No new confirmation entity is introduced.

## Operator Migration

Operators should move values from the old scattered fields into `users`:

- `allowed_user_ids` -> `users[].telegram.user_ids`
- `developer_chat_id` -> `users[].telegram.developer: true`
- `email_intake.user_mappings` -> `users[].email_intake`

Existing email sender and recipient allowlists remain available as security policy. User mapping
answers "which application user owns this email"; allowlists answer "which inbound addresses are
accepted at all".

## Tests

The implementation should verify:

- OIDC email and Telegram id resolve to the same canonical user.
- A Telegram-created durable confirmation appears in the web pending-confirmations endpoint for the
  matching OIDC user.
- Web approval/rejection of that Telegram-created confirmation is authorized.
- Email intake maps sender/recipient identities through the same canonical user config.
- Unknown Telegram users and ambiguous email mappings are rejected.
