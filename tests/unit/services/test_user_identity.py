from __future__ import annotations

from pydantic import ValidationError
from starlette.datastructures import FormData

from family_assistant.config_models import AppConfig
from family_assistant.services.user_identity import (
    UserIdentityResolutionError,
    UserIdentityResolver,
)


def _config_with_user() -> AppConfig:
    return AppConfig.model_validate({
        "users": [
            {
                "id": "andrew@example.com",
                "label": "Andrew",
                "oidc": {
                    "emails": ["Andrew <Andrew@Example.com>"],
                    "subjects": ["keycloak-subject"],
                },
                "telegram": {"user_ids": [123456789], "developer": True},
                "email_intake": {
                    "sender_addresses": ["orders@gmail.com"],
                    "recipient_addresses": ["assistant+andrew@example.net"],
                },
            }
        ],
        "email_intake": {"require_user_mapping": True},
    })


def test_resolves_oidc_telegram_and_email_to_same_canonical_user() -> None:
    resolver = UserIdentityResolver(_config_with_user())

    assert (
        resolver.resolve_oidc_user({
            "email": "andrew@example.com",
            "sub": "keycloak-subject",
        }).user_id
        == "andrew@example.com"
    )
    assert resolver.resolve_telegram_user(123456789).user_id == "andrew@example.com"
    assert resolver.is_developer_telegram_user(123456789) is True
    assert (
        resolver.resolve_email_intake_user(
            FormData({"sender": "orders@gmail.com", "recipient": "unused@example.net"})
        )
        == "andrew@example.com"
    )


def test_resolved_identities_carry_configured_label() -> None:
    resolver = UserIdentityResolver(_config_with_user())

    assert (
        resolver.resolve_oidc_user({
            "email": "andrew@example.com",
            "sub": "keycloak-subject",
        }).label
        == "Andrew"
    )
    assert resolver.resolve_api_token_user("andrew@example.com").label == "Andrew"
    assert resolver.resolve_telegram_user(123456789).label == "Andrew"
    assert resolver.get_user_label("andrew@example.com") == "Andrew"


def test_canonicalize_owner_id_maps_aliases_to_canonical_user() -> None:
    resolver = UserIdentityResolver(_config_with_user())

    # A Telegram numeric owner id resolves to the same canonical user as the
    # web/OIDC session, so an activity ping scoped to it reaches the web
    # subscriber.
    assert resolver.canonicalize_owner_id("123456789") == "andrew@example.com"
    # The canonical id is its own canonical form.
    assert resolver.canonicalize_owner_id("andrew@example.com") == "andrew@example.com"
    # Unresolvable ids are returned unchanged so distinct unknown owners stay
    # distinct.
    assert resolver.canonicalize_owner_id("999") == "999"
    assert (
        resolver.canonicalize_owner_id("stranger@example.com") == "stranger@example.com"
    )


def test_canonicalize_owner_id_resolves_numeric_oidc_subject() -> None:
    # A numeric id that is NOT a configured Telegram id may still be a numeric
    # OIDC subject: canonicalization must fall through to the API/OIDC lookup
    # rather than returning the raw numeric id.
    config = AppConfig.model_validate({
        "users": [
            {
                "id": "andrew@example.com",
                "oidc": {"subjects": ["100200300"]},
                "telegram": {"user_ids": [123456789]},
            }
        ]
    })
    resolver = UserIdentityResolver(config)

    assert resolver.canonicalize_owner_id("100200300") == "andrew@example.com"
    # Still a configured Telegram id where applicable.
    assert resolver.canonicalize_owner_id("123456789") == "andrew@example.com"
    # An unknown numeric id stays raw.
    assert resolver.canonicalize_owner_id("555") == "555"


def test_owner_ids_inverse_respects_cross_namespace_collision_precedence() -> None:
    # "777" is Bob's Telegram id AND Alice's OIDC subject. The canonicalizer
    # resolves it to Bob (Telegram precedence), so the inverse equivalence set
    # must include it only for Bob — otherwise the DB ownership filter would
    # expose Bob's conversation summaries to Alice.
    config = AppConfig.model_validate({
        "users": [
            {
                "id": "alice@example.com",
                "oidc": {"subjects": ["777"]},
            },
            {
                "id": "bob@example.com",
                "telegram": {"user_ids": [777]},
            },
        ]
    })
    resolver = UserIdentityResolver(config)

    assert resolver.canonicalize_owner_id("777") == "bob@example.com"
    assert "777" in resolver.owner_ids_canonicalizing_to("bob@example.com")
    assert "777" not in resolver.owner_ids_canonicalizing_to("alice@example.com")
    assert "alice@example.com" in resolver.owner_ids_canonicalizing_to(
        "alice@example.com"
    )


def test_label_is_none_when_not_configured() -> None:
    config = AppConfig.model_validate({
        "users": [
            {
                "id": "no-label@example.com",
                "oidc": {"emails": ["no-label@example.com"]},
            }
        ]
    })
    resolver = UserIdentityResolver(config)

    resolved = resolver.resolve_oidc_user({"email": "no-label@example.com"})
    assert resolved.label is None
    assert resolver.get_user_label("no-label@example.com") is None


def test_blank_label_is_normalized_to_none() -> None:
    config = AppConfig.model_validate({
        "users": [
            {
                "id": "blank@example.com",
                "label": "   ",
                "oidc": {"emails": ["blank@example.com"]},
            }
        ]
    })
    resolver = UserIdentityResolver(config)

    assert resolver.get_user_label("blank@example.com") is None


def test_rejects_unknown_telegram_user_when_users_configured() -> None:
    resolver = UserIdentityResolver(_config_with_user())

    try:
        resolver.resolve_telegram_user(999)
    except UserIdentityResolutionError as exc:
        assert "not mapped" in str(exc)
    else:
        raise AssertionError("unknown Telegram user should be rejected")


def test_rejects_duplicate_external_identities() -> None:
    try:
        AppConfig.model_validate({
            "users": [
                {
                    "id": "one@example.com",
                    "oidc": {"emails": ["same@example.com"]},
                },
                {
                    "id": "two@example.com",
                    "oidc": {"emails": ["same@example.com"]},
                },
            ]
        })
    except ValidationError as exc:
        assert "same@example.com" in str(exc)
    else:
        raise AssertionError("duplicate external identities should be rejected")


def test_legacy_telegram_identity_is_used_when_users_are_not_configured() -> None:
    config = AppConfig.model_validate({"allowed_user_ids": [123]})
    resolver = UserIdentityResolver(config)

    assert resolver.resolve_telegram_user(123).user_id == "123"


def test_api_token_resolution_rejects_conflicting_legacy_identifier() -> None:
    config = AppConfig.model_validate({
        "users": [
            {
                "id": "alice@example.com",
                "oidc": {"emails": ["alice@example.com"]},
            },
            {
                "id": "bob@example.com",
                "oidc": {"subjects": ["alice@example.com"]},
            },
        ]
    })
    resolver = UserIdentityResolver(config)

    try:
        resolver.resolve_api_token_user("alice@example.com")
    except UserIdentityResolutionError as exc:
        assert "multiple configured users" in str(exc)
    else:
        raise AssertionError("conflicting API token owner should be rejected")
