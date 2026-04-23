"""Unit tests for helpers in :mod:`family_assistant.web.routers.debug_api`.

These tests exercise the internal redaction helpers directly. The functional
endpoint behavior lives in ``tests/functional/web/api/test_debug_api.py``.
"""

from family_assistant.web.routers.debug_api import (
    SENSITIVE_FIELD_NAMES,
    is_sensitive_field_name,
    redact_sensitive_config,
)


def test_allowlisted_field_names_are_sensitive() -> None:
    for name in SENSITIVE_FIELD_NAMES:
        assert is_sensitive_field_name(name), name


def test_substring_fallback_catches_future_secret_field_names() -> None:
    """Field names added later with secret-looking substrings are redacted by fallback."""
    for name in (
        "my_custom_api_key",
        "APIKey",
        "oauth_secret",
        "bearer_token",
        "some_password_hash",
        "service_private_key",
    ):
        assert is_sensitive_field_name(name), name


def test_non_secret_field_names_are_not_redacted() -> None:
    for name in (
        "llm_model",
        "provider",
        "description",
        "timezone",
        "max_iterations",
        "enable_local_tools",
    ):
        assert not is_sensitive_field_name(name), name


def testredact_sensitive_config_walks_nested_structures() -> None:
    input_payload = {
        "home_assistant_token": "ha-123",
        "nested": {
            "my_custom_api_key": "hunter2",
            "camera": {"password": "p4ss"},
            "list_of_dicts": [
                {"oauth_secret": "shh"},
                {"unrelated_value": "visible"},
            ],
        },
        "empty_token": "",  # empty/falsy secrets are left as-is
        "unrelated": "visible",
    }
    redacted = redact_sensitive_config(input_payload)
    assert redacted["home_assistant_token"] == "[REDACTED]"
    assert redacted["nested"]["my_custom_api_key"] == "[REDACTED]"
    assert redacted["nested"]["camera"]["password"] == "[REDACTED]"
    assert redacted["nested"]["list_of_dicts"][0]["oauth_secret"] == "[REDACTED]"
    assert redacted["nested"]["list_of_dicts"][1]["unrelated_value"] == "visible"
    # Falsy secret values stay falsy (no "[REDACTED]") since there's nothing to leak.
    assert not redacted["empty_token"]
    assert redacted["unrelated"] == "visible"
