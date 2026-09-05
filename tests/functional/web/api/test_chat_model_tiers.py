"""Choosing an intelligence level for one web request, end to end.

The web layer is where a *person* chooses a tier, so these cover the whole
path: the request is admitted against what the profile may be run on (not the
narrower list a model is held to), the turn is served by that tier's client
rather than the profile's default one, and the choice is persisted with the
reply so the conversation can say afterwards what it ran at and who chose it.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.llm.model_selection import (
    ModelTierEligibility,
    ModelTierOption,
)
from family_assistant.processing import ProcessingService
from family_assistant.storage.database import Database
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import MessageReasoningInfo

_ELIGIBILITY = ModelTierEligibility(
    default_tier="standard",
    selectable=(
        ModelTierOption(id="standard", label="Standard", description="Everyday work."),
        ModelTierOption(id="deep", label="Deep"),
        ModelTierOption(id="frontier", label="Max"),
    ),
    # `frontier` is deliberately absent: a person may choose it, a model may not.
    auto=frozenset({"standard", "deep"}),
)


def _client_saying(reply: str) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=reply))


@pytest.fixture(name="tier_clients")
def tier_clients_fixture() -> dict[str, RuleBasedMockLLMClient]:
    """One distinguishable client per tier, so the reply names what served it."""
    return {
        "standard": _client_saying("standard served this"),
        "deep": _client_saying("deep served this"),
        "frontier": _client_saying("frontier served this"),
    }


@pytest.fixture(name="tiered_app")
def tiered_app_fixture(
    app_fixture: FastAPI,
    api_test_processing_service: ProcessingService,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> FastAPI:
    """The shared API app, with its profile able to run on three tiers.

    Rebuilt rather than mutated: the client a profile runs on is fixed when the
    service is built, which is the property the whole change rests on.
    """
    config = api_test_processing_service.service_config
    config.tier_eligibility = _ELIGIBILITY
    service = ProcessingService(
        llm_client=tier_clients["standard"],
        tools_provider=api_test_processing_service.tools_provider,
        service_config=config,
        context_providers=api_test_processing_service.context_providers,
        server_url="http://testserver",
        app_config=api_test_processing_service.app_config,
        attachment_registry=api_test_processing_service.attachment_registry,
        tier_llm_clients=tier_clients,
    )
    app_fixture.state.processing_service = service
    app_fixture.state.processing_services = {config.id: service}
    return app_fixture


async def _assistant_reasoning_info(
    db_engine: AsyncEngine, conversation_id: str, interface_type: str = "api"
) -> MessageReasoningInfo:
    """The reasoning info persisted with the conversation's assistant reply."""
    db = Database(engine=db_engine)
    rows = await db.message_history.get_recent_with_metadata(
        interface_type=interface_type, conversation_id=conversation_id, limit=10
    )
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert assistant_rows, "the turn persisted no assistant reply"
    reasoning = assistant_rows[-1]["reasoning_info"]
    assert reasoning is not None
    return cast("MessageReasoningInfo", reasoning)


@pytest.mark.usefixtures("tiered_app")
async def test_a_turn_at_a_chosen_tier_is_served_by_that_tiers_client(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    conversation_id = f"tier-deep-{uuid.uuid4()}"

    response = await api_test_client.post(
        "/api/v1/chat/send_message",
        json={
            "prompt": "think hard about this",
            "conversation_id": conversation_id,
            "model_tier": "deep",
        },
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "deep served this"
    assert not tier_clients["standard"].get_calls()
    reasoning = await _assistant_reasoning_info(db_engine, conversation_id)
    assert reasoning.get("model_tier") == "deep"
    assert reasoning.get("model_tier_source") == "user"


@pytest.mark.usefixtures("tiered_app")
async def test_a_turn_naming_no_tier_stays_on_the_profile_default(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """The default is a tier too, but recorded as chosen by nobody."""
    conversation_id = f"tier-default-{uuid.uuid4()}"

    response = await api_test_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "hello", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "standard served this"
    reasoning = await _assistant_reasoning_info(db_engine, conversation_id)
    assert reasoning.get("model_tier") == "standard"
    assert reasoning.get("model_tier_source") == "default"
    assert "model_tier_requested" not in reasoning


@pytest.mark.usefixtures("tiered_app")
async def test_the_conversation_history_reports_the_tier_a_reply_ran_at(
    api_test_client: AsyncClient,
) -> None:
    """The history view reads this, so serialisation is part of the contract.

    `MessageReasoningInfo` is a TypedDict validated by pydantic, which drops
    keys it does not declare -- so a field added to the stamping side but not
    to the type would vanish exactly here, with nothing else failing.
    """
    conversation_id = f"tier-history-{uuid.uuid4()}"
    await api_test_client.post(
        "/api/v1/chat/send_message",
        json={
            "prompt": "think hard about this",
            "conversation_id": conversation_id,
            "model_tier": "deep",
        },
    )

    response = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )

    assert response.status_code == 200
    assistant = [
        message
        for message in response.json()["messages"]
        if message["role"] == "assistant"
    ]
    assert assistant, "the turn persisted no assistant reply"
    reasoning = assistant[-1]["reasoning_info"]
    assert reasoning["model_tier"] == "deep"
    assert reasoning["model_tier_source"] == "user"
    assert reasoning["model_tier_requested"] == "deep"


@pytest.mark.usefixtures("tiered_app")
async def test_a_person_may_choose_a_tier_no_model_is_allowed_to(
    api_test_client: AsyncClient,
) -> None:
    """`frontier` is outside `auto_model_tiers` but inside what a user may pick."""
    response = await api_test_client.post(
        "/api/v1/chat/send_message",
        json={
            "prompt": "this one is worth it",
            "conversation_id": f"tier-frontier-{uuid.uuid4()}",
            "model_tier": "frontier",
        },
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "frontier served this"


@pytest.mark.usefixtures("tiered_app")
async def test_an_unknown_tier_is_refused_with_the_choices_that_would_work(
    api_test_client: AsyncClient,
) -> None:
    response = await api_test_client.post(
        "/api/v1/chat/send_message",
        json={
            "prompt": "hello",
            "conversation_id": f"tier-bogus-{uuid.uuid4()}",
            "model_tier": "galactic",
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "galactic" in detail
    assert "standard" in detail


@pytest.mark.usefixtures("tiered_app")
async def test_a_streaming_turn_refuses_an_unknown_tier_before_it_starts(
    api_test_client: AsyncClient,
) -> None:
    """A refusal must be an answer about the request, not a stream error.

    The client's remedy is to change the choice, so it needs the refusal before
    it has committed to reading a stream.
    """
    response = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": str(uuid.uuid4()),
            "conversation_id": f"tier-stream-bogus-{uuid.uuid4()}",
            "prompt": "hello",
            "model_tier": "galactic",
        },
    )

    assert response.status_code == 400
    assert "galactic" in response.json()["detail"]


@pytest.mark.usefixtures("tiered_app")
async def test_a_streaming_turn_records_the_tier_it_ran_at(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    conversation_id = f"tier-stream-deep-{uuid.uuid4()}"

    post = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "prompt": "think hard about this",
            "model_tier": "deep",
        },
    )
    assert post.status_code == 200
    stream = await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
    )

    assert stream.status_code == 200
    # The tier reaches the client on turn_ended, which is what a composer reads
    # to show what the turn actually ran at.
    assert '"model_tier": "deep"' in stream.text
    reasoning = await _assistant_reasoning_info(db_engine, conversation_id)
    assert reasoning.get("model_tier") == "deep"
    assert reasoning.get("model_tier_source") == "user"


@pytest.mark.usefixtures("tiered_app")
async def test_the_profile_listing_advertises_the_tiers_a_client_may_offer(
    api_test_client: AsyncClient,
) -> None:
    """This listing is the contract a composer's intelligence control renders."""
    response = await api_test_client.get("/api/v1/profiles")

    assert response.status_code == 200
    profiles = {profile["id"]: profile for profile in response.json()["profiles"]}
    profile = profiles["chat_api_test_profile"]
    assert profile["default_model_tier"] == "standard"
    assert profile["model_tiers"] == [
        {"id": "standard", "label": "Standard", "description": "Everyday work."},
        {"id": "deep", "label": "Deep", "description": None},
        {"id": "frontier", "label": "Max", "description": None},
    ]
