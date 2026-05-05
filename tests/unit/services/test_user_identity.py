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
