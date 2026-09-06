"""Choosing how hard the assistant thinks about one Telegram message.

Telegram has no composer control, so a tier is chosen the way a profile is:
with a leading command. What these pin is that the two kinds of command stay
different things -- a tier command changes the models a message runs on and
nothing else, keeps the conversation on the profile it was already using
(including the profile a reply adopts), applies to that message only, and is
refused out loud where the profile does not offer the tier.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.model_selection import (
    ModelTierEligibility,
    ModelTierOption,
)
from family_assistant.processing import ProcessingService
from tests.mocks.mock_llm import RuleBasedMockLLMClient

from .helpers import wait_for_bot_response

if TYPE_CHECKING:
    from telegram import Bot
    from telegram.ext import Application

    from family_assistant.llm.messages import MessageReasoningInfo
    from tests.mocks.telegram_test_server import TestServerUpdate

    from .conftest import TelegramHandlerTestFixture

CHAT_ID = 123
USER_ID = 12345

_STANDARD = ModelTierOption(id="standard", label="Standard")
_DEEP = ModelTierOption(id="deep", label="Deep")
_FRONTIER = ModelTierOption(id="frontier", label="Max")

ALL_TIERS = ModelTierEligibility(
    default_tier="standard",
    selectable=(_STANDARD, _DEEP, _FRONTIER),
    # `frontier` is deliberately outside what a model may pick; a person asking
    # for it in Telegram is authorizing it themselves.
    auto=frozenset({"standard", "deep"}),
)
NO_FRONTIER = ModelTierEligibility(
    default_tier="standard",
    selectable=(_STANDARD, _DEEP),
    auto=frozenset({"standard"}),
)


def _client_saying(reply: str) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=reply))


@pytest.fixture(name="tier_clients")
def tier_clients_fixture() -> dict[str, RuleBasedMockLLMClient]:
    """One distinguishable client per tier, so a reply names what served it."""
    return {
        "standard": _client_saying("standard served this"),
        "deep": _client_saying("deep served this"),
        "frontier": _client_saying("frontier served this"),
    }


def install_tiered_service(
    fix: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
    eligibility: ModelTierEligibility,
    *,
    profile_id: str | None = None,
) -> ProcessingService:
    """Give a profile three tiers, each with its own client, and register it.

    Rebuilt rather than mutated: the clients a profile can run on are fixed
    when its service is built, which is the property the tier commands rest on.
    """
    template = fix.processing_service
    config = replace(
        template.service_config,
        id=profile_id or template.service_config.id,
        tier_eligibility=eligibility,
    )
    service = ProcessingService(
        llm_client=tier_clients["standard"],
        tools_provider=template.tools_provider,
        service_config=config,
        context_providers=template.context_providers,
        server_url="http://test.server",
        app_config=template.app_config,
        attachment_registry=template.attachment_registry,
        tier_llm_clients=tier_clients,
    )
    fix.handler.telegram_service.processing_services_registry[config.id] = service
    if profile_id is None:
        fix.handler.processing_service = service
    return service


@pytest.fixture(name="tier_commands", autouse=True)
def tier_commands_fixture(telegram_handler_fixture: TelegramHandlerTestFixture) -> None:
    """The commands the shipped tiers claim, as configuration would register."""
    telegram_handler_fixture.handler.telegram_service.slash_command_to_model_tier_map.update({
        "/deep": "deep",
        "/max": "frontier",
    })


def create_context(
    application: Application[Any, Any, Any, Any, Any, Any],
    args: list[str],
) -> ContextTypes.DEFAULT_TYPE:
    context = ContextTypes.DEFAULT_TYPE(
        application=application, chat_id=CHAT_ID, user_id=USER_ID
    )
    context.args = args
    return context


def create_command_update(
    text: str,
    bot: Bot,
    message_id: int = 401,
    reply_to_message_id: int | None = None,
) -> Update:
    """One command message, bound to the bot its replies go back through."""
    user = User(id=USER_ID, first_name="TestUser", is_bot=False)
    chat = Chat(id=CHAT_ID, type="private")
    reply_to = (
        Message(
            message_id=reply_to_message_id,
            date=datetime.now(UTC),
            chat=chat,
            from_user=User(id=1, first_name="Assistant", is_bot=True),
            text="an earlier answer",
        )
        if reply_to_message_id is not None
        else None
    )
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=text,
        reply_to_message=reply_to,
    )
    message.set_bot(bot)
    update = Update(update_id=uuid.uuid4().int & ((1 << 31) - 1), message=message)
    update.set_bot(bot)
    return update


async def run_command(
    fix: TelegramHandlerTestFixture,
    text: str,
    *,
    message_id: int = 401,
    reply_to_message_id: int | None = None,
) -> None:
    """Send one `/command <request>` message the way Telegram dispatches it."""
    _, _, request = text.partition(" ")
    update = create_command_update(
        text,
        fix.bot,
        message_id=message_id,
        reply_to_message_id=reply_to_message_id,
    )
    context = create_context(fix.application, args=request.split())
    await fix.handler.handle_tier_slash_command(update, context)


async def last_bot_text(fix: TelegramHandlerTestFixture) -> str:
    """The text of the last message the bot actually sent to the chat."""
    responses: list[TestServerUpdate] = await wait_for_bot_response(
        fix.telegram_client, timeout=5.0
    )
    assert responses, "the bot sent nothing"
    return responses[-1].get("message", {}).get("text", "")


async def assistant_reasoning_info(
    fix: TelegramHandlerTestFixture,
) -> MessageReasoningInfo:
    rows = await fix.database.message_history.get_recent_with_metadata(
        interface_type="telegram", conversation_id=str(CHAT_ID), limit=20
    )
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert assistant_rows, "the turn persisted no assistant reply"
    reasoning = assistant_rows[-1]["reasoning_info"]
    assert reasoning is not None
    return cast("MessageReasoningInfo", reasoning)


@pytest.mark.asyncio
async def test_deep_runs_the_message_on_the_deep_tier(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """The person's own choice, recorded as theirs."""
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, ALL_TIERS)

    await run_command(fix, "/deep which of these two contracts is worse")

    assert tier_clients["deep"].get_calls()
    assert not tier_clients["standard"].get_calls()
    reasoning = await assistant_reasoning_info(fix)
    assert reasoning.get("model_tier") == "deep"
    assert reasoning.get("model_tier_requested") == "deep"
    assert reasoning.get("model_tier_source") == "user"
    assert "deep served this" in await last_bot_text(fix)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_text",
    [
        # A group chat addresses a command to one bot; Telegram delivers it to
        # the handler registered for `/deep` with the suffix still attached.
        "/deep@FamilyAssistantBot which of these two contracts is worse",
        # Telegram matches a command case-insensitively, so this reaches the
        # same handler too.
        "/DEEP which of these two contracts is worse",
    ],
)
async def test_a_tier_command_telegram_spells_differently_still_runs_the_tier(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
    command_text: str,
) -> None:
    """What arrives is not what was configured, and it means the same thing.

    Reporting a configuration error for a command Telegram itself accepted
    would refuse a request the person spelled correctly.
    """
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, ALL_TIERS)

    await run_command(fix, command_text)

    assert tier_clients["deep"].get_calls()
    assert not tier_clients["standard"].get_calls()
    reasoning = await assistant_reasoning_info(fix)
    assert reasoning.get("model_tier") == "deep"
    assert reasoning.get("model_tier_source") == "user"


@pytest.mark.asyncio
async def test_max_runs_the_message_on_the_frontier_tier(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """`/max` is the product name for the `frontier` recipe."""
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, ALL_TIERS)

    await run_command(fix, "/max read these three quotes and rank them")

    assert tier_clients["frontier"].get_calls()
    assert not tier_clients["standard"].get_calls()
    reasoning = await assistant_reasoning_info(fix)
    assert reasoning.get("model_tier") == "frontier"
    assert reasoning.get("model_tier_source") == "user"


@pytest.mark.asyncio
async def test_a_tier_command_with_no_request_asks_for_one(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """A bare command is a half-typed message, not an empty request to answer."""
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, ALL_TIERS)

    await run_command(fix, "/deep")

    assert not any(client.get_calls() for client in tier_clients.values())
    assert "followed by your request" in await last_bot_text(fix)


@pytest.mark.asyncio
async def test_a_tier_the_profile_does_not_offer_is_refused_out_loud(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """Silently running the default tier would answer a question nobody asked.

    The person chose to spend more; a quiet downgrade would look like they got
    what they asked for.
    """
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, NO_FRONTIER)

    await run_command(fix, "/max this one is worth it")

    assert not any(client.get_calls() for client in tier_clients.values())
    refusal = await last_bot_text(fix)
    assert "frontier" in refusal
    assert "standard, deep" in refusal


@pytest.mark.asyncio
async def test_a_tier_command_keeps_the_profile_a_reply_adopts(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """A tier changes the models, never which assistant answers.

    Replying to another profile's message adopts that profile for a plain
    message, so it does for a tier command too -- otherwise `/deep` on an
    Engineer thread would quietly move the conversation to the Assistant.
    """
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, ALL_TIERS)
    other_clients = {
        "standard": _client_saying("other profile, standard"),
        "deep": _client_saying("other profile, deep"),
        "frontier": _client_saying("other profile, frontier"),
    }
    other = install_tiered_service(
        fix, other_clients, ALL_TIERS, profile_id="specialist_profile"
    )
    await fix.database.message_history.add_message(
        UserMessage(content="an earlier answer"),
        interface_type="telegram",
        conversation_id=str(CHAT_ID),
        timestamp=datetime.now(UTC),
        interface_message_id="390",
        processing_profile_id=other.service_config.id,
    )

    await run_command(fix, "/deep and now the follow-up", reply_to_message_id=390)

    assert other_clients["deep"].get_calls()
    assert not tier_clients["deep"].get_calls()


@pytest.mark.asyncio
async def test_a_tier_command_on_a_reply_stays_in_that_thread(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """A reply continues a thread, and a command on it does not start a new one.

    The profile a reply adopts and the thread it belongs to come from the same
    replied-to row, so resolving one without the other would answer inside the
    right conversation while filing the turn outside it.
    """
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, ALL_TIERS)
    root_id = await fix.database.message_history.add_message(
        UserMessage(content="an earlier answer"),
        interface_type="telegram",
        conversation_id=str(CHAT_ID),
        timestamp=datetime.now(UTC),
        interface_message_id="390",
    )

    await run_command(fix, "/deep and now the follow-up", reply_to_message_id=390)

    rows = await fix.database.message_history.get_recent_with_metadata(
        interface_type="telegram", conversation_id=str(CHAT_ID), limit=20
    )
    turn_rows = [row for row in rows if row["internal_id"] != root_id]
    assert turn_rows, "the tier command persisted nothing"
    assert {row["thread_root_id"] for row in turn_rows} == {root_id}


@pytest.mark.asyncio
async def test_a_profile_command_still_chooses_a_profile(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """Tier commands are registered beside profile commands, not over them."""
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, ALL_TIERS)
    specialist_client = _client_saying("the specialist answered")
    specialist = install_tiered_service(
        fix,
        {
            "standard": specialist_client,
            "deep": _client_saying("specialist deep"),
            "frontier": _client_saying("specialist frontier"),
        },
        ALL_TIERS,
        profile_id="specialist_profile",
    )
    fix.handler.telegram_service.slash_command_to_profile_id_map["/specialist"] = (
        specialist.service_config.id
    )

    update = create_command_update("/specialist do the specialist thing", fix.bot)
    context = create_context(fix.application, args=["do", "the", "specialist", "thing"])
    await fix.handler.handle_generic_slash_command(update, context)

    assert specialist_client.get_calls()
    reasoning = await assistant_reasoning_info(fix)
    assert reasoning.get("model_tier") == "standard"
    assert reasoning.get("model_tier_source") == "default"


@pytest.mark.asyncio
async def test_a_profile_command_addressed_to_the_bot_still_chooses_the_profile(
    telegram_handler_fixture: TelegramHandlerTestFixture,
    tier_clients: dict[str, RuleBasedMockLLMClient],
) -> None:
    """Both kinds of command are normalised by the same shared step."""
    fix = telegram_handler_fixture
    install_tiered_service(fix, tier_clients, ALL_TIERS)
    specialist_client = _client_saying("the specialist answered")
    specialist = install_tiered_service(
        fix,
        {
            "standard": specialist_client,
            "deep": _client_saying("specialist deep"),
            "frontier": _client_saying("specialist frontier"),
        },
        ALL_TIERS,
        profile_id="specialist_profile",
    )
    fix.handler.telegram_service.slash_command_to_profile_id_map["/specialist"] = (
        specialist.service_config.id
    )

    update = create_command_update(
        "/specialist@FamilyAssistantBot do the specialist thing", fix.bot
    )
    context = create_context(fix.application, args=["do", "the", "specialist", "thing"])
    await fix.handler.handle_generic_slash_command(update, context)

    assert specialist_client.get_calls()
