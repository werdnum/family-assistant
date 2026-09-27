"""A stretch with no admissible household message is skipped and measured.

External source rows are excluded from the curator transcript. When no person's
message survives that filter, no model call happens and the lost stretch is
recorded rather than silently discarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from prometheus_client import REGISTRY

from family_assistant.memory.review import (
    MEMORY_CURATOR_PROFILE_ID,
    TAINT_SKIP_REASON,
    MemoryReviewResult,
    run_memory_review,
)
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock
from tests.functional.memory.curator_harness import (
    CONTRIBUTOR,
    CONVERSATION,
    NOW,
    SETTINGS,
    WEB,
    CuratorScript,
    curator_llm,
    curator_service,
    enable_contribution,
    memory_db,
    review_limits,
    seed_turn,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing import ProcessingService
    from family_assistant.storage.database import Database
    from tests.mocks.mock_llm import RuleBasedMockLLMClient

SAID = "for family hotels we need a separate sleeping area for the children"


def _unknown_external() -> TurnTaintState:
    """What an assistant row carries after reading the open web."""
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="https://hotels.example.test/listing",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Fetched a page the household does not control.",
        )
    )


def _context(db: Database, service: ProcessingService) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="internal",
        conversation_id="memory-review",
        user_name="system",
        turn_id=None,
        db_context=db,
        processing_service=service,
        clock=MockClock(NOW),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=None,
        timezone=service.service_config.timezone,
        credential_resolvers=None,
        api_backend=None,
    )


async def _skipped_review(
    db_engine: AsyncEngine,
) -> tuple[Database, MemoryReviewResult, list[int], RuleBasedMockLLMClient]:
    """Seed one researched conversation and review it."""
    limits = review_limits()
    db = memory_db(db_engine, limits)
    llm = curator_llm(CuratorScript())
    service = curator_service(db_engine, llm)

    await enable_contribution(db)
    ids = await seed_turn(
        db,
        turn_id="turn-1",
        said=SAID,
        replied="Here are three that fit.",
        user_taint=_unknown_external().to_metadata(),
        assistant_taint=_unknown_external().to_metadata(),
    )
    result = await run_memory_review(
        _context(db, service),
        interface_type=WEB,
        conversation_id=CONVERSATION,
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
        limits=limits,
    )
    return db, result, ids, llm


def _sample(name: str) -> float:
    """One skip counter's current value, treating "never observed" as zero.

    Read as a delta around the act rather than as an absolute: the registry is
    process-global and other tests in the same worker use the same labels.
    """
    return (
        REGISTRY.get_sample_value(f"{name}_total", {"reason": TAINT_SKIP_REASON}) or 0.0
    )


@pytest.mark.asyncio
async def test_a_tainted_stretch_reaches_no_model(db_engine: AsyncEngine) -> None:
    _db, result, _ids, llm = await _skipped_review(db_engine)

    assert result is MemoryReviewResult.SKIPPED
    assert llm.get_calls() == [], (
        "the stretch must be excluded before anything is sent to a model"
    )


@pytest.mark.asyncio
async def test_a_tainted_stretch_leaves_an_audit_record(
    db_engine: AsyncEngine,
) -> None:
    db, _result, _ids, _llm = await _skipped_review(db_engine)

    events = await db.taint_audit_events.list_since(NOW.replace(year=2000), limit=50)
    skips = [
        event for event in events if event["event_type"] == "memory_review_skipped"
    ]
    assert len(skips) == 1
    assert skips[0]["max_tier"] == SourceTrustTier.UNKNOWN_EXTERNAL.config_value
    assert skips[0]["processing_profile_id"] == MEMORY_CURATOR_PROFILE_ID
    assert skips[0]["effective_outcome"] == "skipped"


@pytest.mark.asyncio
async def test_a_tainted_stretch_leaves_a_skipped_change_log_row(
    db_engine: AsyncEngine,
) -> None:
    db, _result, _ids, _llm = await _skipped_review(db_engine)

    rows = await db.memory_change_log.get_recent(10)
    assert [row.outcome for row in rows] == ["skipped"]
    assert rows[0].reason is not None
    assert "outside the household" in rows[0].reason


@pytest.mark.asyncio
async def test_a_tainted_stretch_advances_the_watermark(
    db_engine: AsyncEngine,
) -> None:
    """Otherwise the same stretch is re-skipped for ever, and nothing after it
    is ever reviewed."""
    db, _result, ids, _llm = await _skipped_review(db_engine)

    watermark = await db.memory_review.get_watermark(
        interface_type=WEB, conversation_id=CONVERSATION
    )
    assert watermark is not None
    assert watermark.last_reviewed_internal_id == ids[-1]


@pytest.mark.asyncio
async def test_the_volume_the_skip_costs_is_counted(db_engine: AsyncEngine) -> None:
    """The measurement the taint refinement is gated on.

    Rows and user characters are counted, not just skips: the design's question
    is how much user text is being lost, and one skip of a long conversation is
    not the same loss as one skip of a greeting.
    """
    names = (
        "family_assistant_memory_review_skips",
        "family_assistant_memory_skipped_rows",
        "family_assistant_memory_skipped_user_chars",
    )
    before = tuple(_sample(name) for name in names)

    await _skipped_review(db_engine)

    after = tuple(_sample(name) for name in names)
    assert after[0] - before[0] == 1
    assert after[1] - before[1] == 2
    assert after[2] - before[2] == len(SAID)


def _machine_reviewed() -> TurnTaintState:
    """What an assistant row carries when its prompt held a reviewed note."""
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id="Packing procedure",
            tier=SourceTrustTier.MACHINE_REVIEWED,
            labels=frozenset(),
            reason="Prompt-included note was admitted by review.",
        )
    )


@pytest.mark.asyncio
async def test_external_assistant_rows_are_counted_as_excluded(
    db_engine: AsyncEngine,
) -> None:
    limits = review_limits()
    db = memory_db(db_engine, limits)
    await enable_contribution(db)
    await seed_turn(
        db,
        turn_id="turn-1",
        said=SAID,
        replied="Outside hotel details.",
        assistant_taint=_unknown_external().to_metadata(),
    )
    counter = "family_assistant_memory_review_excluded_rows_total"
    before = REGISTRY.get_sample_value(counter) or 0.0
    service = curator_service(db_engine, curator_llm(CuratorScript()))

    result = await run_memory_review(
        _context(db, service),
        interface_type=WEB,
        conversation_id=CONVERSATION,
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
        limits=limits,
    )

    assert result is MemoryReviewResult.APPLIED
    assert (REGISTRY.get_sample_value(counter) or 0.0) - before == 1


@pytest.mark.asyncio
async def test_a_stretch_carrying_only_reviewed_material_is_curated(
    db_engine: AsyncEngine,
) -> None:
    """Memory follows the reuse predicate, not authorship.

    A reviewed ambient note raises every turn it is included in to
    ``machine_reviewed``; skipping those would skip nearly every conversation.
    """
    limits = review_limits()
    db = memory_db(db_engine, limits)
    llm = curator_llm(CuratorScript())
    service = curator_service(db_engine, llm)
    await enable_contribution(db)
    await seed_turn(
        db,
        turn_id="turn-1",
        said=SAID,
        replied="Noted.",
        assistant_taint=_machine_reviewed().to_metadata(),
    )

    result = await run_memory_review(
        _context(db, service),
        interface_type=WEB,
        conversation_id=CONVERSATION,
        settings=SETTINGS,
        configured_contributors={CONTRIBUTOR},
        limits=limits,
    )

    assert result is MemoryReviewResult.APPLIED
    assert llm.get_calls(), "a reviewed stretch must reach the curator"
