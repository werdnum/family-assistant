"""One conversation, one turn at a time -- commands included.

Telegram dispatches updates concurrently, so nothing but the handler's own
bookkeeping stops two LLM loops reading and writing one conversation's history
at once, interleaving around half-finished tool calls. A slash command is an
ordinary turn with a selection attached, so it takes the chat's turn slot like
any other message: it is refused while another turn holds the slot, and while
it holds the slot a message arriving mid-turn steers it instead of starting a
second loop.

A command is refused rather than folded into the running turn as steering text
because what it chooses -- a profile, or a model tier -- was settled when that
turn began; accepting it quietly would drop the choice that was the reason for
typing the command.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast

import pytest
from telegram import Update

from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import RuleBasedMockLLMClient

from .helpers import assert_bot_sent_message
from .test_telegram_model_tier_commands import (
    ALL_TIERS,
    create_command_update,
    create_context,
    install_tiered_service,
)

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

    from .conftest import TelegramHandlerTestFixture

BUSY_REPLY = "still working on your previous request"
STEER_REPLY = "I'll apply that to the current response"


class ObservableGate(asyncio.Event):
    """A response gate that says when an LLM loop has reached the model.

    Injected through ``RuleBasedMockLLMClient(response_gate=...)``, the fake's
    own seam for holding a reply in flight: it awaits the gate before
    answering, so ``reached`` is set exactly while a loop is inside a model
    call, and the loop stays there until the test releases it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reached = asyncio.Event()

    async def wait(self) -> Literal[True]:
        self.reached.set()
        return await super().wait()


def client_saying(reply: str) -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=reply))


def gated_client(reply: str) -> tuple[RuleBasedMockLLMClient, ObservableGate]:
    gate = ObservableGate()
    return (
        RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content=reply),
            response_gate=gate,
        ),
        gate,
    )


def install_profile_service(
    fix: TelegramHandlerTestFixture,
    profile_id: str,
    command: str,
    llm_client: RuleBasedMockLLMClient,
) -> ProcessingService:
    """Register a second profile reachable by *command*, as config would."""
    template = fix.processing_service
    service = ProcessingService(
        llm_client=llm_client,
        tools_provider=template.tools_provider,
        service_config=replace(template.service_config, id=profile_id),
        context_providers=template.context_providers,
        server_url="http://test.server",
        app_config=template.app_config,
        attachment_registry=template.attachment_registry,
    )
    fix.handler.telegram_service.processing_services_registry[profile_id] = service
    fix.handler.telegram_service.slash_command_to_profile_id_map[command] = profile_id
    return service


def register_tier_commands(fix: TelegramHandlerTestFixture) -> None:
    fix.handler.telegram_service.slash_command_to_model_tier_map.update({
        "/deep": "deep",
        "/max": "frontier",
    })


async def start_plain_turn(
    fix: TelegramHandlerTestFixture,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> asyncio.Task[None]:
    """Send *text* the way a person does, and let its turn run."""
    sent = await fix.telegram_client.send_message(text)
    update = Update.de_json(sent.get("result", {}), fix.bot)
    return asyncio.create_task(fix.handler.message_handler(update, context))


async def send_plain_message(
    fix: TelegramHandlerTestFixture,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    sent = await fix.telegram_client.send_message(text)
    update = Update.de_json(sent.get("result", {}), fix.bot)
    await fix.handler.message_handler(update, context)


@pytest.mark.asyncio
async def test_a_tier_command_is_refused_while_a_turn_is_running(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """`/deep` arriving mid-turn starts no second loop over the conversation."""
    fix = telegram_handler_fixture
    standard, standard_gate = gated_client("standard served this")
    deep = client_saying("deep served this")
    install_tiered_service(
        fix,
        {"standard": standard, "deep": deep, "frontier": client_saying("frontier")},
        ALL_TIERS,
    )
    register_tier_commands(fix)
    context = create_context(fix.application, args=[])

    running = await start_plain_turn(fix, context, "start something slow")
    await wait_for_condition(
        standard_gate.reached.is_set,
        timeout=5,
        description="the running turn to reach the model",
    )

    await fix.handler.handle_tier_slash_command(
        create_command_update("/deep which contract is worse", fix.bot, message_id=501),
        create_context(fix.application, args=["which", "contract", "is", "worse"]),
    )

    assert not deep.get_calls(), (
        "the tier command ran a second LLM loop while a turn was already running"
    )
    await assert_bot_sent_message(fix.telegram_client, BUSY_REPLY, timeout=5)

    standard_gate.set()
    await asyncio.wait_for(running, timeout=10)


@pytest.mark.asyncio
async def test_a_profile_command_is_refused_while_a_turn_is_running(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """The same rule for `/complex`: the two kinds of command coordinate alike."""
    fix = telegram_handler_fixture
    gate = ObservableGate()
    cast("RuleBasedMockLLMClient", fix.mock_llm).response_gate = gate
    complex_client = client_saying("complex served this")
    install_profile_service(fix, "complex_tasks", "/complex", complex_client)
    context = create_context(fix.application, args=[])

    running = await start_plain_turn(fix, context, "start something slow")
    await wait_for_condition(
        gate.reached.is_set,
        timeout=5,
        description="the running turn to reach the model",
    )

    await fix.handler.handle_generic_slash_command(
        create_command_update("/complex plan the whole trip", fix.bot, message_id=502),
        create_context(fix.application, args=["plan", "the", "whole", "trip"]),
    )

    assert not complex_client.get_calls(), (
        "the profile command ran a second LLM loop while a turn was already running"
    )
    await assert_bot_sent_message(fix.telegram_client, BUSY_REPLY, timeout=5)

    gate.set()
    await asyncio.wait_for(running, timeout=10)


@pytest.mark.asyncio
async def test_a_message_arriving_during_a_tier_command_steers_it(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """A command turn holds the slot, so a message mid-turn steers it."""
    fix = telegram_handler_fixture
    standard = client_saying("standard served this")
    deep, deep_gate = gated_client("deep served this")
    install_tiered_service(
        fix,
        {"standard": standard, "deep": deep, "frontier": client_saying("frontier")},
        ALL_TIERS,
    )
    register_tier_commands(fix)
    context = create_context(fix.application, args=[])

    command_turn = asyncio.create_task(
        fix.handler.handle_tier_slash_command(
            create_command_update(
                "/deep which contract is worse", fix.bot, message_id=503
            ),
            create_context(fix.application, args=["which", "contract", "is", "worse"]),
        )
    )
    await wait_for_condition(
        deep_gate.reached.is_set,
        timeout=5,
        description="the tier command turn to reach the model",
    )

    await send_plain_message(fix, context, "Actually make it urgent")

    assert not standard.get_calls(), (
        "the message started its own turn instead of steering the running one"
    )
    await assert_bot_sent_message(fix.telegram_client, STEER_REPLY, timeout=5)

    deep_gate.set()
    await asyncio.wait_for(command_turn, timeout=10)
