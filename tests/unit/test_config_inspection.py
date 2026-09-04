"""Unit tests for config redaction in :mod:`family_assistant.config_inspection`.

Credential fields are typed ``SecretStr`` and redact themselves, so the tests
that matter are (a) that the type actually holds for the fields that leaked,
and (b) that the two things a type cannot express -- credentials embedded in a
larger value, and dynamically-structured config -- are still handled.
"""

from __future__ import annotations

import json
import os

from pydantic import SecretStr

from family_assistant.config_inspection import (
    REDACTED,
    redact_sensitive_config,
    redact_sensitive_text,
)
from family_assistant.config_loader import expand_env_vars_in_dict
from family_assistant.config_models import (
    ApnsConfig,
    AppConfig,
    MCPConfig,
    MCPServerConfig,
    mcp_servers_for_runtime,
)

PEM_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg\n"
    "-----END PRIVATE KEY-----\n"
)


# --- Declared credentials: masked by type, with no help from this module ---


def test_declared_credential_fields_mask_themselves() -> None:
    """No redaction pass involved -- model_dump alone must not emit the secret."""
    config = AppConfig(
        telegram_token=SecretStr("telegram-secret"),
        openai_api_key=SecretStr("sk-live"),
        apns=ApnsConfig(team_id="TEAM123", auth_key=SecretStr(PEM_KEY)),
    )

    dumped = json.dumps(config.model_dump(mode="json"))

    for secret in ("telegram-secret", "sk-live", "BEGIN PRIVATE KEY"):
        assert secret not in dumped, secret
    assert "TEAM123" in dumped


def test_unset_credential_stays_none_rather_than_redacted() -> None:
    """A dump still distinguishes "not configured" from "configured but hidden"."""
    dumped = AppConfig().model_dump(mode="json")
    assert dumped["telegram_token"] is None
    assert dumped["openai_api_key"] is None


def test_kubernetes_secret_name_is_not_a_credential() -> None:
    """api_keys_secret names a Kubernetes Secret; the old name heuristic hid it."""
    dumped = AppConfig().model_dump(mode="json")
    config = dumped["ai_worker_config"]["kubernetes"]
    assert "api_keys_secret" in config


def test_runtime_mcp_dump_carries_the_real_token() -> None:
    """The masking that protects diagnostics must not reach a connecting client."""
    mcp_config = MCPConfig(
        mcpServers={
            "remote": MCPServerConfig.model_validate({
                "url": "https://mcp.example.com/",
                "token": "live-token",
            })
        }
    )

    assert mcp_config.mcpServers["remote"].model_dump()["token"] != "live-token"
    assert mcp_servers_for_runtime(mcp_config)["remote"]["token"] == "live-token"


# --- Embedded credentials: a type cannot express "part of this string" ---


def test_database_url_password_is_redacted_but_host_survives() -> None:
    redacted = redact_sensitive_config({
        "database_url": "postgresql+asyncpg://fa_user:hunter2@db.internal:5432/family"
    })
    assert redacted["database_url"] == (
        f"postgresql+asyncpg://fa_user:{REDACTED}@db.internal:5432/family"
    )


def test_dsn_passwords_outside_the_rfc_grammar_are_redacted() -> None:
    """SQLAlchemy reads a password up to the "@"; urlsplit stops at "/?#".

    Each of these is a valid runtime database URL whose password the RFC parser
    never sees, so it has to be located with the grammar that accepts it.
    """
    for dsn, password in (
        ("postgresql+asyncpg://user:pa/ss@db/family", "pa/ss"),
        ("postgresql://user:pa?ss@db/family", "pa?ss"),
        ("postgresql://user:pa ss@db/family", "pa ss"),
        ("postgresql://user:pa#ss@db/family", "pa#ss"),
        # Percent-encoded bytes mixed with an unescaped delimiter: the decoded
        # password ("pa@/ss") never appears in the value, so the raw span is
        # what has to be matched.
        ("postgresql://user:pa%40/ss@db/family", "pa%40/ss"),
    ):
        redacted = redact_sensitive_text(dsn)
        assert password not in redacted, dsn
        assert REDACTED in redacted, dsn
        assert redacted.endswith("@db/family"), dsn


def test_url_without_credentials_is_untouched() -> None:
    for url in (
        "sqlite+aiosqlite:///family_assistant.db",
        "https://homeassistant.internal:8123/api",
        "postgresql+asyncpg://fa_user@db.internal:5432/family",
    ):
        assert redact_sensitive_text(url) == url


def test_credential_query_parameters_are_redacted() -> None:
    assert (
        redact_sensitive_text(
            "https://www.searchapi.io/api/v1/mcp?token=live-secret&engine=google"
        )
        == f"https://www.searchapi.io/api/v1/mcp?token={REDACTED}&engine=google"
    )
    assert (
        redact_sensitive_text("https://host/callback?client_secret=hunter2&state=x")
        == f"https://host/callback?client_secret={REDACTED}&state=x"
    )


def test_non_credential_query_parameters_keep_their_encoding() -> None:
    """Rewriting the query must not decode escaped separators or nested URLs."""
    assert redact_sensitive_text(
        "https://api.example.com/v1?token=x&filter=a%26b&next=https%3A%2F%2Fother%2Fp"
    ) == (
        f"https://api.example.com/v1?token={REDACTED}"
        "&filter=a%26b&next=https%3A%2F%2Fother%2Fp"
    )


def test_vendor_namespaced_signature_parameters_are_redacted() -> None:
    assert redact_sensitive_text(
        "https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abc&X-Amz-Expires=60"
    ) == (
        f"https://bucket.s3.amazonaws.com/o?X-Amz-Signature={REDACTED}&X-Amz-Expires=60"
    )


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


def test_unparseable_url_fails_closed() -> None:
    """A malformed endpoint is what an operator inspects; it may still hold a password."""
    assert redact_sensitive_text("https://user:hunter2@[::1") == REDACTED


def test_url_embedded_in_a_larger_string_is_redacted() -> None:
    """MCP stdio arguments carry endpoints as ``--endpoint=https://...``."""
    assert (
        redact_sensitive_text(
            "--endpoint=https://user:hunter2@scraper.internal/mcp?api_key=abc"
        )
        == f"--endpoint=https://user:{REDACTED}@scraper.internal/mcp?api_key={REDACTED}"
    )


def test_pem_is_redacted_under_any_field_name() -> None:
    assert redact_sensitive_config({"notes": PEM_KEY})["notes"] == REDACTED


# --- Dynamic config: no declared shape to annotate ---


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
    assert server["env"] == {"AUTHORIZATION": REDACTED, "BRAVE_API_KEY": REDACTED}
    assert server["command"] == "brave-search-mcp-server"


def test_non_credential_config_is_left_alone() -> None:
    """Names that merely look secret are no longer judged at all."""
    redacted = redact_sensitive_config({
        "llm_parameters": {"claude-opus-5": {"max_tokens": 8192, "temperature": 0.2}},
        "keychute_config": {"token_file": "/etc/family-assistant/keychute.token"},
        "browser_handoff_config": {"auth": {"token_env": "BROWSER_HANDOFF_TOKEN"}},
        "ai_worker_config": {"docker": {"anthropic_api_key_env": "ANTHROPIC_API_KEY"}},
    })
    assert redacted["llm_parameters"]["claude-opus-5"]["max_tokens"] == 8192
    assert (
        redacted["keychute_config"]["token_file"]
        == "/etc/family-assistant/keychute.token"
    )
    assert (
        redacted["browser_handoff_config"]["auth"]["token_env"]
        == "BROWSER_HANDOFF_TOKEN"
    )
    assert (
        redacted["ai_worker_config"]["docker"]["anthropic_api_key_env"]
        == "ANTHROPIC_API_KEY"
    )


# --- End to end over the dump get_resolved_config actually returns ---


def test_live_app_config_dump_contains_no_credential_material() -> None:
    app_config = AppConfig(
        database_url="postgresql+asyncpg://fa_user:db-password@db.internal/family",
        telegram_token=SecretStr("telegram-secret"),
        apns=ApnsConfig(team_id="TEAM123", auth_key=SecretStr(PEM_KEY)),
        mcp_config=MCPConfig(
            mcpServers={
                "shopping_search_tools": MCPServerConfig.model_validate({
                    "url": "https://www.searchapi.io/api/v1/mcp?token=searchapi-secret",
                    "token": "mcp-bearer-secret",
                    "env": {"AUTHORIZATION": "Bearer env-secret"},
                })
            }
        ),
    )

    serialized = json.dumps(redact_sensitive_config(app_config.model_dump(mode="json")))

    for secret in (
        "db-password",
        "telegram-secret",
        "searchapi-secret",
        "mcp-bearer-secret",
        "env-secret",
        "BEGIN PRIVATE KEY",
    ):
        assert secret not in serialized, secret
    assert "db.internal" in serialized
    assert "www.searchapi.io" in serialized
    assert "TEAM123" in serialized


# --- SecretStr must not escape into untyped runtime paths ---
#
# These paths lose the declared type (a plain dict, an unvalidated model_copy),
# so the type checker cannot see them: a SecretStr reaching one of them is
# serialized as its mask and the credential silently stops working.


def test_cli_override_produces_a_usable_secret() -> None:
    """model_copy(update=...) does not validate, so the CLI must convert."""
    config = AppConfig().model_copy(
        update={"telegram_token": SecretStr("cli-token")},
    )
    assert config.telegram_token is not None
    assert config.telegram_token.get_secret_value() == "cli-token"


def test_env_placeholders_expand_inside_secret_fields() -> None:
    """A masked field still carries a $VAR placeholder that must be expanded.

    ``expand_env_vars_in_dict`` walks strings; a SecretStr is not one, so
    without explicit handling the placeholder survives into the runtime config
    and the server is contacted with a literal "${VAR}" as its token.
    """
    os.environ["FA_TEST_MCP_TOKEN"] = "real-token-value"
    try:
        for placeholder in ("$FA_TEST_MCP_TOKEN", "${FA_TEST_MCP_TOKEN}"):
            config = MCPConfig(
                mcpServers={
                    "remote": MCPServerConfig.model_validate({
                        "url": "https://mcp.example.com/",
                        "token": placeholder,
                    })
                }
            )
            expanded = MCPConfig.model_validate(
                expand_env_vars_in_dict({
                    "mcpServers": {
                        name: server.model_dump()
                        for name, server in config.mcpServers.items()
                    }
                })
            )
            runtime = mcp_servers_for_runtime(expanded)["remote"]
            assert runtime["token"] == "real-token-value", placeholder
    finally:
        del os.environ["FA_TEST_MCP_TOKEN"]
