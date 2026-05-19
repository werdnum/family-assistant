"""Unit tests for helpers in :mod:`family_assistant.web.routers.debug_api`.

These tests exercise the internal redaction helpers directly. The functional
endpoint behavior lives in ``tests/functional/web/api/test_debug_api.py``.
"""

from family_assistant.web.routers.debug_api import (
    SENSITIVE_FIELD_NAMES,
    is_sensitive_field_name,
    redact_sensitive_config,
    resolve_live_llm_fallback_model,
    resolve_live_llm_model,
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
        "on_demand_local_tools",
    ):
        assert not is_sensitive_field_name(name), name


def test_resolve_live_llm_model_reads_model_attribute() -> None:
    """OpenAIClient/AnthropicClient shape: ``self.model``."""

    class _OpenAILike:
        model = "gpt-5-turbo"

    assert resolve_live_llm_model(_OpenAILike()) == "gpt-5-turbo"


def test_resolve_live_llm_model_reads_model_name_and_strips_prefix() -> None:
    """GoogleGenAIClient shape: ``self.model_name`` with ``models/`` prefix."""

    class _GoogleLike:
        model_name = "models/gemini-3.1-pro-preview"

    assert resolve_live_llm_model(_GoogleLike()) == "gemini-3.1-pro-preview"


def test_resolve_live_llm_model_returns_none_when_no_attribute() -> None:
    class _BareClient:
        pass

    assert resolve_live_llm_model(_BareClient()) is None
    assert resolve_live_llm_model(None) is None


def test_resolve_live_llm_model_prefers_model_over_model_name() -> None:
    """When both attributes are present, ``model`` takes precedence."""

    class _Both:
        model = "gpt-5-turbo"
        model_name = "models/gemini-3.1-pro-preview"

    assert resolve_live_llm_model(_Both()) == "gpt-5-turbo"


def test_resolve_live_llm_model_reads_primary_model_on_retrying_client() -> None:
    """RetryingLLMClient shape: the wrapper sets ``self.primary_model`` only.

    It does NOT expose ``.model`` or ``.model_name`` itself, so the helper
    must check ``primary_model`` first to correctly report the active model
    for profiles configured with ``processing_config.retry_config``.
    """

    class _RetryingLike:
        primary_model = "anthropic/claude-sonnet-4-6"
        fallback_model = "openai/gpt-5.5"

    assert resolve_live_llm_model(_RetryingLike()) == "anthropic/claude-sonnet-4-6"


def test_resolve_live_llm_model_prefers_primary_model_over_plain_model() -> None:
    """When both are present, primary_model wins — it reflects the wrapper's choice."""

    class _Hybrid:
        primary_model = "primary-one"
        model = "concrete-two"

    assert resolve_live_llm_model(_Hybrid()) == "primary-one"


def test_resolve_live_llm_model_strips_google_prefix_from_primary_model() -> None:
    """The ``models/`` prefix normalization also applies to primary_model."""

    class _RetryingGoogle:
        primary_model = "models/gemini-3.1-pro-preview"

    assert resolve_live_llm_model(_RetryingGoogle()) == "gemini-3.1-pro-preview"


def test_resolve_live_llm_fallback_model_returns_configured_fallback() -> None:
    """Fallback is reported only when a ``fallback_client`` is actually wired."""

    class _RetryingLike:
        fallback_client = object()  # truthy = fallback is real
        fallback_model = "openai/gpt-5.5"

    assert resolve_live_llm_fallback_model(_RetryingLike()) == "openai/gpt-5.5"


def test_resolve_live_llm_fallback_model_strips_google_prefix() -> None:
    class _RetryingGoogleFallback:
        fallback_client = object()
        fallback_model = "models/gemini-3.5-flash"

    assert (
        resolve_live_llm_fallback_model(_RetryingGoogleFallback()) == "gemini-3.5-flash"
    )


def test_resolve_live_llm_fallback_model_returns_none_when_no_fallback_client() -> None:
    """RetryingLLMClient sets ``fallback_model`` to a default string even when no
    ``fallback_client`` is configured, so the default would otherwise leak."""

    class _PrimaryOnlyRetrying:
        fallback_client = None  # no real fallback is wired
        fallback_model = "openai/gpt-5.5"  # default from RetryingLLMClient.__init__

    assert resolve_live_llm_fallback_model(_PrimaryOnlyRetrying()) is None


def test_resolve_live_llm_fallback_model_returns_none_for_plain_client() -> None:
    """Concrete provider clients do not have fallback_client / fallback_model."""

    class _OpenAILike:
        model = "gpt-5-turbo"

    assert resolve_live_llm_fallback_model(_OpenAILike()) is None
    assert resolve_live_llm_fallback_model(None) is None


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
