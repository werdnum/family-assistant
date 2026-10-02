"""Replayed integration tests for the Antigravity managed agent's submit and poll paths.

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
is the one that regressed; the second is the one that masked it. A credential-bound environment is also polled to
completion: the API returns its env map as a list, which the SDK response
model cannot validate even though the run succeeds.
"""

import os

import pytest

from family_assistant.config_models import (
    AntigravityEgressCredentialConfig,
    AntigravityEgressRuleConfig,
    AntigravityEnvironmentConfig,
)
from family_assistant.llm.antigravity_egress import (
    AntigravityCredentialStore,
    EgressResolution,
)
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_interaction_terminal_error_status,
)
from tests.helpers import wait_for_condition

from .vcr_helpers import sanitize_response

ANTIGRAVITY_AGENT_ID = "antigravity-preview-05-2026"
# Pinned to 3.7 rather than tracking the shipped default: these cases assert
# the *shape* of the submit request, and re-recording
# test_submit_accepted_with_an_egress_allowlist_and_credential needs a key
# entitled to credential-attached sandbox egress, which returns 403
# permission_denied otherwise. The reasoning model is incidental here.
REASONING_MODEL = "gemini-3.7-flash"

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

    ``resolve`` renders each header as its own single-key object; the
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


class _CredentialEnvironment:
    async def resolve(self) -> EgressResolution:
        return EgressResolution(
            network=None,
            env={
                "FA_TEST_ENV": {"credential": "fa-issue-1301-test-env"},
            },
        )


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_poll_delivers_report_with_a_credential_bound_environment(
    llm_record_mode: str,
) -> None:
    """A credential-bound sandbox round-trips without losing its final report."""
    api_key = os.getenv("GEMINI_API_KEY", "test-gemini-key")
    store = AntigravityCredentialStore(api_key=api_key)
    client = GoogleGenAIClient(
        api_key=api_key,
        model="antigravity-preview-09-2026",
        antigravity_model="gemini-3.8-flash",
        antigravity_egress_resolver=_CredentialEnvironment(),
    )
    interaction_id: str | None = None
    try:
        await store.ensure_substituted(
            "fa-issue-1301-test-env", "harmless-test-value", ["example.com"]
        )
        interaction = await client.start_agent_interaction([
            SystemMessage(
                content="Run the requested code and report its output. Do not access the network."
            ),
            UserMessage(
                content="Run Python to print 1301 and report that number, then stop."
            ),
        ])
        assert interaction.id
        interaction_id = interaction.id

        async def finished() -> bool:
            nonlocal interaction
            interaction = await client.get_agent_interaction(interaction_id)
            return (
                interaction.status == "completed"
                or is_interaction_terminal_error_status(interaction.status)
            )

        await wait_for_condition(
            finished,
            timeout=600.0,
            interval=0.01 if llm_record_mode == "replay" else 15.0,
        )
        assert interaction.status == "completed"
        assert "1301" in (interaction.output_text or "")
    finally:
        if interaction_id is not None and interaction.status == "in_progress":
            await client.cancel_agent_interaction(interaction_id)
        await client.close()
        await store.delete("fa-issue-1301-test-env")
        await store.aclose()
