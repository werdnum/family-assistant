"""Replayed integration tests for the Antigravity managed agent's submit path.

These record what ``interactions.create`` actually does with the request
``GoogleGenAIClient`` builds, which is the gap that let a broken request shape
ship: the unit tests in ``tests/llm/test_google_antigravity.py`` assert the
request we *intend* to send, and agreed with the code while every real run
failed with ``400 Missing required field 'environment'``.

VCR rather than the Gemini SDK's own ``DebugConfig`` replay (the usual
mechanism for this provider, see the ``llm_replay_config`` fixture): the
Interactions API is served by the SDK's separate ``_gaos`` client, which the
replay layer wrapping ``models.generate_content`` does not intercept. VCR sits
at the HTTP transport, so it captures both.

Two shapes are covered because they are the two that exist in practice: a
profile that configures no ``antigravity_config.environment`` at all (the
shipped ``defaults.yaml`` ``coder``), and one that configures an egress
allowlist with an injected credential (this deployment's ``coder``). The first
is the one that regressed; the second is the one that masked it.
"""

import os

import pytest

from family_assistant.config_models import (
    AntigravityEgressCredentialConfig,
    AntigravityEgressRuleConfig,
    AntigravityEnvironmentConfig,
)
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient

from .vcr_helpers import sanitize_response

ANTIGRAVITY_AGENT_ID = "antigravity-preview-05-2026"
REASONING_MODEL = "gemini-3.8-flash"

# Rendered into the request body as an Authorization header for the sandbox's
# egress proxy, so it is recorded into the cassette. Deliberately not a real
# credential -- the API accepts or rejects the request's *shape*, and never
# validates the token at submit.
_PLACEHOLDER_TOKEN_ENV = "FA_TEST_ANTIGRAVITY_EGRESS_TOKEN"
_PLACEHOLDER_TOKEN = "not-a-real-token"

_SYSTEM = "You are a coding agent. Run the code and report the output."
_TASK = "Using Python, print the numbers 1 to 3, one per line."


def _client(environment: AntigravityEnvironmentConfig | None) -> GoogleGenAIClient:
    return GoogleGenAIClient(
        # Recording needs a real key; replay does not, and the cassette holds
        # no `x-goog-api-key` (see `vcr_config`'s `filter_headers`).
        api_key=os.getenv("GEMINI_API_KEY", "test-gemini-key"),
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model=REASONING_MODEL,
        antigravity_environment=environment,
    )


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_submit_accepted_when_the_profile_configures_no_environment() -> None:
    """The shipped ``coder`` mounts nothing and sets no egress policy.

    It therefore has nothing to put in an ``environment`` block, which is
    exactly the case the API refuses when the field is left out rather than
    stated as the default sandbox.
    """
    client = _client(None)
    try:
        interaction = await client.start_agent_interaction([
            SystemMessage(content=_SYSTEM),
            UserMessage(content=_TASK),
        ])
    finally:
        await client.close()

    assert interaction.id
    assert interaction.status != "failed"


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_submit_accepted_with_an_egress_allowlist_and_credential() -> None:
    """A configured allowlist reaches the API in the shape it accepts.

    ``resolve_network`` renders each header as its own single-key object; the
    surrounding types would just as happily produce one object of several
    keys, and nothing but the API can say which it takes.
    """
    os.environ[_PLACEHOLDER_TOKEN_ENV] = _PLACEHOLDER_TOKEN
    client = _client(
        AntigravityEnvironmentConfig(
            network="allowlist",
            allowlist=[
                AntigravityEgressRuleConfig(domain="*"),
                AntigravityEgressRuleConfig(
                    domain="api.github.com",
                    credential=AntigravityEgressCredentialConfig(
                        type="bearer", token_env=_PLACEHOLDER_TOKEN_ENV
                    ),
                ),
            ],
        )
    )
    try:
        interaction = await client.start_agent_interaction([
            SystemMessage(content=_SYSTEM),
            UserMessage(content=_TASK),
        ])
    finally:
        await client.close()
        del os.environ[_PLACEHOLDER_TOKEN_ENV]

    assert interaction.id
    assert interaction.status != "failed"
