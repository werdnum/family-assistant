"""The shipped defaults must actually reach the client that needs them.

`use_responses_api` is opt-in, which means a broken wiring path fails silently:
the model drops to Chat Completions and loses reasoning propagation across tool
loops with no error anywhere. The old model-name prefix at least could not be
mis-wired, so replacing it with config is only safe if the config is pinned.
"""

from family_assistant.config_loader import load_config
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.providers.openai_client import OpenAIClient


def _shipped_llm_parameters() -> dict[str, dict[str, object]]:
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )
    return config.llm_parameters


def test_shipped_defaults_opt_gpt_5_6_sol_into_responses_api() -> None:
    """defaults.yaml must declare the opt-in for the model that requires it."""
    llm_parameters = _shipped_llm_parameters()

    assert llm_parameters["gpt-5.6-sol"]["use_responses_api"] is True


def test_client_built_from_shipped_defaults_uses_responses_api() -> None:
    """End to end: shipped config -> factory -> a client on the Responses API."""
    client = LLMClientFactory.create_client({
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "api_key": "test-key",
        "model_parameters": _shipped_llm_parameters(),
    })

    assert isinstance(client, OpenAIClient)
    assert client._uses_responses_api() is True


def test_shipped_defaults_leave_other_openai_models_on_chat_completions() -> None:
    """The opt-in must not leak to models that have no Responses support."""
    client = LLMClientFactory.create_client({
        "provider": "openai",
        "model": "gpt-5.5",
        "api_key": "test-key",
        "model_parameters": _shipped_llm_parameters(),
    })

    assert isinstance(client, OpenAIClient)
    assert client._uses_responses_api() is False
