"""Integration test for LLM retry and fallback behavior."""

import os

import pytest

from family_assistant.llm.factory import LLMClientFactory
from tests.factories.messages import create_user_message

from .vcr_helpers import sanitize_response


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "primary_provider,primary_model,fallback_provider,fallback_model",
    [("google", "gemini-2.5-flash-lite", "openai", "gpt-4.1-nano")],
)
async def test_real_provider_fallback(
    primary_provider: str,
    primary_model: str,
    fallback_provider: str,
    fallback_model: str,
) -> None:
    """A rejected Google request is answered by the configured OpenAI fallback."""
    client = LLMClientFactory.create_client({
        "retry_config": {
            "primary": {
                "provider": primary_provider,
                "model": primary_model,
                # The recorded 400 makes this fallback deterministic.
                "api_key": "invalid-primary-key",
                "api_base": "https://generativelanguage.googleapis.com/v1beta",
            },
            "fallback": {
                "provider": fallback_provider,
                "model": fallback_model,
                "api_key": os.getenv("OPENAI_API_KEY", "test-key"),
            },
        }
    })

    response = await client.generate_response([
        create_user_message("Reply with: 'Primary response received'")
    ])

    assert response.content == "Primary response received"
    assert response.resolved_model is not None
    assert response.resolved_model.startswith("gpt-4.1-nano")
