"""Unit tests for config redaction in :mod:`family_assistant.config_inspection`.

Field-name matching alone both leaks (credentials embedded inside URLs and
inline PEM blobs live under innocuous names like ``database_url``, ``url`` and
``auth_key``) and over-matches (``max_tokens``, ``token_file``, ``*_key_env``).
These tests pin both directions.
"""

from __future__ import annotations

import json

from family_assistant.config_inspection import (
    REDACTED,
    is_sensitive_field_name,
    redact_sensitive_config,
    redact_sensitive_text,
)
from family_assistant.config_models import (
    ApnsConfig,
    AppConfig,
    MCPConfig,
    MCPServerConfig,
)

PEM_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg\n"
    "-----END PRIVATE KEY-----\n"
)


def test_database_url_password_is_redacted_but_host_survives() -> None:
    """The DSN stays diagnostically useful with only the password removed."""
    redacted = redact_sensitive_config({
        "database_url": "postgresql+asyncpg://fa_user:hunter2@db.internal:5432/family"
    })
    assert redacted["database_url"] == (
        f"postgresql+asyncpg://fa_user:{REDACTED}@db.internal:5432/family"
    )


def test_url_without_credentials_is_untouched() -> None:
    for url in (
        "sqlite+aiosqlite:///family_assistant.db",
        "https://homeassistant.internal:8123/api",
        "postgresql+asyncpg://fa_user@db.internal:5432/family",
    ):
        assert redact_sensitive_text(url) == url


def test_credential_query_parameters_are_redacted() -> None:
    redacted = redact_sensitive_text(
        "https://www.searchapi.io/api/v1/mcp?token=live-secret&engine=google"
    )
    assert redacted == (
        f"https://www.searchapi.io/api/v1/mcp?token={REDACTED}&engine=google"
    )


def test_credential_query_parameters_are_redacted_case_insensitively() -> None:
    redacted = redact_sensitive_text("https://api.example.com/v1?API_KEY=abc&page=2")
    assert redacted == f"https://api.example.com/v1?API_KEY={REDACTED}&page=2"


def test_non_credential_query_parameters_keep_their_encoding() -> None:
    """Rewriting the query must not decode escaped separators or nested URLs."""
    redacted = redact_sensitive_text(
        "https://api.example.com/v1?token=x&filter=a%26b&next=https%3A%2F%2Fother%2Fp"
    )
    assert redacted == (
        f"https://api.example.com/v1?token={REDACTED}"
        "&filter=a%26b&next=https%3A%2F%2Fother%2Fp"
    )


def test_percent_encoded_credential_parameter_name_is_matched() -> None:
    redacted = redact_sensitive_text("https://api.example.com/v1?API%5FKEY=abc&page=2")
    assert redacted == f"https://api.example.com/v1?API%5FKEY={REDACTED}&page=2"


def test_ipv6_host_url_credentials_are_redacted() -> None:
    """A closing bracket ending an IPv6 literal host is not prose punctuation."""
    assert redact_sensitive_text("https://user:hunter2@[::1]") == (
        f"https://user:{REDACTED}@[::1]"
    )
    assert redact_sensitive_text("https://user:hunter2@[::1]:8080/p") == (
        f"https://user:{REDACTED}@[::1]:8080/p"
    )


def test_prose_punctuation_after_a_url_is_preserved() -> None:
    assert redact_sensitive_text("see https://user:pw@example.com/p.") == (
        f"see https://user:{REDACTED}@example.com/p."
    )
    assert redact_sensitive_text("endpoint (https://api.example.com/v1?token=abc)") == (
        f"endpoint (https://api.example.com/v1?token={REDACTED})"
    )


def test_credential_parameter_names_reuse_the_field_name_axis() -> None:
    """A name the field axis knows is a credential in a query string too."""
    assert redact_sensitive_text(
        "https://host/callback?client_secret=hunter2&state=x"
    ) == (f"https://host/callback?client_secret={REDACTED}&state=x")
    assert redact_sensitive_text(
        "https://h/p?access_token=a&refresh_token=b&page=1"
    ) == (f"https://h/p?access_token={REDACTED}&refresh_token={REDACTED}&page=1")


def test_unparseable_url_fails_closed() -> None:
    """A malformed endpoint is what an operator inspects; it may still hold a password."""
    assert redact_sensitive_text("https://user:hunter2@[::1") == REDACTED


def test_mcp_env_block_values_are_redacted_whatever_the_variable_is_called() -> None:
    """$VAR references are expanded at load time, so the dump holds real values."""
    redacted = redact_sensitive_config({
        "mcp_config": {
            "mcpServers": {
                "brave": {
                    "command": "brave-search-mcp-server",
                    "env": {
                        "AUTHORIZATION": "Bearer live-token",
                        "BRAVE_API_KEY": "live-key",
                    },
                }
            }
        }
    })
    server = redacted["mcp_config"]["mcpServers"]["brave"]
    assert server["env"] == {
        "AUTHORIZATION": REDACTED,
        "BRAVE_API_KEY": REDACTED,
    }
    assert server["command"] == "brave-search-mcp-server"


def test_vendor_namespaced_signature_parameters_are_redacted() -> None:
    redacted = redact_sensitive_text(
        "https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abc&X-Amz-Expires=60"
    )
    assert redacted == (
        f"https://bucket.s3.amazonaws.com/o?X-Amz-Signature={REDACTED}&X-Amz-Expires=60"
    )


def test_inline_pem_private_key_is_redacted() -> None:
    """``apns.auth_key`` holds a PEM inline; its name matches no secret substring."""
    redacted = redact_sensitive_config({
        "apns": {
            "team_id": "TEAM123",
            "key_id": "KEY123",
            "auth_key": PEM_KEY,
            "auth_key_path": None,
            "bundle_id": "com.example.app",
        }
    })
    apns = redacted["apns"]
    assert apns["auth_key"] == REDACTED
    assert apns["team_id"] == "TEAM123"
    assert apns["key_id"] == "KEY123"
    assert apns["bundle_id"] == "com.example.app"


def test_pem_is_redacted_under_any_field_name() -> None:
    redacted = redact_sensitive_config({"notes": PEM_KEY})
    assert redacted["notes"] == REDACTED


def test_mcp_server_url_credentials_are_redacted() -> None:
    redacted = redact_sensitive_config({
        "mcp_config": {
            "mcpServers": {
                "shopping_search_tools": {
                    "url": "https://www.searchapi.io/api/v1/mcp?token=live-secret",
                    "transport": "http",
                }
            }
        }
    })
    server = redacted["mcp_config"]["mcpServers"]["shopping_search_tools"]
    assert server["url"] == f"https://www.searchapi.io/api/v1/mcp?token={REDACTED}"
    assert server["transport"] == "http"


def test_numeric_fields_matching_secret_substrings_are_not_redacted() -> None:
    """``max_tokens`` is a count, not a credential; only strings can carry secrets."""
    redacted = redact_sensitive_config({
        "llm_parameters": {
            "claude-opus-5": {"max_tokens": 8192, "temperature": 0.2},
        }
    })
    params = redacted["llm_parameters"]["claude-opus-5"]
    assert params["max_tokens"] == 8192
    assert params["temperature"] == 0.2


def test_numeric_credentials_under_sensitive_names_are_redacted() -> None:
    """extra="allow" models accept whatever an operator writes, including a number."""
    redacted = redact_sensitive_config({
        "mcp_config": {
            "mcpServers": {
                "legacy": {"command": "srv", "password": 123456, "pin_token": 654321}
            }
        }
    })
    server = redacted["mcp_config"]["mcpServers"]["legacy"]
    assert server["password"] == REDACTED
    assert server["pin_token"] == REDACTED
    assert server["command"] == "srv"


def test_booleans_under_sensitive_names_stay_readable() -> None:
    redacted = redact_sensitive_config({
        "token_enabled": True,
        "api_key_present": False,
    })
    assert redacted["token_enabled"] is True
    assert redacted["api_key_present"] is False


def test_indirection_field_names_are_not_redacted() -> None:
    """Fields naming where a secret lives hold metadata, not the secret."""
    for name in (
        "token_file",
        "token_env",
        "anthropic_api_key_env",
        "gemini_api_key_env",
        "auth_key_path",
        "private_key_path",
        "secrets_dir",
    ):
        assert not is_sensitive_field_name(name), name


def test_indirection_values_survive_redaction() -> None:
    redacted = redact_sensitive_config({
        "keychute_config": {"token_file": "/etc/family-assistant/keychute.token"},
        "browser_handoff_config": {"auth": {"token_env": "BROWSER_HANDOFF_TOKEN"}},
        "ai_worker_config": {
            "docker": {
                "anthropic_api_key_env": "ANTHROPIC_API_KEY",
                "gemini_api_key_env": "GEMINI_API_KEY",
            }
        },
    })
    assert (
        redacted["keychute_config"]["token_file"]
        == "/etc/family-assistant/keychute.token"
    )
    assert (
        redacted["browser_handoff_config"]["auth"]["token_env"]
        == "BROWSER_HANDOFF_TOKEN"
    )
    docker = redacted["ai_worker_config"]["docker"]
    assert docker["anthropic_api_key_env"] == "ANTHROPIC_API_KEY"
    assert docker["gemini_api_key_env"] == "GEMINI_API_KEY"


def test_named_secrets_are_still_redacted() -> None:
    redacted = redact_sensitive_config({
        "telegram_token": "12345:abcdef",
        "openai_api_key": "sk-live",
        "pwa_config": {
            "vapid_private_key": "vapid-secret",
            "vapid_public_key": "vapid-public",
        },
        "api_keys": ["one", "two"],
        "empty_token": "",
    })
    assert redacted["telegram_token"] == REDACTED
    assert redacted["openai_api_key"] == REDACTED
    assert redacted["pwa_config"]["vapid_private_key"] == REDACTED
    assert redacted["pwa_config"]["vapid_public_key"] == "vapid-public"
    assert redacted["api_keys"] == [REDACTED, REDACTED]
    assert not redacted["empty_token"]


def test_url_embedded_in_a_larger_string_is_redacted() -> None:
    """MCP stdio arguments carry endpoints as ``--endpoint=https://...``."""
    redacted = redact_sensitive_text(
        "--endpoint=https://user:hunter2@scraper.internal/mcp?api_key=abc"
    )
    assert redacted == (
        f"--endpoint=https://user:{REDACTED}@scraper.internal/mcp?api_key={REDACTED}"
    )


def test_live_app_config_dump_contains_no_credential_material() -> None:
    """End-to-end over a real AppConfig: the dump get_resolved_config returns."""
    app_config = AppConfig(
        database_url="postgresql+asyncpg://fa_user:db-password@db.internal/family",
        telegram_token="telegram-secret",
        apns=ApnsConfig(
            team_id="TEAM123",
            key_id="KEY123",
            auth_key=PEM_KEY,
            bundle_id="com.example.app",
        ),
        mcp_config=MCPConfig(
            mcpServers={
                "shopping_search_tools": MCPServerConfig.model_validate({
                    "url": "https://www.searchapi.io/api/v1/mcp?token=searchapi-secret",
                    "transport": "http",
                })
            }
        ),
    )

    serialized = json.dumps(redact_sensitive_config(app_config.model_dump(mode="json")))

    for secret in (
        "db-password",
        "telegram-secret",
        "searchapi-secret",
        "BEGIN PRIVATE KEY",
    ):
        assert secret not in serialized, secret
    # Non-credential context around the redactions stays usable.
    assert "db.internal" in serialized
    assert "www.searchapi.io" in serialized
    assert "TEAM123" in serialized
