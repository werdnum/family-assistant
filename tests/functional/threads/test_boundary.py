"""The cross-conversation chokepoint and its three rules.

See docs/design/one-thread-chat-ux.md, "Moving content across the boundary".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, select

from family_assistant.config_models import ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.messages import (
    AssistantMessage,
    ProviderMetadataDict,
    UserMessage,
)
from family_assistant.memory.due import select_due_conversations
from family_assistant.memory.review_settings import MemoryReviewSettings
from family_assistant.processing.types import ProcessingServiceConfig
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    as_inherited,
    prompt_window_taint,
)
from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import (
    MESSAGE_HISTORY_SOURCE_TYPE,
    message_history_table,
)
from family_assistant.storage.vector import DocumentRecord
from family_assistant.threads.boundary import (
    AssistantRequest,
    BoundaryDestination,
    CopiedTurns,
    CrossBoundaryCopyRefusedError,
    MovedTurn,
    copy_across_boundary,
    move_turn_across_boundary,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.repositories.message_history import MovedTurnRows

# Anchored to the real clock: history windows are read against it, so a fixed
# date ages out of the window and the copied rows stop being visible.
NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
HOME = "web_conv_home"
THREAD = "web_conv_thread"
UNTRUSTED: TaintMetadata = cast(
    "TaintMetadata", {"max_tier": "unknown_external", "sources": []}
)


def _profile(profile_id: str, *, household: bool) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts={},
        timezone=ZoneInfo("UTC"),
        history_budget_chars=100_000,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id=profile_id,
        receives_household_context=household,
    )


def _destination(profile: ProcessingServiceConfig) -> BoundaryDestination:
    return BoundaryDestination(
        interface_type="web",
        conversation_id=THREAD,
        profile=profile,
        user_id="alice",
    )


async def _home_turn(db: Database, *, turn_id: str) -> None:
    """A Home turn whose reply carries untrusted taint, as tool output would."""
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="what is on the calendar?"),
        interface_type="web",
        conversation_id=HOME,
        timestamp=NOW - timedelta(minutes=2),
        turn_id=turn_id,
        processing_profile_id="default_assistant",
        user_id="alice",
    )
    await db.message_history.add_message(
        AssistantMessage(content="Swimming at 4:30.", taint_metadata=UNTRUSTED),
        interface_type="web",
        conversation_id=HOME,
        timestamp=NOW - timedelta(minutes=1),
        turn_id=turn_id,
        processing_profile_id="default_assistant",
    )


async def _thread_rows(db: Database) -> list[dict[str, object]]:
    rows = await db.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == THREAD)
        .order_by(message_history_table.c.internal_id)
    )
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_copied_turns_keep_taint_and_take_the_destination_profile(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=CopiedTurns(
            source_interface_type="web",
            source_conversation_id=HOME,
            turn_ids=("home-turn",),
        ),
        turn_id="unused",
        now=NOW,
    )

    home_rows = await db.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == HOME)
        .order_by(message_history_table.c.internal_id)
    )
    thread_rows = await _thread_rows(db)
    assert [
        (row["role"], row["processing_profile_id"], row["taint_metadata_json"])
        for row in thread_rows
    ] == [
        (row["role"], "complex_tasks", row["taint_metadata_json"]) for row in home_rows
    ]


@pytest.mark.asyncio
async def test_copied_turns_are_visible_to_the_destination_profile(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=CopiedTurns(
            source_interface_type="web",
            source_conversation_id=HOME,
            turn_ids=("home-turn",),
        ),
        turn_id="unused",
        now=NOW,
    )

    history = await db.message_history.get_recent(
        interface_type="web",
        conversation_id=THREAD,
        processing_profile_id="complex_tasks",
    )
    assert [message.content for message in history] == [
        "what is on the calendar?",
        "Swimming at 4:30.",
    ]


@pytest.mark.asyncio
async def test_turns_are_refused_by_a_profile_without_household_context(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    with pytest.raises(CrossBoundaryCopyRefusedError):
        await copy_across_boundary(
            db,
            destination=_destination(_profile("coder", household=False)),
            payload=CopiedTurns(
                source_interface_type="web",
                source_conversation_id=HOME,
                turn_ids=("home-turn",),
            ),
            turn_id="unused",
            now=NOW,
        )

    assert await _thread_rows(db) == []


@pytest.mark.asyncio
async def test_an_assistant_request_reaches_a_profile_without_household_context(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    await copy_across_boundary(
        db,
        destination=_destination(_profile("coder", household=False)),
        payload=AssistantRequest(text="Write a CSV parser.", taint=None),
        turn_id="seed-turn",
        now=NOW,
    )

    rows = await _thread_rows(db)
    assert [(row["content"], row["turn_id"]) for row in rows] == [
        ("Write a CSV parser.", "seed-turn")
    ]


@pytest.mark.asyncio
async def test_an_assistant_request_is_not_stored_as_the_users_words(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=AssistantRequest(text="Plan the Japan trip.", taint=UNTRUSTED),
        turn_id="seed-turn",
        now=NOW,
    )

    rows = await _thread_rows(db)
    assert [(row["role"], row["is_internal"], row["user_id"]) for row in rows] == [
        ("user", True, "alice")
    ]
    assert (
        TurnTaintState.from_metadata(rows[0]["taint_metadata_json"]).max_tier
        is SourceTrustTier.UNKNOWN_EXTERNAL
    )


@pytest.mark.asyncio
async def test_an_assistant_request_introduces_what_its_writer_inherited(
    db_engine: AsyncEngine,
) -> None:
    """The writer's own window does not come along, so its carry-in must."""
    db = Database(engine=db_engine)
    inherited = (
        TurnTaintState
        .empty()
        .add_source(
            as_inherited(
                TaintSource(
                    source_type=TaintSourceType.BROWSER_SNAPSHOT,
                    source_id="plumber-site",
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason="A plumber's website.",
                )
            )
        )
        .to_metadata()
    )

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=AssistantRequest(text="Plan the Japan trip.", taint=inherited),
        turn_id="seed-turn",
        now=NOW,
    )

    window = await db.message_history.get_recent(
        interface_type="web",
        conversation_id=THREAD,
        processing_profile_id="complex_tasks",
    )
    assert prompt_window_taint(window).max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_a_moved_turn_leaves_a_marker_and_takes_its_attachments(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")
    [user_row] = [
        row
        for row in await db.message_history.get_rows_by_turn_id("home-turn")
        if row["role"] == "user"
    ]
    await db.execute(
        attachment_metadata_table.insert().values(
            attachment_id="11111111-1111-1111-1111-111111111111",
            source_type="user",
            source_id="alice",
            mime_type="image/png",
            size=1,
            conversation_id=HOME,
            message_id=user_row["internal_id"],
            created_at=NOW,
        )
    )
    await db.execute(
        insert(DocumentRecord).values(
            source_type=MESSAGE_HISTORY_SOURCE_TYPE,
            source_id="message_turn:home-turn",
            added_at=NOW,
        )
    )

    moved = await move_turn_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        turn=MovedTurn(
            source_interface_type="web",
            source_conversation_id=HOME,
            turn_id="home-turn",
        ),
        now=NOW,
    )

    home_rows = await db.fetch_all(
        select(message_history_table).where(
            message_history_table.c.conversation_id == HOME
        )
    )
    assert [
        (row["internal_id"], row["role"], row["moved_to_conversation_id"])
        for row in home_rows
    ] == [(moved.marker_id, "assistant", THREAD)]
    thread_rows = await _thread_rows(db)
    assert [row["internal_id"] for row in thread_rows] == list(moved.copied_ids)
    assert [row["taint_metadata_json"] for row in thread_rows][-1] == UNTRUSTED
    attachment = await db.fetch_one(select(attachment_metadata_table))
    assert attachment is not None
    assert (attachment["message_id"], attachment["conversation_id"]) == (
        moved.copied_ids[0],
        THREAD,
    )
    # Home's index record goes; the copy, under a turn of its own, is indexed anew.
    assert await db.fetch_all(select(DocumentRecord.id)) == []
    assert {row["turn_id"] for row in thread_rows} != {"home-turn"}


@pytest.mark.asyncio
async def test_two_moves_of_one_turn_copy_it_once(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    async def move() -> MovedTurnRows:
        return await move_turn_across_boundary(
            db,
            destination=_destination(_profile("complex_tasks", household=True)),
            turn=MovedTurn(
                source_interface_type="web",
                source_conversation_id=HOME,
                turn_id="home-turn",
            ),
            now=NOW,
        )

    outcomes = await asyncio.gather(move(), move(), return_exceptions=True)

    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        "LookupError",
        "MovedTurnRows",
    ]
    assert len(await _thread_rows(db)) == 2


@pytest.mark.asyncio
async def test_copies_drop_thinking_bound_to_their_old_history(
    db_engine: AsyncEngine,
) -> None:
    """Anthropic thinking binds to the prefix; other providers' metadata does not."""
    db = Database(engine=db_engine)
    for provider in ("anthropic", "openai"):
        await db.message_history.add_message(
            AssistantMessage(
                content=f"thought with {provider}",
                taint_metadata=UNTRUSTED,
                provider_metadata=ProviderMetadataDict(provider=provider),
            ),
            interface_type="web",
            conversation_id=HOME,
            timestamp=NOW,
            turn_id=f"turn-{provider}",
            processing_profile_id="default_assistant",
        )

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=CopiedTurns(
            source_interface_type="web",
            source_conversation_id=HOME,
            turn_ids=("turn-anthropic", "turn-openai"),
        ),
        turn_id="unused",
        now=NOW,
    )

    assert {
        row["content"]: row["provider_metadata"] for row in await _thread_rows(db)
    } == {
        "thought with anthropic": None,
        "thought with openai": {"provider": "openai"},
    }


@pytest.mark.asyncio
async def test_copied_rows_are_stamped_in_order_and_apart(
    db_engine: AsyncEngine,
) -> None:
    """The messages endpoint pages by timestamp, so equal stamps would be skipped."""
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=CopiedTurns(
            source_interface_type="web",
            source_conversation_id=HOME,
            turn_ids=("home-turn",),
        ),
        turn_id="unused",
        now=NOW,
    )

    stamps = [cast("datetime", row["timestamp"]) for row in await _thread_rows(db)]
    assert stamps == sorted(set(stamps))
    assert len(stamps) == 2


@pytest.mark.asyncio
async def test_a_turn_is_not_moved_into_a_profile_without_household_context(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _home_turn(db, turn_id="home-turn")

    with pytest.raises(CrossBoundaryCopyRefusedError):
        await move_turn_across_boundary(
            db,
            destination=_destination(_profile("coder", household=False)),
            turn=MovedTurn(
                source_interface_type="web",
                source_conversation_id=HOME,
                turn_id="home-turn",
            ),
            now=NOW,
        )

    assert await _thread_rows(db) == []
    assert len(await db.message_history.get_rows_by_turn_id("home-turn")) == 2


@pytest.mark.asyncio
async def test_an_assistant_request_does_not_make_memory_review_due(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await copy_across_boundary(
        db,
        destination=_destination(_profile("complex_tasks", household=True)),
        payload=AssistantRequest(text="Plan the Japan trip.", taint=None),
        turn_id="seed-turn",
        now=NOW - timedelta(hours=2),
    )

    due = await select_due_conversations(
        db,
        now=NOW,
        settings=MemoryReviewSettings(
            idle_window_minutes={"web": 30},
            max_deferral_hours=24,
            contributing_interfaces=frozenset({"web"}),
        ),
        contributing_profiles={"complex_tasks": NOW - timedelta(days=1)},
    )

    assert due == []
