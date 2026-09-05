"""The Auto classifier: what it decides, and how it fails.

A routing failure has to be visible and non-fatal, so most of this is about the
ways the call can go wrong: the run still has to happen, and the record still
has to say which way it went. The other half is the constraint -- the schema
handed to the provider admits exactly the profile's automatic tiers, so a
decision outside the authorized range cannot be expressed rather than being
caught after the model has already made it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, get_args

import pytest
from pydantic import BaseModel

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.llm.model_routing import ModelRouter
from family_assistant.llm.model_selection import (
    ROUTING_OUTCOMES,
    ModelTierEligibility,
    ModelTierOption,
    RoutingOutcome,
)
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.llm.model_routing import RoutingDecision
    from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
        StructuredMatcherArgs,
    )

ELIGIBILITY = ModelTierEligibility(
    default_tier="standard",
    selectable=(
        ModelTierOption(id="standard", label="Standard", description="Everyday work."),
        ModelTierOption(id="deep", label="Deep", description="Hard judgement calls."),
        ModelTierOption(id="frontier", label="Max"),
    ),
    # `frontier` is selectable by a person but outside the automatic range.
    auto=frozenset({"standard", "deep"}),
)

PROMPT = (
    "Choose a tier.\nTiers:\n{tiers}\nProfile guidance:\n{guidance}\nAnswer with an id."
)


def _router(client: LLMInterface, *, timeout_seconds: float = 5.0) -> ModelRouter:
    return ModelRouter(
        client,
        prompt_template=PROMPT,
        classifier_model="mock-classifier",
        timeout_seconds=timeout_seconds,
        history_messages=6,
    )


def _deciding_client(tier: str) -> RuleBasedMockLLMClient:
    """A classifier that answers *tier* whatever it is asked."""

    def answer(args: StructuredMatcherArgs) -> BaseModel:
        return args["response_model"].model_validate({"tier": tier})

    return RuleBasedMockLLMClient(rules=[], structured_rules=[(lambda _: True, answer)])


def _capturing_client(
    captured: list[StructuredMatcherArgs], tier: str = "standard"
) -> RuleBasedMockLLMClient:
    """A deciding classifier that also hands the test what it was asked."""

    def answer(args: StructuredMatcherArgs) -> BaseModel:
        captured.append(args)
        return args["response_model"].model_validate({"tier": tier})

    return RuleBasedMockLLMClient(rules=[], structured_rules=[(lambda _: True, answer)])


def _prompt_text(args: StructuredMatcherArgs) -> str:
    return "\n".join(
        message.content
        for message in args["messages"]
        if isinstance(message.content, str)
    )


async def _route(
    client: LLMInterface,
    *,
    guidance: str | None = None,
    history: Sequence[LLMMessage] = (),
    request_text: str = "what is the weather",
    attachment_summary: Sequence[str] = (),
    timeout_seconds: float = 5.0,
) -> RoutingDecision:
    return await _router(client, timeout_seconds=timeout_seconds).route(
        eligibility=ELIGIBILITY,
        guidance=guidance,
        history=history,
        request_text=request_text,
        attachment_summary=attachment_summary,
    )


async def test_a_usable_answer_is_a_decision_naming_the_tier() -> None:
    decision = await _route(_deciding_client("deep"))

    assert decision.tier == "deep"
    assert decision.outcome == "decided"
    assert decision.classifier_model == "mock-classifier"
    assert decision.latency_ms >= 0


async def test_the_schema_offered_to_the_provider_admits_only_automatic_tiers() -> None:
    """The constraint is the schema, not a check after the model has answered.

    `frontier` is selectable by a person, so a router that offered every
    selectable tier would let the classifier spend at a level only an explicit
    human choice is supposed to reach.
    """
    captured: list[StructuredMatcherArgs] = []

    await _route(_capturing_client(captured))

    assert len(captured) == 1
    response_model = captured[0]["response_model"]
    schema = response_model.model_json_schema()
    assert schema["properties"]["tier"]["enum"] == ["standard", "deep"]
    with pytest.raises(ValueError, match="frontier"):
        response_model.model_validate({"tier": "frontier"})


async def test_the_tier_list_and_the_profile_guidance_reach_the_prompt() -> None:
    """Both are what the classifier routes on, so both have to arrive."""
    captured: list[StructuredMatcherArgs] = []

    await _route(
        _capturing_client(captured),
        guidance="Reach for deep when evidence conflicts.",
    )

    prompt = _prompt_text(captured[0])
    assert "standard: Standard -- Everyday work." in prompt
    assert "deep: Deep -- Hard judgement calls." in prompt
    assert "frontier" not in prompt
    assert "Reach for deep when evidence conflicts." in prompt


async def test_recent_history_and_attachment_names_reach_the_classifier() -> None:
    captured: list[StructuredMatcherArgs] = []

    await _route(
        _capturing_client(captured),
        history=[
            UserMessage(content="how do the two quotes compare?"),
            AssistantMessage(content="the first is cheaper but excludes labour"),
        ],
        request_text="which should I take",
        attachment_summary=["quote.pdf (application/pdf)"],
    )

    prompt = _prompt_text(captured[0])
    assert "how do the two quotes compare?" in prompt
    assert "the first is cheaper but excludes labour" in prompt
    assert "quote.pdf (application/pdf)" in prompt
    assert "which should I take" in prompt


async def test_a_classifier_that_does_not_answer_in_time_is_a_timeout() -> None:
    """A lost turn is worse than a weaker one, so this must not raise."""

    class StallingClient(RuleBasedMockLLMClient):
        """Never answers. The turn it was asked about still has to run."""

        def __init__(self) -> None:
            super().__init__(rules=[])
            self.released = asyncio.Event()

        async def generate_structured[T: BaseModel](
            self,
            messages: Sequence[LLMMessage],
            response_model: type[T],
            max_retries: int = 2,
        ) -> T:
            await self.released.wait()
            raise AssertionError("the stall is never released")

    client = StallingClient()

    decision = await _route(client, timeout_seconds=0.05)

    assert decision.outcome == "timeout"
    assert decision.tier is None
    assert decision.classifier_model == "mock-classifier"


async def test_an_answer_outside_the_offered_tiers_is_invalid_not_a_choice() -> None:
    """A model that answers off-list has not made a decision to act on.

    Constructed past validation, which is what a provider ignoring the schema
    it was handed amounts to. The router refuses to act on it either way.
    """

    def off_list(args: StructuredMatcherArgs) -> BaseModel:
        return args["response_model"].model_construct(tier="frontier")

    client = RuleBasedMockLLMClient(
        rules=[], structured_rules=[(lambda _: True, off_list)]
    )

    decision = await _route(client)

    assert decision.outcome == "invalid"
    assert decision.tier is None


async def test_an_unusable_structured_answer_is_invalid_rather_than_an_error() -> None:
    """A schema violation surfaces as `StructuredOutputError` from any client.

    Distinguished from `error` because the remedy differs: a run of these says
    look at the classifier model, not at the network.
    """
    decision = await _route(RuleBasedMockLLMClient(rules=[], structured_rules=[]))

    assert decision.outcome == "invalid"
    assert decision.tier is None


async def test_a_provider_failure_is_an_error_the_run_survives() -> None:
    class UnreachableClient(RuleBasedMockLLMClient):
        """Fails the way an unreachable provider does."""

        async def generate_structured[T: BaseModel](
            self,
            messages: Sequence[LLMMessage],
            response_model: type[T],
            max_retries: int = 2,
        ) -> T:
            raise ConnectionError("classifier endpoint unreachable")

    decision = await _route(UnreachableClient(rules=[]))

    assert decision.outcome == "error"
    assert decision.tier is None


async def test_the_request_text_never_reaches_the_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Routing runs on every turn, so logging the prompt would log the household.

    Both the deciding and the failing path: the failure paths are the ones
    tempted to log what went in.
    """
    secret = "the mortgage settlement figure is 412000"
    with caplog.at_level(logging.DEBUG, logger="family_assistant.llm.model_routing"):
        await _route(_deciding_client("deep"), request_text=secret)
        await _route(
            RuleBasedMockLLMClient(rules=[], structured_rules=[]), request_text=secret
        )

    assert secret not in caplog.text


async def test_a_profile_with_no_automatic_tiers_is_not_asked_to_choose() -> None:
    """Startup refuses this configuration; reaching it must not spend a call."""
    client = _deciding_client("deep")

    decision = await _router(client).route(
        eligibility=ModelTierEligibility(default_tier="standard"),
        guidance=None,
        history=[],
        request_text="hello",
        attachment_summary=[],
    )

    assert decision.outcome == "invalid"
    assert not client.get_calls()


def test_the_persisted_outcome_values_are_derived_from_the_type() -> None:
    """The envelope validates a stored outcome against this set.

    A hand-written second list would be free to drift from the type until a row
    this deployment itself wrote failed to load.
    """
    assert set(get_args(RoutingOutcome.__value__)) == ROUTING_OUTCOMES
