"""Replayed integration tests for classifying a real Interactions agent run.

The reconciliation work rests on a claim about the provider that no fake can
establish: that a run's readings change over its life, and that the fields we
classify from -- ``status``, ``steps``, ``output_text``, ``usage``, the
timestamps -- are populated the way ``observe_async`` assumes. The functional
tests script those readings; these take them from the API.

Both cases below are the ones the production audit turned on. A run that is
still going must read as pending rather than as anything terminal, and a
cancellation this application requested must only be reported as a
cancellation once the provider itself says so -- which is a fact about the
provider's ``cancel`` then ``get`` sequence, not about our code.

VCR rather than the Gemini SDK's ``DebugConfig`` replay, for the reason given
in ``test_google_antigravity_integration.py``: the Interactions API is served
by the SDK's separate ``_gaos`` client, which the replay layer does not
intercept.
"""

import os
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.processing.interactions_agent_service import (
    InteractionsAgentProcessingService,
)
from family_assistant.processing.protocol import (
    PENDING,
    TERMINAL_REMOTE_DISPOSITIONS,
    RemoteDisposition,
    RemoteObservation,
)
from family_assistant.processing.types import ProcessingServiceConfig
from tests.helpers import wait_for_condition

from .vcr_helpers import sanitize_response

ANTIGRAVITY_AGENT_ID = "antigravity-preview-05-2026"
# Pinned for the same reason as the submit-shape tests: the reasoning model is
# incidental to what these assert, and re-recording needs a key entitled to
# this preview.
REASONING_MODEL = "gemini-3.7-flash"

_SYSTEM = "You are a coding agent. Run the code and report the output."
_TASK = "Using Python, print the numbers 1 to 3, one per line, then stop."


class _NoToolsProvider:
    """The profile holds no tools; the agent runs server-side."""

    async def get_tool_definitions(self) -> list:
        return []

    async def execute_tool(self, *args: object, **kwargs: object) -> str:
        _ = (args, kwargs)
        raise AssertionError("a sandboxed agent profile should never call a tool")

    async def close(self) -> None:
        pass


def _service() -> InteractionsAgentProcessingService:
    """The real profile, so the classifier under test is the shipped one."""
    client = GoogleGenAIClient(
        # Recording needs a real key; replay does not, and the cassette holds
        # no `x-goog-api-key` (see `vcr_config`'s `filter_headers`).
        api_key=os.getenv("GEMINI_API_KEY", "test-gemini-key"),
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model=REASONING_MODEL,
    )
    config = ProcessingServiceConfig(
        prompts={"system_prompt": _SYSTEM},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="coder",
    )
    return InteractionsAgentProcessingService(
        llm_client=client,
        tools_provider=_NoToolsProvider(),
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )


async def _start_run(service: InteractionsAgentProcessingService) -> str:
    interaction = await service._google_client().start_agent_interaction([
        SystemMessage(content=_SYSTEM),
        UserMessage(content=_TASK),
    ])
    assert interaction.id
    return interaction.id


def _poll_interval(llm_record_mode: str) -> float:
    """Seconds between reads: paced while recording, immediate while replaying.

    An agent run takes minutes, so recording at a short interval would write
    hundreds of near-identical readings into the cassette. Replay has no run to
    wait for -- it walks the recorded readings in order -- so it takes them as
    fast as it can.
    """
    return 0.01 if llm_record_mode == "replay" else 15.0


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_a_live_run_reads_as_pending_and_records_a_bounded_reading() -> None:
    """A run that is still going is pending, and its reading carries no output.

    The disposition matters because everything downstream keys off it: a live
    run classified as anything terminal is a run we would fail, cancel, or
    "recover" an empty result from. The bounded reading matters because it is
    what gets persisted on every poll of every run.
    """
    service = _service()
    try:
        interaction_id = await _start_run(service)
        observation = await service.observe_async(interaction_id)
    finally:
        await service._google_client().close()

    assert observation.disposition is RemoteDisposition.PENDING
    assert service.result_for_observation(observation) is PENDING
    assert observation.remote_task_id == interaction_id

    metadata = observation.to_metadata()
    # A live run has produced no result yet, so there is nothing to measure --
    # and the record says so rather than carrying a partial transcript.
    assert metadata["has_output"] is False
    assert _TASK not in str(metadata)
    assert _SYSTEM not in str(metadata)
    # Fixed shape: a provider field we did not ask for cannot appear here.
    assert set(metadata) == {
        "remote_task_id",
        "status",
        "disposition",
        "observed_at",
        "remote_created_at",
        "remote_updated_at",
        "has_output",
        "output_chars",
        "step_count",
        "total_tokens",
        "resolved_model",
        "error_summary",
        "event_cursor",
    }


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_cancellation_is_only_confirmed_by_the_providers_own_reading(
    llm_record_mode: str,
) -> None:
    """Asking to cancel proves nothing; a later reading is what confirms it.

    ``cancel`` returns nothing, so the only evidence a run actually stopped is
    a subsequent ``get``. This pins that the provider does eventually report
    ``cancelled`` and that we classify it as its own disposition rather than
    folding it in with the other terminal errors -- which is what let a timed
    out run tell the user it had been cancelled when nobody had confirmed it.
    """
    service = _service()
    try:
        interaction_id = await _start_run(service)
        before = await service.observe_async(interaction_id)
        assert before.disposition is RemoteDisposition.PENDING

        await service.cancel_async(interaction_id)

        settled = await wait_for_condition(
            lambda: _terminal_or_none(service, interaction_id),
            timeout=300.0,
            interval=_poll_interval(llm_record_mode),
            description="the provider to report the cancelled run terminal",
        )
    finally:
        await service._google_client().close()

    # wait_for_condition returns only a truthy result, so this is the reading
    # that satisfied it; the narrowing is for the type checker.
    assert settled is not None
    assert settled.disposition is RemoteDisposition.CANCELLED
    assert settled.status == "cancelled"
    # A cancelled run is terminal, but it is not a completion: nothing may be
    # recovered from it as a late result.
    assert settled.is_terminal
    assert settled.result is None


async def _terminal_or_none(
    service: InteractionsAgentProcessingService, interaction_id: str
) -> RemoteObservation | None:
    """One reading, returned only once the provider considers the run over."""
    observation = await service.observe_async(interaction_id)
    return (
        observation if observation.disposition in TERMINAL_REMOTE_DISPOSITIONS else None
    )


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_a_real_completion_is_classified_as_carrying_a_result(
    llm_record_mode: str,
) -> None:
    """A run that actually did the work reads as a useful completion.

    The empty-completion rule is only safe if a genuine completion clears it,
    and what it clears it on -- output text, steps, token usage -- are provider
    fields no fake can vouch for. Classifying a real result as empty would
    convert every successful agent run into a failure, so this is the case that
    bounds the rule from the other side.
    """
    service = _service()
    try:
        interaction_id = await _start_run(service)
        settled = await wait_for_condition(
            lambda: _terminal_or_none(service, interaction_id),
            timeout=1800.0,
            interval=_poll_interval(llm_record_mode),
            description="the agent run to reach a terminal state",
        )
    finally:
        await service._google_client().close()

    assert settled is not None
    assert settled.disposition is RemoteDisposition.COMPLETED
    assert settled.result is not None
    assert not settled.result.has_error

    # The evidence the classifier ran on is really there, and the persisted
    # record describes the result without reproducing it.
    metadata = settled.to_metadata()
    assert metadata["has_output"] is True
    assert metadata["output_chars"] > 0
    assert settled.result.text_reply not in str(metadata)
    # The Interactions API does not report a resolved model for these runs, so
    # the record carries None. That is the distinction the fixed-shape metadata
    # exists to keep: "the provider did not report it" is written down, rather
    # than filled in from the model we asked for.
    assert metadata["resolved_model"] is None
