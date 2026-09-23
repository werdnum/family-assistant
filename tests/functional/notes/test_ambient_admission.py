"""One synchronous review decides whether a note enters every future prompt.

Milestone 4 of docs/design/ambient-note-admission-at-write-time.md.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, cast

import pytest

from family_assistant.config_models import ToolCallReviewConfig
from family_assistant.security.ambient_admission import AMBIENT_ADMISSION_EVENT_TYPE
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyMode,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.tool_call_review import (
    ToolCallReviewResult,
    ToolCallReviewStatus,
    ToolCallReviewVerdict,
)
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.notes import add_or_update_note_tool
from family_assistant.tools.types import ConfirmationOutcome
from family_assistant.tools.workspace_files import workspace_import_note_tool
from tests.functional.notes.ambient_helpers import (
    SKILL_BODY,
    notes_provider,
    state_at,
    stored_tier,
    tool_context,
    tracker_at,
    write_note,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing import ProcessingService
    from family_assistant.security.taint import InMemoryTurnTaintTracker
    from family_assistant.services.tool_call_review import (
        ToolCallReviewConstraints,
        ToolCallReviewer,
        ToolCallReviewInput,
    )
    from family_assistant.tools.types import ToolExecutionContext

ENFORCE = TaintPolicyMode.ENFORCE
OBSERVE = TaintPolicyMode.OBSERVE


class _Reviewer:
    """A reviewer returning scripted verdicts and recording what it saw."""

    def __init__(
        self,
        *verdicts: ToolCallReviewVerdict,
        fallback: bool = False,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._verdicts = list(verdicts)
        self._fallback = fallback
        self._gate = gate
        self.inputs: list[ToolCallReviewInput] = []
        self.entered = asyncio.Event()

    async def review_tool_call(
        self,
        review_input: ToolCallReviewInput,
        constraints: ToolCallReviewConstraints,
        *,
        budget_exhausted: bool = False,
    ) -> ToolCallReviewResult:
        del budget_exhausted
        self.inputs.append(review_input)
        self.entered.set()
        if self._gate is not None and len(self.inputs) == 1:
            await self._gate.wait()
        if self._fallback:
            return ToolCallReviewResult(
                verdict=constraints.fallback_verdict,
                reason="The reviewer timed out.",
                status=ToolCallReviewStatus.TIMEOUT_FALLBACK,
                latency_ms=0,
                used_fallback=True,
            )
        verdict = self._verdicts[min(len(self.inputs), len(self._verdicts)) - 1]
        return ToolCallReviewResult(
            verdict=verdict,
            reason=f"Scripted {verdict.value}.",
            status=ToolCallReviewStatus.MODEL_VERDICT,
            latency_ms=0,
            used_fallback=False,
        )

    @property
    def contents(self) -> list[object]:
        return [review.arguments.get("content") for review in self.inputs]


class _ConfirmationManager:
    def __init__(self, kind: Literal["approved", "rejected"] = "approved") -> None:
        self.kind: Literal["approved", "rejected"] = kind
        self.prompts: list[str] = []

    async def request_confirmation(self, **kwargs: object) -> ConfirmationOutcome:
        self.prompts.append(str(kwargs["prompt_text"]))
        return ConfirmationOutcome(kind=self.kind)


def _gate(
    mode: TaintPolicyMode,
    reviewer: _Reviewer | None,
    *,
    policy: dict[str, object] | None = None,
) -> TaintTrackingToolsProvider:
    return TaintTrackingToolsProvider(
        LocalToolsProvider(registrations=[]),
        taint_policy=TaintPolicyConfig.model_validate({
            "mode": mode.value,
            **(policy or {}),
        }),
        tool_call_reviewer=cast("ToolCallReviewer | None", reviewer),
        review_config=ToolCallReviewConfig(timeout_seconds=1),
    )


def _context(
    db: Database,
    tracker: InMemoryTurnTaintTracker,
    gate: TaintTrackingToolsProvider,
    *,
    confirmations: _ConfirmationManager | None = None,
    registry: AttachmentRegistry | None = None,
) -> ToolExecutionContext:
    context = tool_context(db, tracker, attachment_registry=registry)
    context.tools_provider = gate
    context.tool_call_review_messages = []
    if confirmations is not None:
        context.confirmation_ui_managers = {"web": confirmations}  # type: ignore[dict-item]
    return context


async def _save(
    context: ToolExecutionContext,
    title: str = "Packing procedure",
    content: str = "Roll clothes; pack shoes first.",
    *,
    append: bool = False,
    attachment_ids: list[str] | None = None,
) -> str:
    return await add_or_update_note_tool(
        context,
        title=title,
        content=content,
        include_in_prompt=True,
        append=append,
        attachment_ids=attachment_ids,
    )


async def _prompt(db: Database) -> str:
    return "\n".join(
        await notes_provider(db).get_context_fragments(acting_user_id=None)
    )


async def _admission_events(db: Database) -> list[dict[str, object]]:
    events = await db.taint_audit_events.list_since(
        datetime(2000, 1, 1, tzinfo=UTC), limit=50
    )
    return [
        cast("dict[str, object]", event)
        for event in events
        if event["event_type"] == AMBIENT_ADMISSION_EVENT_TYPE
    ]


EXTERNAL = SourceTrustTier.UNKNOWN_EXTERNAL


# --------------------------------------------------------------------------- #
# Admission
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [ENFORCE, OBSERVE])
async def test_an_admitting_verdict_stamps_machine_reviewed(
    db_engine: AsyncEngine, mode: TaintPolicyMode
) -> None:
    db = Database(db_engine)
    reviewer = _Reviewer(ToolCallReviewVerdict.ALLOW)

    result = await _save(_context(db, tracker_at(EXTERNAL), _gate(mode, reviewer)))

    assert "reviewed" in result
    assert await stored_tier(db, "Packing procedure") is (
        SourceTrustTier.MACHINE_REVIEWED
    )


@pytest.mark.asyncio
async def test_the_next_turn_merges_exactly_one_reviewed_source(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _save(
        _context(
            db,
            tracker_at(EXTERNAL),
            _gate(ENFORCE, _Reviewer(ToolCallReviewVerdict.ALLOW)),
        )
    )

    sources = await notes_provider(db).get_context_taint_sources()

    assert "Roll clothes" in await _prompt(db)
    assert [source.tier for source in sources] == [SourceTrustTier.MACHINE_REVIEWED]


@pytest.mark.asyncio
async def test_the_reviewer_sees_the_resolved_candidate_as_the_payload(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(
        db,
        "Packing procedure",
        "Roll clothes.",
        provenance=NoteProvenanceStamp.internal(),
    )
    reviewer = _Reviewer(ToolCallReviewVerdict.ALLOW)

    await _save(
        _context(db, tracker_at(EXTERNAL), _gate(ENFORCE, reviewer)),
        content="Pack shoes first.",
        append=True,
    )

    assert reviewer.inputs[0].sink_class is SinkClass.AMBIENT_PROMPT_WRITE
    assert reviewer.contents == ["Roll clothes.\nPack shoes first."]
    note = await db.notes.get_by_title(
        "Packing procedure",
        read_policy=_unrestricted(),
    )
    assert note is not None
    assert note.content == "Roll clothes.\nPack shoes first."


def _unrestricted() -> NoteReadPolicy:
    # ast-grep-ignore: no-unrestricted-note-read-policy - test inspection
    return NoteReadPolicy.UNRESTRICTED


# --------------------------------------------------------------------------- #
# Denial
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_enforce_denial_refuses_and_leaves_the_row_untouched(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(
        db, "Packing procedure", "Original.", provenance=NoteProvenanceStamp.internal()
    )

    result = await _save(
        _context(
            db,
            tracker_at(EXTERNAL),
            _gate(ENFORCE, _Reviewer(ToolCallReviewVerdict.DENY)),
        )
    )

    assert result.startswith("Error:")
    assert "Original." in await _prompt(db)
    assert await stored_tier(db, "Packing procedure") is (
        SourceTrustTier.TRUSTED_INTERNAL
    )


@pytest.mark.asyncio
async def test_an_observe_denial_persists_reference_material(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)

    result = await _save(
        _context(
            db,
            tracker_at(EXTERNAL),
            _gate(OBSERVE, _Reviewer(ToolCallReviewVerdict.DENY)),
        )
    )

    assert "reference material" in result
    assert "Roll clothes" not in await _prompt(db)
    assert await stored_tier(db, "Packing procedure") is EXTERNAL


@pytest.mark.asyncio
async def test_a_denied_replacement_of_a_reviewed_note_drops_it_from_the_prompt(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(
        db,
        "Packing procedure",
        "Reviewed original.",
        provenance=NoteProvenanceStamp.admitted(
            title="Packing procedure", decided_by="test"
        ),
    )

    await _save(
        _context(
            db,
            tracker_at(EXTERNAL),
            _gate(OBSERVE, _Reviewer(ToolCallReviewVerdict.DENY)),
        )
    )

    prompt = await _prompt(db)
    assert "Reviewed original." not in prompt
    assert "Roll clothes" not in prompt


@pytest.mark.asyncio
async def test_an_observe_denial_of_a_known_contact_candidate_is_floored(
    db_engine: AsyncEngine,
) -> None:
    """A non-admitted external candidate is never eligible, whatever its tier."""
    db = Database(db_engine)

    await _save(
        _context(
            db,
            tracker_at(SourceTrustTier.KNOWN_CONTACT),
            _gate(OBSERVE, _Reviewer(ToolCallReviewVerdict.DENY)),
        )
    )

    assert await stored_tier(db, "Packing procedure") is (SourceTrustTier.KNOWN_CONTACT)
    assert "Roll clothes" not in await _prompt(db)


# --------------------------------------------------------------------------- #
# Fallbacks and configuration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failed_reviewer_falls_back_to_confirming_the_resolved_candidate(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(
        db,
        "Packing procedure",
        "Roll clothes.",
        provenance=NoteProvenanceStamp.internal(),
    )
    confirmations = _ConfirmationManager("approved")

    await _save(
        _context(
            db,
            tracker_at(EXTERNAL),
            _gate(ENFORCE, _Reviewer(fallback=True)),
            confirmations=confirmations,
        ),
        content="Pack shoes first.",
        append=True,
    )

    assert len(confirmations.prompts) == 1
    assert "Roll clothes.\\nPack shoes first." in confirmations.prompts[0]
    assert "append" not in confirmations.prompts[0]
    assert await stored_tier(db, "Packing procedure") is (
        SourceTrustTier.MACHINE_REVIEWED
    )


@pytest.mark.asyncio
async def test_a_declined_confirmation_refuses(db_engine: AsyncEngine) -> None:
    db = Database(db_engine)

    result = await _save(
        _context(
            db,
            tracker_at(EXTERNAL),
            _gate(ENFORCE, _Reviewer(fallback=True)),
            confirmations=_ConfirmationManager("rejected"),
        )
    )

    assert result.startswith("Error:")
    assert (
        await db.notes.get_by_title("Packing procedure", read_policy=_unrestricted())
        is None
    )


@pytest.mark.asyncio
async def test_a_failed_reviewer_in_observe_mode_persists_without_prompting(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    confirmations = _ConfirmationManager("approved")

    await _save(
        _context(
            db,
            tracker_at(EXTERNAL),
            _gate(OBSERVE, _Reviewer(fallback=True)),
            confirmations=confirmations,
        )
    )

    assert confirmations.prompts == []
    assert await stored_tier(db, "Packing procedure") is EXTERNAL


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "saved"), [(ENFORCE, False), (OBSERVE, True)])
async def test_with_no_reviewer_nothing_is_admitted(
    db_engine: AsyncEngine, mode: TaintPolicyMode, saved: bool
) -> None:
    db = Database(db_engine)

    result = await _save(_context(db, tracker_at(EXTERNAL), _gate(mode, None)))

    assert "No tool-call reviewer is configured" in result
    note = await db.notes.get_by_title("Packing procedure", read_policy=_unrestricted())
    assert (note is not None) is saved
    assert "Roll clothes" not in await _prompt(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", ["allow", "audit"])
async def test_an_operator_override_of_an_external_cell_admits(
    db_engine: AsyncEngine, cell: str
) -> None:
    db = Database(db_engine)
    gate = _gate(
        ENFORCE,
        None,
        policy={
            "matrix_overrides": {"unknown_external": {"ambient_prompt_write": cell}}
        },
    )

    await _save(_context(db, tracker_at(EXTERNAL), gate))

    assert await stored_tier(db, "Packing procedure") is (
        SourceTrustTier.MACHINE_REVIEWED
    )
    events = await _admission_events(db)
    assert "operator_override" in str(events[-1]["reason"])


_STRENGTHENED_TRUSTED_CELL: dict[str, object] = {
    "matrix_overrides": {
        "trusted_user": {
            "ambient_prompt_write": {"outcome": "adjudicate", "fallback": "confirm"}
        }
    }
}


@pytest.mark.asyncio
async def test_a_strengthened_trusted_cell_reviews_but_keeps_the_trusted_stamp(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    reviewer = _Reviewer(ToolCallReviewVerdict.ALLOW)

    await _save(
        _context(
            db,
            tracker_at(None),
            _gate(ENFORCE, reviewer, policy=_STRENGTHENED_TRUSTED_CELL),
        )
    )

    assert len(reviewer.inputs) == 1
    assert await stored_tier(db, "Packing procedure") is (
        SourceTrustTier.TRUSTED_INTERNAL
    )


@pytest.mark.asyncio
async def test_an_observe_denial_of_a_trusted_candidate_keeps_its_stamp_and_logs(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)

    await _save(
        _context(
            db,
            tracker_at(None),
            _gate(
                OBSERVE,
                _Reviewer(ToolCallReviewVerdict.DENY),
                policy=_STRENGTHENED_TRUSTED_CELL,
            ),
        )
    )

    assert await stored_tier(db, "Packing procedure") is (
        SourceTrustTier.TRUSTED_INTERNAL
    )
    assert (await _admission_events(db))[-1]["effective_outcome"] == "not_admitted"


@pytest.mark.asyncio
async def test_reviewed_material_alone_writes_ambient_notes_without_review(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    reviewer = _Reviewer(ToolCallReviewVerdict.DENY)

    await _save(
        _context(
            db,
            tracker_at(SourceTrustTier.MACHINE_REVIEWED),
            _gate(ENFORCE, reviewer),
        )
    )

    assert reviewer.inputs == []
    assert await stored_tier(db, "Packing procedure") is (
        SourceTrustTier.MACHINE_REVIEWED
    )
    assert "Roll clothes" in await _prompt(db)


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "verdict", "confirm", "outcome"),
    [
        (ENFORCE, ToolCallReviewVerdict.ALLOW, False, "admitted"),
        (ENFORCE, None, True, "admitted"),
        (OBSERVE, ToolCallReviewVerdict.DENY, False, "not_admitted"),
    ],
    ids=["reviewer", "confirmation", "observe-denial"],
)
async def test_every_stamp_change_leaves_an_audit_event_keyed_to_the_note(
    db_engine: AsyncEngine,
    mode: TaintPolicyMode,
    verdict: ToolCallReviewVerdict | None,
    confirm: bool,
    outcome: str,
) -> None:
    db = Database(db_engine)
    reviewer = _Reviewer(verdict) if verdict is not None else _Reviewer(fallback=True)

    await _save(
        _context(
            db,
            tracker_at(EXTERNAL),
            _gate(mode, reviewer),
            confirmations=_ConfirmationManager() if confirm else None,
        )
    )

    events = await _admission_events(db)
    assert len(events) == 1
    event = events[0]
    assert event["artifact_id"] == "note:Packing procedure"
    assert event["effective_outcome"] == outcome
    assert event["max_tier"] == EXTERNAL.config_value
    assert event["sources_json"]
    review_context = cast("dict[str, object]", event["review_context_json"])
    assert "omitted_source_count" in review_context


# --------------------------------------------------------------------------- #
# Resolution and concurrency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_note_edited_during_its_review_is_re_resolved_and_re_reviewed(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(
        db,
        "Packing procedure",
        "Roll clothes.",
        provenance=NoteProvenanceStamp.internal(),
    )
    release = asyncio.Event()
    reviewer = _Reviewer(ToolCallReviewVerdict.ALLOW, gate=release)
    save = asyncio.create_task(
        _save(
            _context(db, tracker_at(EXTERNAL), _gate(ENFORCE, reviewer)),
            content="Pack shoes first.",
            append=True,
        )
    )
    await reviewer.entered.wait()
    await write_note(
        db,
        "Packing procedure",
        "Edited in the notes UI.",
        provenance=NoteProvenanceStamp.user_edit(),
    )
    release.set()

    await save

    assert reviewer.contents == [
        "Roll clothes.\nPack shoes first.",
        "Edited in the notes UI.\nPack shoes first.",
    ]
    note = await db.notes.get_by_title("Packing procedure", read_policy=_unrestricted())
    assert note is not None
    assert note.content == "Edited in the notes UI.\nPack shoes first."


def _registry(db_engine: AsyncEngine) -> AttachmentRegistry:
    return AttachmentRegistry(
        storage_path=tempfile.mkdtemp(), db_engine=db_engine, config=None
    )


@pytest.mark.asyncio
async def test_a_clean_turn_associating_an_unlabelled_attachment_is_reviewed(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    registry = _registry(db_engine)
    # A pre-chokepoint attachment: registered with no envelope at all.
    file_data = await registry._store_file_only(
        file_content=b"legacy",
        filename="legacy.txt",
        content_type="text/plain",
        media_limited=False,
    )
    await registry.register_attachment(
        db_context=db,
        attachment_id=file_data.attachment_id,
        source_type="tool",
        source_id="legacy",
        mime_type="text/plain",
        description="Sender-controlled description",
        size=6,
        content_url=file_data.content_url,
        storage_path=file_data.storage_path,
    )
    reviewer = _Reviewer(ToolCallReviewVerdict.ALLOW)
    tracker = tracker_at(None)

    await _save(
        _context(db, tracker, _gate(ENFORCE, reviewer), registry=registry),
        attachment_ids=[file_data.attachment_id],
    )

    assert len(reviewer.inputs) == 1
    assert reviewer.inputs[0].taint_state.max_tier is EXTERNAL
    # An explicit read of the same attachment still contributes nothing.
    assert tracker.snapshot().max_tier is SourceTrustTier.TRUSTED_USER


@pytest.mark.asyncio
async def test_a_clean_update_of_an_unreviewed_note_is_reviewed_at_its_stored_tier(
    db_engine: AsyncEngine,
) -> None:
    """The title is retained, so replacing everything else does not launder it."""
    db = Database(db_engine)
    await write_note(
        db,
        "Packing procedure",
        "Copied from a page.",
        include_in_prompt=False,
        provenance=NoteProvenanceStamp.machine(state_at(EXTERNAL)),
    )
    reviewer = _Reviewer(ToolCallReviewVerdict.ALLOW)

    await _save(
        _context(db, tracker_at(None), _gate(ENFORCE, reviewer)),
        content="Entirely new body.",
        attachment_ids=[],
    )

    assert len(reviewer.inputs) == 1
    assert reviewer.inputs[0].taint_state.max_tier is EXTERNAL


# --------------------------------------------------------------------------- #
# Imports
# --------------------------------------------------------------------------- #


def _workspace_with(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


async def _import(
    db: Database,
    tmp_path: Path,
    gate: TaintTrackingToolsProvider,
    name: str,
    tracker: InMemoryTurnTaintTracker,
) -> None:
    context = _context(db, tracker, gate)
    context.processing_service = cast(
        "ProcessingService",
        SimpleNamespace(
            app_config=SimpleNamespace(
                ai_worker_config=SimpleNamespace(workspace_mount_path=str(tmp_path))
            ),
            tools_provider=None,
        ),
    )
    await workspace_import_note_tool(context, path=name)


@pytest.mark.asyncio
async def test_an_import_reads_as_external_and_defaults_to_reference(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    db = Database(db_engine)
    _workspace_with(tmp_path, "notes.md", "Some research.")
    reviewer = _Reviewer(ToolCallReviewVerdict.DENY)
    tracker = tracker_at(None)

    await _import(db, tmp_path, _gate(ENFORCE, reviewer), "notes.md", tracker)

    assert reviewer.inputs == []
    assert tracker.snapshot().max_tier is EXTERNAL
    assert await stored_tier(db, "notes") is EXTERNAL
    assert "Some research." not in await _prompt(db)


@pytest.mark.asyncio
async def test_an_imported_skill_is_reviewed_and_unadmitted_stays_out_of_the_catalog(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    db = Database(db_engine)
    _workspace_with(tmp_path, "packing.md", SKILL_BODY)
    reviewer = _Reviewer(ToolCallReviewVerdict.DENY)

    await _import(
        db, tmp_path, _gate(OBSERVE, reviewer), "packing.md", tracker_at(None)
    )

    assert len(reviewer.inputs) == 1
    assert reviewer.inputs[0].arguments.get("imported_from_workspace_file") == (
        "packing.md"
    )
    assert "Pack for a trip" not in await _prompt(db)


@pytest.mark.asyncio
async def test_an_admitted_import_is_machine_reviewed(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    db = Database(db_engine)
    _workspace_with(
        tmp_path, "rules.md", "---\ninclude_in_prompt: true\n---\nHouse rules."
    )

    await _import(
        db,
        tmp_path,
        _gate(ENFORCE, _Reviewer(ToolCallReviewVerdict.ALLOW)),
        "rules.md",
        tracker_at(None),
    )

    assert await stored_tier(db, "rules") is SourceTrustTier.MACHINE_REVIEWED
    assert "House rules." in await _prompt(db)
