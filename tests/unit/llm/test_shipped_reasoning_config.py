"""Direct OpenAI models must reach the Responses API under the shipped config.

Reasoning state only survives a tool loop on the Responses API. The default is
on, so what needs pinning is that nothing in the shipped `llm_parameters` quietly
turns it back off, and that a model nobody has configured still gets it.
"""

import pytest

from family_assistant.config_loader import load_config
from family_assistant.llm.factory import LLMClientFactory
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
