"""The council flow end to end: main assistant -> council -> three members.

See docs/design/council.md. The coordinator and members are real
`ProcessingService`s on the real delegation tools, task worker, history and
completion paths; only the models are fakes, one distinguishable client per
exact-model preset. What is under test is the runtime the council depends on --
a delegated run that waits for its own background delegations, wakes once per
completed phase, keeps each member on its preset and its own history, and hands
its final turn back to whoever convened it -- not the quality of any prompt.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.model_selection import ModelTierEligibility, ModelTierOption
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage import delegation_runs_table
from family_assistant.storage.database import Database
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
    ToolsProvider,
)
from tests.conftest import cleanup_task_worker
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import (
    LLMOutput,
    MatcherArgs,
    RuleBasedMockLLMClient,
    extract_text_from_content,
    get_message_content,
    last_real_message,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.tools.types import ToolDefinition
    from family_assistant.utils.clock import MockClock

MAIN_ID = "default_assistant"
COUNCIL_ID = "council"
MEMBER_ID = "council_member"
INTERFACE_TYPE = "test_interface"
CONVERSATION_ID = "council_conversation"
USER_NAME = "CouncilTester"
TOPIC = "garden"
SEATS = {"A": "gpt_6_astra", "B": "claude_fable_5_1", "C": "kimi_k3"}
PHASES = ("independent proposal", "first review", "second review")
WAKE_TRIGGER = "System: Delegations you started have finished."
MAIN_WAKE_TRIGGER = "System: Delegated profile task completed."
DATA_MARKER = "Delegated profile task completed data."
FAILED_DATA_MARKER = "Delegated profile task failed data."


class RecordingChatInterface:
    """Records what reaches the person."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str | None:
        _ = (
            conversation_id,
            parse_mode,
            reply_to_interface_id,
            attachment_ids,
            on_behalf_of_user_id,
            taint_metadata,
        )
        self.texts.append(text)
        return f"sent-{len(self.texts)}"


def _texts(messages: list[LLMMessage]) -> list[str]:
    return [extract_text_from_content(get_message_content(m)) for m in messages]


def _last_text(messages: list[LLMMessage]) -> str:
    last = last_real_message(messages)
    return "" if last is None else extract_text_from_content(get_message_content(last))


def _delegate(target: str, request: str, **extra: str) -> ToolCallItem:
    return ToolCallItem(
        id=f"call_{uuid.uuid4().hex}",
        type="function",
        function=ToolCallFunction(
            name="delegate_to_service",
            arguments=json.dumps({
                "target_service_id": target,
                "user_request": request,
                "delivery_hint": "background",
                **extra,
            }),
        ),
    )


def _tools_provider() -> ToolsProvider:
    return PolicyEnforcingToolsProvider(
        wrapped_provider=CompositeToolsProvider(
            providers=[
                LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
                MCPToolsProvider(mcp_server_configs={}),
            ]
        ),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )


def _config(profile_id: str, **kwargs: Any) -> ProcessingServiceConfig:  # noqa: ANN401 - forwarded config fields
    return ProcessingServiceConfig(
        id=profile_id,
        prompts={"system_prompt": f"You are the {profile_id} profile."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=200,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        **kwargs,
    )


@dataclass
class Coordinator:
    """A scripted council coordinator, following the skill's phase structure.

    It learns everything from its own history -- each member's latest reference
    and report come from the result rows the runtime pins into the wake --
    because that is all a real coordinator has after a wake.
    """

    wakes: list[list[str]] = field(default_factory=list)
    live_wake_triggers: list[int] = field(default_factory=list)

    def client(self) -> RuleBasedMockLLMClient:
        return RuleBasedMockLLMClient(
            rules=[
                (self._after_launch, LLMOutput(content="Phase under way.")),
                (self._is_wake, self._on_wake),
                (lambda _kwargs: True, self._launch_proposals),
            ]
        )

    @staticmethod
    def _after_launch(kwargs: MatcherArgs) -> bool:
        last = last_real_message(kwargs["messages"])
        return last is not None and last.role == "tool"

    @staticmethod
    def _is_wake(kwargs: MatcherArgs) -> bool:
        return _last_text(kwargs["messages"]).startswith(WAKE_TRIGGER)

    @staticmethod
    def _launch_proposals(_kwargs: MatcherArgs) -> LLMOutput:
        return LLMOutput(
            content="Convening the council.",
            tool_calls=[
                _delegate(
                    MEMBER_ID,
                    f"[Council: {TOPIC}; member {label}; {PHASES[0]}]\nBrief: plan the garden.",
                    model_tier=preset,
                )
                for label, preset in SEATS.items()
            ],
        )

    def _on_wake(self, kwargs: MatcherArgs) -> LLMOutput:
        texts = _texts(kwargs["messages"])
        self.wakes.append(texts)
        self.live_wake_triggers.append(
            sum(
                1
                for message, text in zip(kwargs["messages"], texts, strict=True)
                if message.role == "system" and text.startswith(WAKE_TRIGGER)
            )
        )
        latest: dict[str, tuple[str, str]] = {}
        failed: list[str] = []
        for text in texts:
            if not text.startswith((DATA_MARKER, FAILED_DATA_MARKER)):
                continue
            label = re.search(r"member ([ABC]);", text)
            reference = re.search(r"Delegation reference: (\S+)", text)
            assert label and reference
            if text.startswith(FAILED_DATA_MARKER):
                failed.append(label.group(1))
                continue
            report = text.split("Delegated result:\n", 1)[1]
            latest[label.group(1)] = (reference.group(1), report)

        if failed:
            return LLMOutput(
                content=f"PARTIAL: member {', '.join(failed)} failed; "
                + " | ".join(report for _ref, report in latest.values())
            )
        phase = len(self.wakes)
        if phase == len(PHASES):
            return LLMOutput(
                content="SYNTHESIS: "
                + " | ".join(latest[label][1] for label in sorted(latest))
            )
        exchanged = "\n".join(
            f"Report {label}: {latest[label][1]}" for label in sorted(latest)
        )
        return LLMOutput(
            content=f"Starting the {PHASES[phase]}.",
            tool_calls=[
                _delegate(
                    MEMBER_ID,
                    f"[Council: {TOPIC}; member {label}; {PHASES[phase]}]\n{exchanged}",
                    model_tier=preset,
                    resume_delegation_id=latest[label][0],
                )
                for label, preset in SEATS.items()
            ],
        )


@dataclass
class Seat:
    """A seat's model: says which model it is and which phase it answered.

    It snapshots what each call was given, since the loop goes on appending to
    the same message list after the call returns.
    """

    preset: str
    gate: asyncio.Event | None = None
    requests: list[list[str]] = field(default_factory=list)

    def client(self) -> RuleBasedMockLLMClient:
        return RuleBasedMockLLMClient(
            rules=[(lambda _kwargs: True, self._answer)], response_gate=self.gate
        )

    def _answer(self, kwargs: MatcherArgs) -> LLMOutput:
        self.requests.append(_texts(kwargs["messages"]))
        request = _last_text(kwargs["messages"])
        phase = next(name for name in PHASES if f"; {name}]" in request)
        return LLMOutput(content=f"REPORT[{self.preset}:{phase}]")


class FailingSeat(RuleBasedMockLLMClient):
    """A seat whose provider is down.

    A subclass rather than a rule: the rule evaluator catches whatever a rule
    raises and falls through to the default response.
    """

    def __init__(self) -> None:
        super().__init__(rules=[])

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        raise RuntimeError("provider unavailable")


def _main_client() -> RuleBasedMockLLMClient:
    def relay(kwargs: MatcherArgs) -> LLMOutput:
        result = next(
            text for text in _texts(kwargs["messages"]) if "Delegated result:" in text
        )
        return LLMOutput(
            content="Council says: " + result.split("Delegated result:\n", 1)[1]
        )

    return RuleBasedMockLLMClient(
        rules=[
            (
                lambda kwargs: _last_text(kwargs["messages"]).startswith(
                    MAIN_WAKE_TRIGGER
                ),
                relay,
            ),
            (Coordinator._after_launch, LLMOutput(content="The council is convened.")),
            (
                lambda _kwargs: True,
                LLMOutput(
                    content="Convening.",
                    tool_calls=[
                        _delegate(COUNCIL_ID, "Convene a council on the garden.")
                    ],
                ),
            ),
        ]
    )


@dataclass
class Council:
    main: ProcessingService
    coordinator: Coordinator
    seats: dict[str, Seat]
    unselected: RuleBasedMockLLMClient
    chat: RecordingChatInterface
    engine: AsyncEngine

    async def convene(self) -> str | None:
        result = await self.main.handle_chat_interaction(
            db_context=Database(engine=self.engine),
            interface_type=INTERFACE_TYPE,
            conversation_id=CONVERSATION_ID,
            trigger_content_parts=[{"type": "text", "text": "Ask the council."}],
            trigger_interface_message_id="msg_council",
            user_name=USER_NAME,
            chat_interface=cast("ChatInterface", self.chat),
            request_confirmation_callback=None,
        )
        assert result.error_traceback is None
        return result.text_reply

    async def runs(self, target: str) -> list[Any]:
        return list(
            await Database(engine=self.engine).fetch_all(
                select(delegation_runs_table)
                .where(delegation_runs_table.c.target_service_id == target)
                .order_by(delegation_runs_table.c.created_at)
            )
        )

    def seat_requests(self, preset: str) -> list[list[str]]:
        """The messages each call to a seat's model was given."""
        return self.seats[preset].requests


def _build_council(
    engine: AsyncEngine,
    seats: dict[str, Seat],
    failing: dict[str, RuleBasedMockLLMClient] | None = None,
) -> Council:
    coordinator = Coordinator()
    unselected = RuleBasedMockLLMClient(
        rules=[], default_response=LLMOutput(content="REPORT[unselected]")
    )
    main = ProcessingService(
        llm_client=_main_client(),
        tools_provider=_tools_provider(),
        service_config=_config(MAIN_ID),
        context_providers=[],
        server_url=None,
        app_config=AppConfig(),
    )
    council = ProcessingService(
        llm_client=coordinator.client(),
        tools_provider=_tools_provider(),
        service_config=_config(COUNCIL_ID),
        context_providers=[],
        server_url=None,
        app_config=AppConfig(),
    )
    member = ProcessingService(
        llm_client=unselected,
        tools_provider=_tools_provider(),
        service_config=_config(
            MEMBER_ID,
            tier_eligibility=ModelTierEligibility(
                default_tier="deep",
                selectable=(ModelTierOption(id="deep", label="Deep"),),
                delegation=tuple(
                    ModelTierOption(id=preset, label=preset)
                    for preset in SEATS.values()
                ),
            ),
        ),
        context_providers=[],
        server_url=None,
        app_config=AppConfig(),
        tier_llm_clients={
            "deep": unselected,
            **{preset: seat.client() for preset, seat in seats.items()},
            **(failing or {}),
        },
    )
    registry = {MAIN_ID: main, COUNCIL_ID: council, MEMBER_ID: member}
    for service in registry.values():
        service.processing_services_registry = registry
    return Council(
        main=main,
        coordinator=coordinator,
        seats=seats,
        unselected=unselected,
        chat=RecordingChatInterface(),
        engine=engine,
    )


@asynccontextmanager
async def _workers(
    council: Council,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    db_engine: AsyncEngine,
    mock_clock: MockClock,
) -> AsyncIterator[None]:
    """Two workers, as production runs, so members can finish out of order."""
    _worker, new_task_event, _shutdown = task_worker_manager(
        council.main, cast("MagicMock", council.chat)
    )
    shutdown = asyncio.Event()
    second = TaskWorker(
        processing_service=council.main,
        chat_interface=cast("ChatInterface", council.chat),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=cast("Any", None),
        shutdown_event_instance=shutdown,
        engine=db_engine,
        clock=mock_clock,
    )
    second.register_task_handler(
        "delegated_profile_run", second.handle_delegated_profile_run
    )
    handle = asyncio.create_task(second.run(new_task_event))
    try:
        yield
    finally:
        await cleanup_task_worker(handle, shutdown, new_task_event)


def _seats() -> dict[str, Seat]:
    return {preset: Seat(preset) for preset in SEATS.values()}


@pytest.mark.asyncio
async def test_the_council_runs_every_phase_and_answers_its_caller(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    """Three phases, three seats, one synthesis -- delivered through the main assistant."""
    council = _build_council(db_engine, _seats())

    async with _workers(council, task_worker_manager, db_engine, mock_clock):
        assert await council.convene() == "The council is convened."
        await wait_for_condition(
            lambda: any(
                text.startswith("Council says: SYNTHESIS")
                for text in council.chat.texts
            ),
            timeout=60,
            description="the synthesis reaching the person",
        )

    delivered = [text for text in council.chat.texts if text.startswith("Council says")]
    assert delivered == [
        "Council says: SYNTHESIS: "
        + " | ".join(f"REPORT[{preset}:second review]" for preset in SEATS.values())
    ]
    # One wake per completed phase, never one per member.
    assert len(council.coordinator.wakes) == len(PHASES)
    [council_run] = await council.runs(COUNCIL_ID)
    assert council_run["status"] == "completed"
    assert council_run["result_text"].startswith("SYNTHESIS")


@pytest.mark.asyncio
async def test_earlier_phase_wakes_are_not_replayed_as_instructions(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    """Each wake is the one live system trigger; earlier ones are history."""
    council = _build_council(db_engine, _seats())

    async with _workers(council, task_worker_manager, db_engine, mock_clock):
        await council.convene()
        await wait_for_condition(
            lambda: len(council.coordinator.wakes) == len(PHASES),
            timeout=60,
            description="the coordinator woken for every phase",
        )

    assert council.coordinator.live_wake_triggers == [1] * len(PHASES)


@pytest.mark.asyncio
async def test_each_seat_keeps_its_model_and_its_own_history(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    """Proposals are independent; later phases see the whole prior phase.

    Every resume names its seat's preset again, so each seat's three turns
    reach that seat's model, share one subconversation, and never reach the
    member profile's own default model.
    """
    council = _build_council(db_engine, _seats())

    async with _workers(council, task_worker_manager, db_engine, mock_clock):
        await council.convene()
        await wait_for_condition(
            lambda: any("SYNTHESIS" in text for text in council.chat.texts),
            timeout=60,
            description="the synthesis reaching the person",
        )

    assert not council.unselected.get_calls()
    member_runs = await council.runs(MEMBER_ID)
    for preset in SEATS.values():
        seat_runs = [
            run
            for run in member_runs
            if (run["model_selection_json"] or {}).get("tier") == preset
        ]
        assert len(seat_runs) == len(PHASES)
        assert len({run["subconversation_id"] for run in seat_runs}) == 1

        proposal, first_review, second_review = council.seat_requests(preset)
        assert not any("REPORT[" in text for text in proposal)
        for other in SEATS.values():
            assert any(
                f"REPORT[{other}:independent proposal]" in t for t in first_review
            )
            assert any(f"REPORT[{other}:first review]" in t for t in second_review)
        # Its own earlier answer is in its history, not only in what it was sent.
        assert f"REPORT[{preset}:independent proposal]" in second_review


@pytest.mark.asyncio
async def test_the_council_waits_for_its_slowest_member(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    """Neither the coordinator's first turn nor its first finished member is an answer."""
    gate = asyncio.Event()
    seats = _seats()
    seats["gpt_6_astra"] = Seat("gpt_6_astra", gate)
    council = _build_council(db_engine, seats)

    async def others_done_while_sol_runs() -> bool:
        runs = {
            run["model_selection_json"]["tier"]: run
            for run in await council.runs(MEMBER_ID)
        }
        return (
            len(runs) == len(SEATS)
            and runs["claude_fable_5_1"]["status"] == "completed"
            and runs["kimi_k3"]["status"] == "completed"
            and runs["gpt_6_astra"]["status"] == "running"
        )

    async with _workers(council, task_worker_manager, db_engine, mock_clock):
        await council.convene()
        await wait_for_condition(
            others_done_while_sol_runs,
            timeout=30,
            description="two of three seats done",
        )
        [waiting] = await council.runs(COUNCIL_ID)
        coordinator_wakes_while_waiting = len(council.coordinator.wakes)
        gate.set()
        await wait_for_condition(
            lambda: any("SYNTHESIS" in text for text in council.chat.texts),
            timeout=60,
            description="the synthesis reaching the person",
        )

    assert waiting["status"] == "awaiting_children"
    assert coordinator_wakes_while_waiting == 0
    assert [text for text in council.chat.texts if text.startswith("Council says")] == [
        next(text for text in council.chat.texts if "SYNTHESIS" in text)
    ]


@pytest.mark.asyncio
async def test_a_failed_seat_reaches_the_coordinator_with_the_others(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    """A failure is a finished member: it neither strands the phase nor goes missing."""
    council = _build_council(db_engine, _seats(), failing={"kimi_k3": FailingSeat()})

    async with _workers(council, task_worker_manager, db_engine, mock_clock):
        await council.convene()
        await wait_for_condition(
            lambda: any("PARTIAL" in text for text in council.chat.texts),
            timeout=60,
            description="the partial result reaching the person",
        )

    [partial] = [text for text in council.chat.texts if "PARTIAL" in text]
    assert "member C failed" in partial
    assert "REPORT[gpt_6_astra:independent proposal]" in partial
    assert "REPORT[claude_fable_5_1:independent proposal]" in partial
    assert len(council.coordinator.wakes) == 1
