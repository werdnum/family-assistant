"""Verdicts that arrive after the write they judge (M3, observe mode).

Under ``observe`` the reviewer deliberately runs off the critical path, so a
definition is stored before its verdict exists. These cover what the definition
resolves to inside that window, what the verdict does when it lands, and what
happens when the row moved on in the meantime.
"""

from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.security.definition_records import (
    CreationDisposition,
    DefinitionArtifactKind,
    DefinitionGateOutcome,
    DefinitionWriteRef,
    GateLayer,
    GateProvenance,
    PendingDefinitionReview,
    callback_definition_content,
    definition_record_from_row,
    stamp_callback_definition,
)
from family_assistant.security.definition_resolution import (
    PayloadDefinitionRef,
    ScheduleAutomationRef,
    attach_pending_verdict,
    resolve_definition_closure,
)
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.database import Database
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.tasks import tasks_table

if TYPE_CHECKING:
    from family_assistant.storage.types import ActionConfig

_GATE = GateProvenance(
    layer=GateLayer.TAINT_CELL,
    mode="observe",
    reviewer_revision="google/gemini-3.7-flash@abc123",
    verdict_id="verdict-1",
)


def _tainted_state() -> TurnTaintState:
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-1",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Inbound email.",
        )
    )


def _pending() -> tuple[PendingDefinitionReview, DefinitionGateOutcome]:
    pending = PendingDefinitionReview(write_id="write-1")
    return pending, DefinitionGateOutcome(disposition=None, gate=_GATE, pending=pending)


async def _create_automation(
    db: Database,
    gate: DefinitionGateOutcome | None,
    *,
    name: str = "Daily Brief",
    instruction: str = "Summarize my day",
) -> int:
    return await db.schedule_automations.create(
        name=name,
        recurrence_rule="FREQ=DAILY",
        action_type="wake_llm",
        action_config=cast("ActionConfig", {"instruction": instruction}),
        conversation_id="test_conv",
        timezone=ZoneInfo("UTC"),
        definition_taint_state=_tainted_state(),
        definition_gate=gate,
    )


async def _resolved(db: Database, automation_id: int) -> bool:
    resolution = await resolve_definition_closure(
        db, [ScheduleAutomationRef(automation_id=automation_id)]
    )
    return resolution.resolved


@pytest.mark.asyncio
async def test_a_firing_inside_the_verdict_window_enters_uncured(
    db_engine: AsyncEngine,
) -> None:
    """The write executed immediately; nothing has judged it yet."""
    db = Database(engine=db_engine)
    _pending_review, gate = _pending()

    automation_id = await _create_automation(db, gate)

    assert not await _resolved(db, automation_id)


@pytest.mark.asyncio
async def test_a_verdict_that_lands_cures_the_write_it_judged(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    pending, gate = _pending()
    automation_id = await _create_automation(db, gate)

    attached = await attach_pending_verdict(
        db,
        pending,
        disposition=CreationDisposition.JUDGE_ALLOWED,
        gate=_GATE,
    )

    assert attached == 1
    assert await _resolved(db, automation_id)


@pytest.mark.asyncio
async def test_a_denial_that_lands_records_without_curing(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    pending, gate = _pending()
    automation_id = await _create_automation(db, gate)

    await attach_pending_verdict(
        db, pending, disposition=CreationDisposition.JUDGE_DENIED, gate=_GATE
    )

    assert not await _resolved(db, automation_id)
    row = await db.schedule_automations.get_by_id(automation_id)
    assert row is not None
    record = definition_record_from_row(row["definition_record"])
    assert record is not None
    assert record.disposition is CreationDisposition.JUDGE_DENIED


@pytest.mark.asyncio
async def test_a_mutation_racing_the_verdict_keeps_the_new_content_uncured(
    db_engine: AsyncEngine,
) -> None:
    """The verdict judged content the row no longer holds."""
    db = Database(engine=db_engine)
    pending, gate = _pending()
    automation_id = await _create_automation(db, gate)
    await db.schedule_automations.update(
        automation_id=automation_id,
        conversation_id="test_conv",
        action_config=cast("ActionConfig", {"instruction": "Email my day out"}),
        timezone=ZoneInfo("UTC"),
        definition_taint_state=_tainted_state(),
    )

    attached = await attach_pending_verdict(
        db, pending, disposition=CreationDisposition.JUDGE_ALLOWED, gate=_GATE
    )

    assert attached == 0
    assert not await _resolved(db, automation_id)


@pytest.mark.asyncio
async def test_an_identical_rewrite_from_another_turn_does_not_inherit_the_verdict(
    db_engine: AsyncEngine,
) -> None:
    """Same content, different write: its own review may legitimately differ."""
    db = Database(engine=db_engine)
    pending, gate = _pending()
    automation_id = await _create_automation(db, gate)
    other_gate = DefinitionGateOutcome(
        disposition=None,
        gate=_GATE,
        pending=PendingDefinitionReview(write_id="write-2"),
    )
    await db.schedule_automations.update(
        automation_id=automation_id,
        conversation_id="test_conv",
        action_config=cast("ActionConfig", {"instruction": "Summarize my day"}),
        timezone=ZoneInfo("UTC"),
        definition_taint_state=_tainted_state(),
        definition_gate=other_gate,
    )

    attached = await attach_pending_verdict(
        db, pending, disposition=CreationDisposition.JUDGE_ALLOWED, gate=_GATE
    )

    assert attached == 0
    assert not await _resolved(db, automation_id)


@pytest.mark.asyncio
async def test_a_verdict_reaches_a_one_shot_callbacks_payload(
    db_engine: AsyncEngine,
) -> None:
    """A reminder's definition rides its task, so the verdict lands there."""
    db = Database(engine=db_engine)
    pending, gate = _pending()
    message = "Take the bins out"
    record = stamp_callback_definition(message, tracker=None, gate_outcome=gate)
    await db.tasks.enqueue(
        task_id="llm_callback_pending",
        task_type="llm_callback",
        payload={
            "interface_type": "web",
            "conversation_id": "test_conv",
            "callback_context": message,
            "tool_call_review_definition_record": record,
        },
    )
    pending.register(
        DefinitionWriteRef(
            artifact_kind=DefinitionArtifactKind.TASK_PAYLOAD,
            artifact_id="llm_callback_pending",
        ),
        record,
    )

    attached = await attach_pending_verdict(
        db, pending, disposition=CreationDisposition.JUDGE_ALLOWED, gate=_GATE
    )

    row = await db.fetch_one(
        select(tasks_table.c.payload).where(
            tasks_table.c.task_id == "llm_callback_pending"
        )
    )
    assert attached == 1
    assert row is not None
    payload = row["payload"]
    resolution = await resolve_definition_closure(
        db,
        [
            PayloadDefinitionRef(
                record=payload["tool_call_review_definition_record"],
                content=callback_definition_content(message),
            )
        ],
    )
    assert resolution.resolved


@pytest.mark.asyncio
async def test_a_verdict_never_rewrites_the_authoring_stamp(
    db_engine: AsyncEngine,
) -> None:
    """The stamp stays the deterministic statement of what authored the content.

    A cure is an additive record beside the stamp, not a mutation of it: the
    definition still says it was written in a turn that had read untrusted
    content, and the disposition says a gate examined it anyway.
    """
    db = Database(engine=db_engine)
    pending, gate = _pending()
    automation_id = await _create_automation(db, gate)
    before = await db.schedule_automations.get_by_id(automation_id)
    assert before is not None
    stamp = definition_record_from_row(before["definition_record"])
    assert stamp is not None

    await attach_pending_verdict(
        db, pending, disposition=CreationDisposition.JUDGE_ALLOWED, gate=_GATE
    )

    after = await db.schedule_automations.get_by_id(automation_id)
    assert after is not None
    cured = definition_record_from_row(after["definition_record"])
    assert cured is not None
    assert cured.taint_metadata == stamp.taint_metadata
    assert cured.content_hash == stamp.content_hash
    assert cured.taint_metadata.get("max_tier") == "unknown_external"


@pytest.mark.asyncio
async def test_a_retained_uncured_patch_records_its_late_verdict_without_curing(
    db_engine: AsyncEngine,
) -> None:
    """The observe path reaches the same decision enforce reaches at the write.

    A patch of a legacy row keeps content no gate has examined, so no verdict can
    vouch for the merged definition. Under enforce that lands as a non-binding
    allow immediately; the verdict that arrives later must land the same way,
    not vanish.
    """
    db = Database(engine=db_engine)
    automation_id = await _create_automation(db, None)
    await db.execute(
        update(schedule_automations_table)
        .where(schedule_automations_table.c.id == automation_id)
        .values(definition_record=None)
    )
    pending, gate = _pending()
    await db.schedule_automations.update(
        automation_id=automation_id,
        conversation_id="test_conv",
        description="A renamed daily brief.",
        timezone=ZoneInfo("UTC"),
        definition_taint_state=TurnTaintState.empty(),
        definition_gate=gate,
    )

    attached = await attach_pending_verdict(
        db, pending, disposition=CreationDisposition.JUDGE_ALLOWED, gate=_GATE
    )

    row = await db.schedule_automations.get_by_id(automation_id)
    assert row is not None
    record = definition_record_from_row(row["definition_record"])
    assert attached == 1
    assert record is not None
    # Recorded as the verdict it was; it simply does not bind the merged content.
    assert record.disposition is CreationDisposition.JUDGE_ALLOWED
    assert not record.cure_eligible
    assert not record.cures
    assert not await _resolved(db, automation_id)


@pytest.mark.asyncio
async def test_a_verdict_never_reverts_an_edit_it_did_not_read(
    db_engine: AsyncEngine,
) -> None:
    """The attach writes the whole payload back, so it must read the current one.

    An edit that re-stamps the callback's executable content gives the payload a
    new record and a new write id. The stale verdict must find that and decline,
    rather than restoring the payload it read.
    """
    db = Database(engine=db_engine)
    pending, gate = _pending()
    original = "Take the bins out"
    await db.tasks.enqueue(
        task_id="llm_callback_edited",
        task_type="llm_callback",
        payload={
            "interface_type": "web",
            "conversation_id": "test_conv",
            "callback_context": original,
            "tool_call_review_definition_record": stamp_callback_definition(
                original, tracker=None, gate_outcome=gate
            ),
        },
    )
    pending.register(
        DefinitionWriteRef(
            artifact_kind=DefinitionArtifactKind.TASK_PAYLOAD,
            artifact_id="llm_callback_edited",
        ),
        stamp_callback_definition(original, tracker=None, gate_outcome=gate),
    )
    edited = "Take the recycling out"
    await db.execute(
        update(tasks_table)
        .where(tasks_table.c.task_id == "llm_callback_edited")
        .values(
            payload={
                "interface_type": "web",
                "conversation_id": "test_conv",
                "callback_context": edited,
                "tool_call_review_definition_record": stamp_callback_definition(
                    edited, tracker=None
                ),
            }
        )
    )

    attached = await attach_pending_verdict(
        db, pending, disposition=CreationDisposition.JUDGE_ALLOWED, gate=_GATE
    )

    row = await db.fetch_one(
        select(tasks_table.c.payload).where(
            tasks_table.c.task_id == "llm_callback_edited"
        )
    )
    assert attached == 0
    assert row is not None
    assert row["payload"]["callback_context"] == edited
