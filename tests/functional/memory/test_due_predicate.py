"""Which conversations the sweep finds due, and which it must not.

Slice 4 of docs/design/conversation-memory.md, "Reviews are scheduled from
state, not from events". Each test here is one row of the eligibility rule: a
source the design excludes, a window it measures, or a boundary it respects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.memory.due import (
    DueReason,
    select_due_conversations,
    select_review_rows,
)
from family_assistant.memory.review_settings import MemoryReviewSettings
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

NOW = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)
ENABLED_AT = NOW - timedelta(days=7)
CONTRIBUTOR = "default_assistant"
CONVERSATION = "conv-1"

SETTINGS = MemoryReviewSettings(
    idle_window_minutes={"web": 30, "telegram": 90},
    max_deferral_hours=24,
    contributing_interfaces=frozenset({"web", "telegram"}),
)
CONTRIBUTING = {CONTRIBUTOR: ENABLED_AT}


async def _say(
    db: Database,
    *,
    minutes_ago: float,
    interface_type: str = "web",
    conversation_id: str = CONVERSATION,
    profile_id: str | None = CONTRIBUTOR,
    role: str = "user",
    is_internal: bool = False,
    subconversation_id: str | None = None,
) -> int:
    """Persist one row at ``minutes_ago`` before :data:`NOW`."""
    message = (
        UserMessage(content="a thing was said")
        if role == "user"
        else AssistantMessage(content="a thing was answered")
    )
    return await db.message_history.add_message(
        message,
        interface_type=interface_type,
        conversation_id=conversation_id,
        timestamp=NOW - timedelta(minutes=minutes_ago),
        turn_id="turn-1",
        processing_profile_id=profile_id,
        subconversation_id=subconversation_id,
        is_internal=is_internal,
        user_id="alice" if role == "user" else None,
    )


async def _due(
    db: Database,
    *,
    now: datetime = NOW,
    settings: MemoryReviewSettings = SETTINGS,
    contributing: dict[str, datetime] | None = None,
) -> list[str]:
    conversations = await select_due_conversations(
        db,
        now=now,
        settings=settings,
        contributing_profiles=CONTRIBUTING if contributing is None else contributing,
    )
    return [conversation.conversation_id for conversation in conversations]


# ---------------------------------------------------------------------------
# The idle window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_conversation_with_recent_activity_is_not_due(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=5)

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_the_same_conversation_is_due_once_the_window_passes(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=5)

    assert await _due(db, now=NOW + timedelta(minutes=30)) == [CONVERSATION]


@pytest.mark.asyncio
async def test_the_window_is_per_interface(db_engine: AsyncEngine) -> None:
    """Telegram is bursty; a reply twenty minutes later is the same exchange."""
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45, interface_type="web", conversation_id="web-conv")
    await _say(db, minutes_ago=45, interface_type="telegram", conversation_id="tg-conv")

    assert await _due(db) == ["web-conv"]


@pytest.mark.asyncio
async def test_the_longer_window_still_admits_a_quiet_telegram_chat(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(
        db, minutes_ago=120, interface_type="telegram", conversation_id="tg-conv"
    )

    assert await _due(db) == ["tg-conv"]


@pytest.mark.asyncio
async def test_a_continuously_active_conversation_becomes_due_by_deferral(
    db_engine: AsyncEngine,
) -> None:
    """The clause that guarantees a busy group chat is reviewed at all."""
    db = Database(engine=db_engine)
    for minutes_ago in (30 * 60, 20 * 60, 10 * 60, 5):
        await _say(
            db,
            minutes_ago=minutes_ago,
            interface_type="telegram",
            conversation_id="tg-conv",
        )

    conversations = await select_due_conversations(
        db, now=NOW, settings=SETTINGS, contributing_profiles=CONTRIBUTING
    )

    assert [c.conversation_id for c in conversations] == ["tg-conv"]
    assert conversations[0].reason is DueReason.MAX_DEFERRAL


@pytest.mark.asyncio
async def test_an_idle_conversation_reports_the_idle_reason(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45)

    conversations = await select_due_conversations(
        db, now=NOW, settings=SETTINGS, contributing_profiles=CONTRIBUTING
    )

    assert conversations[0].reason is DueReason.IDLE


# ---------------------------------------------------------------------------
# What counts as activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_assistant_only_tail_is_not_due(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45, role="assistant")

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_an_internal_user_row_is_not_activity(db_engine: AsyncEngine) -> None:
    """An automation-triggered turn is the application speaking, not a person."""
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45, is_internal=True)

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_one_real_user_row_among_internal_ones_is_enough(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=50, is_internal=True)
    await _say(db, minutes_ago=45)

    assert await _due(db) == [CONVERSATION]


# ---------------------------------------------------------------------------
# Sources the design excludes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("interface_type", ["telephone", "voice"])
async def test_a_spoken_interface_never_contributes(
    db_engine: AsyncEngine, interface_type: str
) -> None:
    """The pinned exclusion: spoken interfaces read memory but do not feed it.

    Aged past the maximum deferral as well as the idle window, so the only
    thing that can exclude it is the contributing-interface filter itself.
    """
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=30 * 60, interface_type=interface_type)

    assert await _due(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interface_type", ["email", "a2a", "api", "research"])
async def test_a_non_contributing_interface_is_never_eligible(
    db_engine: AsyncEngine, interface_type: str
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=30 * 60, interface_type=interface_type)

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_a_subconversation_row_is_never_eligible(
    db_engine: AsyncEngine,
) -> None:
    """A delegation subconversation, and the curator's own rows, live here."""
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45, subconversation_id="sub-1")

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_a_non_contributing_profile_is_never_eligible(
    db_engine: AsyncEngine,
) -> None:
    """A slash command inside a contributing chat switches the profile."""
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45, profile_id="engineer")

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_a_conversation_is_not_due_on_a_non_contributing_profile_alone(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45, profile_id="engineer")
    await _say(db, minutes_ago=45, role="assistant")

    assert await _due(db) == []


# ---------------------------------------------------------------------------
# The enablement boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_before_the_enablement_moment_are_never_selected(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=14 * 24 * 60)

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_an_excluded_backlog_does_not_hurry_a_review(
    db_engine: AsyncEngine,
) -> None:
    """The deferral clause is measured over admitted rows only."""
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=30 * 24 * 60)
    await _say(db, minutes_ago=5)

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_an_excluded_backlog_does_not_delay_a_review(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=30 * 24 * 60)
    await _say(db, minutes_ago=45)

    assert await _due(db) == [CONVERSATION]


@pytest.mark.asyncio
async def test_re_enablement_moves_the_boundary(db_engine: AsyncEngine) -> None:
    """Rows written while contribution was off are never eligible."""
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45)

    assert await _due(db, contributing={CONTRIBUTOR: NOW - timedelta(minutes=30)}) == []


# ---------------------------------------------------------------------------
# The watermark
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_at_or_before_the_watermark_are_not_eligible(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    last = await _say(db, minutes_ago=45)
    await db.memory_review.advance_watermark(
        interface_type="web",
        conversation_id=CONVERSATION,
        last_reviewed_internal_id=last,
        now=NOW,
    )

    assert await _due(db) == []


@pytest.mark.asyncio
async def test_a_row_after_the_watermark_makes_the_conversation_due_again(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    reviewed = await _say(db, minutes_ago=90)
    await db.memory_review.advance_watermark(
        interface_type="web",
        conversation_id=CONVERSATION,
        last_reviewed_internal_id=reviewed,
        now=NOW,
    )
    fresh = await _say(db, minutes_ago=45)

    conversations = await select_due_conversations(
        db, now=NOW, settings=SETTINGS, contributing_profiles=CONTRIBUTING
    )

    assert len(conversations) == 1
    assert conversations[0].watermark == reviewed
    assert conversations[0].first_eligible_internal_id == fresh
    assert conversations[0].last_eligible_internal_id == fresh


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_due_when_memory_is_off(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45)

    settings = MemoryReviewSettings(
        enabled=False,
        idle_window_minutes=dict(SETTINGS.idle_window_minutes),
        contributing_interfaces=SETTINGS.contributing_interfaces,
    )

    assert await _due(db, settings=settings) == []


@pytest.mark.asyncio
async def test_nothing_is_due_when_no_profile_contributes(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _say(db, minutes_ago=45)

    assert await _due(db, contributing={}) == []


# ---------------------------------------------------------------------------
# The rows one review reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_review_rows_are_the_eligible_ones_after_the_watermark(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    reviewed = await _say(db, minutes_ago=200)
    await _say(db, minutes_ago=180, profile_id="engineer")
    await _say(db, minutes_ago=170, subconversation_id="sub-1")
    wanted = [
        await _say(db, minutes_ago=100),
        await _say(db, minutes_ago=90, role="assistant"),
    ]

    rows = await select_review_rows(
        db,
        interface_type="web",
        conversation_id=CONVERSATION,
        watermark=reviewed,
        settings=SETTINGS,
        contributing_profiles=CONTRIBUTING,
    )

    assert [row["internal_id"] for row in rows] == wanted
