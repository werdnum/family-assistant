"""Replayed integration tests for TypeSafe's Jev classifier.

The unit tests assert the request ``JevClient`` intends to send and parse
canned answers; these record what the real ``/v1/systemone`` endpoint accepts
and returns for the three requests the application makes: the client's typed
questions, the turn relevance questions asked at a compaction event, and the
Auto tier choice.

Recording needs ``TYPESAFE_API_KEY``; replay does not, and the cassette holds
no ``authorization`` header (see ``vcr_config``'s ``filter_headers``). Jev is
deterministic for a pinned model, so a re-recording should only change the
answers when the pinned model changes.
"""

import os

import pytest

from family_assistant.config_models import TypeSafeConfig
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.llm.model_routing import JevModelRouter
from family_assistant.llm.model_selection import ModelTierEligibility, ModelTierOption
from family_assistant.llm.typesafe import ChoiceQuestion, JevClient, NoulQuestion
from family_assistant.processing.history_relevance import JevTurnRelevance

from .vcr_helpers import sanitize_response

# The shipped pin: the relevance threshold is tuned against this model.
JEV_MODEL = TypeSafeConfig().model
# Generous, because a replayed request returns at once and a recorded one must
# not be abandoned on a slow network and recorded as a timeout instead.
TIMEOUT_SECONDS = 30.0

ELIGIBILITY = ModelTierEligibility(
    default_tier="standard",
    selectable=(
        ModelTierOption(
            id="standard",
            label="Standard",
            description="Everyday requests: lookups, reminders, short answers.",
        ),
        ModelTierOption(
            id="deep",
            label="Deep",
            description="Multi-step reasoning, planning, careful judgement calls.",
        ),
    ),
    auto=frozenset({"standard", "deep"}),
)


def _client() -> JevClient:
    return JevClient(
        api_key=os.getenv("TYPESAFE_API_KEY", "test-typesafe-key"),
        model=JEV_MODEL,
        timeout_seconds=TIMEOUT_SECONDS,
    )


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_jev_answers_typed_questions() -> None:
    client = _client()
    try:
        answers = await client.ask(
            {"message": "The washing machine is leaking all over the laundry floor."},
            {
                "urgent": NoulQuestion(
                    instructions="The message describes something needing prompt action",
                    true_criteria="Damage is happening or imminent.",
                    false_criteria="Nothing needs doing soon.",
                ),
                "category": ChoiceQuestion(
                    instructions="Which part of household life is this about?",
                    options={
                        "home_maintenance": "Repairs, appliances, the house itself",
                        "finance": "Money, bills, budgets",
                        "health": "Illness, appointments, medication",
                    },
                ),
            },
        )
    finally:
        await client.close()

    assert answers.model
    assert answers.nouls["urgent"] > 0.5
    category = answers.choices["category"]
    assert set(category) == {"home_maintenance", "finance", "health"}
    assert max(category, key=lambda option: category[option]) == "home_maintenance"
    assert sum(category.values()) == pytest.approx(1.0, abs=0.01)


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_jev_ranks_the_turn_a_follow_up_continues() -> None:
    client = _client()
    relevance = JevTurnRelevance(
        client, mode="active", threshold=0.5, timeout_seconds=TIMEOUT_SECONDS
    )
    try:
        outcome = await relevance.assess(
            request="And what did the second plumber quote for the hot water system?",
            turns=[
                (
                    "plumbers",
                    "Can you find me two plumber quotes for replacing the hot water system?",
                    "Bayside Plumbing quoted $2,400 and Rapid Pipes quoted $2,150, "
                    "both including installation.",
                ),
                (
                    "weather",
                    "What's the weather this weekend?",
                    "Saturday is sunny and 24 degrees; Sunday has showers.",
                ),
            ],
        )
    finally:
        await client.close()

    assert outcome.outcome == "decided"
    assert outcome.active
    assert outcome.probabilities["plumbers"] > outcome.probabilities["weather"]
    assert outcome.relevant_keys == frozenset({"plumbers"})


@pytest.mark.no_db
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_jev_chooses_an_auto_tier_with_a_probability_for_each() -> None:
    client = _client()
    router = JevModelRouter(client, timeout_seconds=TIMEOUT_SECONDS, history_messages=6)
    try:
        decision = await router.route(
            eligibility=ELIGIBILITY,
            guidance="Use deep for anything that needs a plan weighed across "
            "several constraints; standard for everything else.",
            history=[
                UserMessage(content="We're thinking about refinancing."),
                AssistantMessage(content="Happy to help think it through."),
            ],
            request_text=(
                "Compare keeping our 6.1% fixed loan against refinancing to a "
                "5.4% variable with a $3,000 break fee, given we might sell in "
                "three years, and recommend one."
            ),
            attachment_summary=[],
        )
    finally:
        await client.close()

    assert decision.outcome == "decided"
    assert decision.probabilities is not None
    assert set(decision.probabilities) == {"standard", "deep"}
    assert decision.tier == "deep"
