"""Relevance changes compaction order only when active and successful."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from family_assistant.processing.history_relevance import RelevanceOutcome
from family_assistant.storage.database import Database
from family_assistant.storage.history_compaction import (
    HistoryScope,
    TurnMode,
    history_compaction_events_table,
)
from family_assistant.web.turn_producer import initial_turn_taint
from tests.functional.history_window_helpers import (
    CONVERSATION_ID,
    PROFILE_ID,
    Recorder,
    make_service,
    run_turn,
    seed_turn,
    texts,
)
from tests.functional.test_history_compaction import HISTORY_TOOLS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.utils.clock import MockClock

SCOPE = HistoryScope("telegram", CONVERSATION_ID, PROFILE_ID, None)


class FakeTurnRelevance:
    def __init__(self, *, active: bool, fail: bool = False) -> None:
        self.active = active
        self.fail = fail
        self.older_key: str | None = None
        self.newer_key: str | None = None
        self.requests: list[str] = []

    async def assess(
        self, *, request: str, turns: Sequence[tuple[str, str, str]]
    ) -> RelevanceOutcome:
        self.requests.append(request)
        self.older_key = next(key for key, user, _ in turns if user == "Older request")
        self.newer_key = next(key for key, user, _ in turns if user == "Newer request")
        if self.fail:
            return RelevanceOutcome("error", False, "fake-jev", 0)
        return RelevanceOutcome(
            "decided",
            self.active,
            "fake-jev",
            0,
            {self.older_key: 0.9, self.newer_key: 0.1},
        )


async def _seed_history(db: Database, mock_clock: MockClock) -> None:
    await seed_turn(
        db,
        mock_clock,
        "Other request",
        "Other answer",
        tool_calls=1,
        tool_result="other-detail " * 300,
    )
    await seed_turn(
        db,
        mock_clock,
        "Older request",
        "Older answer",
        tool_calls=1,
        tool_result="older-detail " * 300,
    )
    await seed_turn(
        db,
        mock_clock,
        "Newer request",
        "Newer answer",
        tool_calls=1,
        tool_result="newer-detail " * 300,
    )
    await seed_turn(db, mock_clock, "Newest request", "Newest answer")


async def test_active_relevance_keeps_older_turn_verbatim(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_history(db, mock_clock)
    recorder = Recorder()
    relevance = FakeTurnRelevance(active=True)
    service = make_service(
        mock_clock,
        recorder,
        budget_chars=10_000,
        tools=HISTORY_TOOLS,
        turn_relevance=relevance,
    )

    await run_turn(service, db, "Continue the older request")

    text = texts(recorder.requests[0])
    assert "older-detail " * 300 in text
    assert "newer-detail " * 300 not in text
    assert "Newer request" in text
    assert "[Called lookup" in text
    event = await db.history_compaction.latest(SCOPE)
    assert event is not None
    assert event.reason == "budget"
    assert relevance.older_key is not None
    assert relevance.newer_key is not None
    assert event.decisions[relevance.older_key].mode is TurnMode.VERBATIM
    assert event.decisions[relevance.newer_key].mode is TurnMode.COMPACTED


async def test_shadow_relevance_records_alternative_but_compacts_oldest_first(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_history(db, mock_clock)
    recorder = Recorder()
    relevance = FakeTurnRelevance(active=False)
    service = make_service(
        mock_clock,
        recorder,
        budget_chars=10_000,
        tools=HISTORY_TOOLS,
        turn_relevance=relevance,
    )

    await run_turn(service, db, "Continue the older request")

    text = texts(recorder.requests[0])
    assert "older-detail " * 300 not in text
    assert "newer-detail " * 300 in text
    event = await db.history_compaction.latest(SCOPE)
    assert event is not None
    assert event.reason == "budget"
    assert relevance.older_key is not None
    assert relevance.newer_key is not None
    assert event.decisions[relevance.older_key].mode is TurnMode.COMPACTED
    assert event.decisions[relevance.newer_key].mode is TurnMode.VERBATIM
    table = history_compaction_events_table
    row = await db.fetch_one(select(table.c.details).where(table.c.id == event.id))
    assert row is not None
    details = row["details"]
    assert details["relevance"]["outcome"] == "decided"
    assert details["relevance"]["active"] is False
    assert details["relevance"]["probabilities"] == {
        relevance.older_key: 0.9,
        relevance.newer_key: 0.1,
    }
    assert details["relevance_would_decide"][relevance.older_key] == "verbatim"
    assert details["relevance_would_decide"][relevance.newer_key] == "compacted"


async def test_relevance_failure_falls_back_to_oldest_first(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_history(db, mock_clock)
    recorder = Recorder()
    relevance = FakeTurnRelevance(active=True, fail=True)
    service = make_service(
        mock_clock,
        recorder,
        budget_chars=10_000,
        tools=HISTORY_TOOLS,
        turn_relevance=relevance,
    )

    await run_turn(service, db, "Continue the older request")

    text = texts(recorder.requests[0])
    assert "older-detail " * 300 not in text
    assert "newer-detail " * 300 in text
    event = await db.history_compaction.latest(SCOPE)
    assert event is not None
    assert relevance.older_key is not None
    assert relevance.newer_key is not None
    assert event.decisions[relevance.older_key].mode is TurnMode.COMPACTED
    assert event.decisions[relevance.newer_key].mode is TurnMode.VERBATIM
    table = history_compaction_events_table
    row = await db.fetch_one(select(table.c.details).where(table.c.id == event.id))
    assert row is not None
    assert row["details"]["relevance"]["outcome"] == "error"


async def test_a_system_triggered_turn_is_ranked_against_its_trigger(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_history(db, mock_clock)
    relevance = FakeTurnRelevance(active=True)
    service = make_service(
        mock_clock,
        Recorder(),
        budget_chars=3_000,
        tools=HISTORY_TOOLS,
        turn_relevance=relevance,
    )

    await service.handle_chat_interaction(
        db_context=db,
        interface_type="telegram",
        conversation_id=CONVERSATION_ID,
        trigger_content_parts=[
            {"type": "text", "text": "The delegated quote comparison finished."}
        ],
        trigger_interface_message_id=None,
        user_name="Alice",
        trigger_role="system",
    )

    assert relevance.requests == ["The delegated quote comparison finished."]


async def test_the_web_taint_read_ranks_against_the_coming_prompt(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_history(db, mock_clock)
    relevance = FakeTurnRelevance(active=True)
    service = make_service(
        mock_clock,
        Recorder(),
        budget_chars=3_000,
        tools=HISTORY_TOOLS,
        turn_relevance=relevance,
    )

    await initial_turn_taint(
        db,
        service,
        interface_type="telegram",
        conversation_id=CONVERSATION_ID,
        request_text="And the older one?",
    )

    assert relevance.requests == ["And the older one?"]
