"""The shipped `llm_parameters` must actually deliver reasoning to each provider.

Two provider-specific ways that can silently fail. On OpenAI, reasoning state
only survives a tool loop on the Responses API; the default is on, so what needs
pinning is that no shipped entry quietly turns it back off and that an
unconfigured model still gets it. On Anthropic, thinking is off unless a shipped
entry enables it, and the enabling shape is not portable across model
generations -- the wrong one is a 400 raised mid-conversation rather than at
startup, so the shape is asserted here instead.
"""

import pytest

from family_assistant.config_loader import load_config
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.openai_client import OpenAIClient


def _shipped_llm_parameters() -> dict[str, dict[str, object]]:
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )
    return config.llm_parameters


def _client_for(model: str) -> OpenAIClient:
    client = LLMClientFactory.create_client({
        "provider": "openai",
        "model": model,
        "api_key": "test-key",
        "model_parameters": _shipped_llm_parameters(),
    })
    assert isinstance(client, OpenAIClient)
    return client


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("gpt-5.6-sol", id="complex-tasks-primary"),
        pytest.param("gpt-5.5", id="retry-fallback"),
        pytest.param("gpt-4.1", id="unconfigured-model"),
    ],
)
def test_shipped_defaults_put_direct_openai_models_on_responses_api(
    model: str,
) -> None:
    """End to end: shipped config -> factory -> a client on the Responses API."""
    assert _client_for(model)._uses_responses_api() is True


def test_shipped_defaults_do_not_disable_the_responses_api_anywhere() -> None:
    """No shipped entry may pin a model back to Chat Completions unnoticed."""
    disabled = {
        pattern: params
        for pattern, params in _shipped_llm_parameters().items()
        if params.get("use_responses_api") is False
    }

    assert disabled == {}


def _anthropic_client_for(model: str) -> AnthropicClient:
    client = LLMClientFactory.create_client({
        "provider": "anthropic",
        "model": model,
        "api_key": "test-key",
        "model_parameters": _shipped_llm_parameters(),
    })
    assert isinstance(client, AnthropicClient)
    return client


def test_shipped_defaults_enable_adaptive_thinking_for_sonnet_5() -> None:
    """The Anthropic profiles' model must get thinking, in its generation's shape.

    `enabled` + `budget_tokens` is what the previous generation took and is a 400
    on this one, so the type is asserted rather than merely the presence of a
    `thinking` key.
    """
    params = _anthropic_client_for("claude-sonnet-5")._get_model_specific_params(
        "claude-sonnet-5"
    )

    assert params["thinking"] == {"type": "adaptive"}


def test_shipped_sonnet_5_request_carries_thinking_and_survives_validation() -> None:
    """End to end: shipped config -> factory -> a request the client will send.

    Covers the budget/`max_tokens` validation too, which otherwise only fails
    once a conversation is already underway.
    """
    client = _anthropic_client_for("claude-sonnet-5")
    system_blocks, api_messages = client._convert_messages_to_anthropic_format([
        UserMessage(content="hello")
    ])

    params = client._build_request_params(
        api_messages=api_messages,
        system_blocks=system_blocks,
        tools=None,
        tool_choice=None,
    )

    assert params["thinking"] == {"type": "adaptive"}
    # Thinking shares this budget with the response, so it has to be above the
    # client's own 8192 default for the pairing to be deliberate.
    assert params["max_tokens"] > 8192


def test_shipped_defaults_leave_thinking_off_for_unconfigured_anthropic_models() -> (
    None
):
    """Enabling thinking is per model, so it must not leak to the fallback.

    `claude-fable-5` is the `complex_tasks` fallback and rejects an explicit
    disabled thinking config, so it has to inherit nothing at all here.
    """
    params = _anthropic_client_for("claude-fable-5")._get_model_specific_params(
        "claude-fable-5"
    )

    assert "thinking" not in params
