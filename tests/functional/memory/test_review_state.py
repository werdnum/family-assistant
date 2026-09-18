"""The watermark and the enablement boundary, as stored state.

Slice 4 of docs/design/conversation-memory.md. Both are what the review sweep
is scheduled from, so both have to survive a restart and neither may move
backwards on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
INTERFACE = "web"
CONVERSATION = "conv-1"


@pytest.mark.asyncio
async def test_an_unreviewed_conversation_has_no_watermark(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    assert (
        await db.memory_review.get_watermark(
            interface_type=INTERFACE, conversation_id=CONVERSATION
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_watermark_round_trips(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)

    await db.memory_review.advance_watermark(
        interface_type=INTERFACE,
        conversation_id=CONVERSATION,
        last_reviewed_internal_id=42,
        now=NOW,
    )

    watermark = await db.memory_review.get_watermark(
        interface_type=INTERFACE, conversation_id=CONVERSATION
    )
    assert watermark is not None
    assert watermark.last_reviewed_internal_id == 42
    assert watermark.interface_type == INTERFACE
    assert watermark.conversation_id == CONVERSATION


@pytest.mark.asyncio
async def test_a_watermark_moves_forward(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    await db.memory_review.advance_watermark(
        interface_type=INTERFACE,
        conversation_id=CONVERSATION,
        last_reviewed_internal_id=42,
        now=NOW,
    )

    await db.memory_review.advance_watermark(
        interface_type=INTERFACE,
        conversation_id=CONVERSATION,
        last_reviewed_internal_id=99,
        now=NOW + timedelta(minutes=1),
    )

    watermark = await db.memory_review.get_watermark(
        interface_type=INTERFACE, conversation_id=CONVERSATION
    )
    assert watermark is not None
    assert watermark.last_reviewed_internal_id == 99


@pytest.mark.asyncio
async def test_a_watermark_never_moves_backwards(db_engine: AsyncEngine) -> None:
    """A retried or raced review must not re-expose a curated stretch."""
    db = Database(engine=db_engine)
    await db.memory_review.advance_watermark(
        interface_type=INTERFACE,
        conversation_id=CONVERSATION,
        last_reviewed_internal_id=99,
        now=NOW,
    )

    await db.memory_review.advance_watermark(
        interface_type=INTERFACE,
        conversation_id=CONVERSATION,
        last_reviewed_internal_id=42,
        now=NOW + timedelta(minutes=1),
    )

    watermark = await db.memory_review.get_watermark(
        interface_type=INTERFACE, conversation_id=CONVERSATION
    )
    assert watermark is not None
    assert watermark.last_reviewed_internal_id == 99


@pytest.mark.asyncio
async def test_watermarks_are_per_conversation(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    await db.memory_review.advance_watermark(
        interface_type=INTERFACE,
        conversation_id=CONVERSATION,
        last_reviewed_internal_id=42,
        now=NOW,
    )

    other = await db.memory_review.get_watermark(
        interface_type="telegram", conversation_id=CONVERSATION
    )

    assert other is None


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turning_contribution_on_records_the_moment(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    await db.memory_review.record_enablement(
        profile_ids_contributing={"default_assistant"}, now=NOW
    )

    assert await db.memory_review.get_enablement() == {"default_assistant": NOW}


@pytest.mark.asyncio
async def test_a_restart_does_not_re_stamp_the_boundary(
    db_engine: AsyncEngine,
) -> None:
    """Re-stamping would discard everything said since the feature went on."""
    db = Database(engine=db_engine)
    await db.memory_review.record_enablement(
        profile_ids_contributing={"default_assistant"}, now=NOW
    )

    await db.memory_review.record_enablement(
        profile_ids_contributing={"default_assistant"}, now=NOW + timedelta(days=2)
    )

    assert await db.memory_review.get_enablement() == {"default_assistant": NOW}


@pytest.mark.asyncio
async def test_turning_contribution_off_disables_the_profile(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await db.memory_review.record_enablement(
        profile_ids_contributing={"default_assistant"}, now=NOW
    )

    await db.memory_review.record_enablement(
        profile_ids_contributing=set(), now=NOW + timedelta(days=1)
    )

    assert await db.memory_review.get_enablement() == {}


@pytest.mark.asyncio
async def test_turning_contribution_on_again_records_a_new_moment(
    db_engine: AsyncEngine,
) -> None:
    """One boundary per enablement: what was said while off is never curated."""
    db = Database(engine=db_engine)
    later = NOW + timedelta(days=3)
    await db.memory_review.record_enablement(
        profile_ids_contributing={"default_assistant"}, now=NOW
    )
    await db.memory_review.record_enablement(
        profile_ids_contributing=set(), now=NOW + timedelta(days=1)
    )

    await db.memory_review.record_enablement(
        profile_ids_contributing={"default_assistant"}, now=later
    )

    assert await db.memory_review.get_enablement() == {"default_assistant": later}


@pytest.mark.asyncio
async def test_profiles_are_enabled_and_disabled_independently(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await db.memory_review.record_enablement(
        profile_ids_contributing={"default_assistant", "complex_tasks"}, now=NOW
    )

    await db.memory_review.record_enablement(
        profile_ids_contributing={"complex_tasks"}, now=NOW + timedelta(days=1)
    )

    assert await db.memory_review.get_enablement() == {"complex_tasks": NOW}
