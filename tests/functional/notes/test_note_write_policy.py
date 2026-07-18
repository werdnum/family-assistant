"""Tests for repository-level note write confinement (NoteWritePolicy).

Phase 1 of docs/design/profile-confined-note-writes-and-automation-approvals.md:
visibility confinement is enforced in the repository so every write path is
covered, not just the add_or_update_note tool.
"""

from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.notes import (
    NotesRepository,
    NoteWritePolicy,
    NoteWritePolicyError,
)
from family_assistant.tools.confirmation import (
    render_add_or_update_note_confirmation,
)
from family_assistant.tools.notes import add_or_update_note_tool
from family_assistant.tools.types import ToolExecutionContext


async def cleanup_notes(engine: AsyncEngine) -> None:
    async with DatabaseContext(engine=engine) as db:
        await db.execute_with_retry(delete(notes_table))


def _confined_policy() -> NoteWritePolicy:
    return NoteWritePolicy(
        visibility_grants={"ops_diagnostics"},
        default_labels=["ops_diagnostics"],
        required_labels=["ops_diagnostics"],
        allowed_labels=["ops_diagnostics"],
    )


def _make_tool_context(
    db_context: DatabaseContext,
    *,
    visibility_grants: set[str] | None = None,
    default_labels: list[str] | None = None,
    required_labels: list[str] | None = None,
    allowed_labels: list[str] | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test",
        user_name="tester",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=visibility_grants,
        default_note_visibility_labels=default_labels,
        required_note_visibility_labels=required_labels,
        allowed_note_visibility_labels=allowed_labels,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


# ---------------------------------------------------------------------------
# resolve_labels (pure value-object logic)
# ---------------------------------------------------------------------------


def test_resolve_labels_required_added_when_omitted() -> None:
    policy = _confined_policy()
    assert policy.resolve_labels(
        is_new_note=True, requested_labels=None, existing_labels=[]
    ) == ["ops_diagnostics"]


def test_resolve_labels_required_added_when_empty_list_requested() -> None:
    policy = _confined_policy()
    # Explicit [] cannot escape the required floor.
    assert policy.resolve_labels(
        is_new_note=True, requested_labels=[], existing_labels=[]
    ) == ["ops_diagnostics"]


def test_resolve_labels_ceiling_rejects_unexpected_label() -> None:
    policy = _confined_policy()
    with pytest.raises(NoteWritePolicyError):
        policy.resolve_labels(
            is_new_note=True, requested_labels=["family"], existing_labels=[]
        )


def test_resolve_labels_unconstrained_preserves_empty() -> None:
    # UNCONSTRAINED reproduces pre-confinement behavior: [] stays [].
    assert (
        NoteWritePolicy.UNCONSTRAINED.resolve_labels(
            is_new_note=True, requested_labels=[], existing_labels=[]
        )
        == []
    )


def test_resolve_labels_preserves_existing_on_update_when_omitted() -> None:
    policy = _confined_policy()
    assert policy.resolve_labels(
        is_new_note=False,
        requested_labels=None,
        existing_labels=["ops_diagnostics"],
    ) == ["ops_diagnostics"]


def test_resolve_labels_refuses_update_of_unrestricted_note() -> None:
    # A confined writer may not relabel an existing unrestricted note into its
    # quarantine space on a title collision (required floor missing).
    policy = _confined_policy()
    with pytest.raises(NoteWritePolicyError):
        policy.resolve_labels(
            is_new_note=False, requested_labels=None, existing_labels=[]
        )


def test_resolve_labels_refuses_update_of_note_over_ceiling() -> None:
    # An existing note carrying labels beyond the ceiling is off-limits even if
    # the floor label is present.
    policy = _confined_policy()
    with pytest.raises(NoteWritePolicyError):
        policy.resolve_labels(
            is_new_note=False,
            requested_labels=None,
            existing_labels=["ops_diagnostics", "family"],
        )


# ---------------------------------------------------------------------------
# Repository enforcement (covers bypass paths that skip the tool layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_applies_required_labels(db_engine: AsyncEngine) -> None:
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Diag Report",
            content="findings",
            write_policy=_confined_policy(),
        )
        note = await db.notes.get_by_title("Diag Report", visibility_grants=None)
        assert note is not None
        assert note.visibility_labels == ["ops_diagnostics"]


@pytest.mark.asyncio
async def test_repository_required_labels_win_over_empty_request(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Diag Report",
            content="findings",
            visibility_labels=[],
            write_policy=_confined_policy(),
        )
        note = await db.notes.get_by_title("Diag Report", visibility_grants=None)
        assert note is not None
        assert note.visibility_labels == ["ops_diagnostics"]


@pytest.mark.asyncio
async def test_repository_ceiling_rejects_write(db_engine: AsyncEngine) -> None:
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        with pytest.raises(NoteWritePolicyError):
            await db.notes.add_or_update(
                title="Diag Report",
                content="findings",
                visibility_labels=["family"],
                write_policy=_confined_policy(),
            )
        # Nothing persisted.
        assert (
            await db.notes.get_by_title("Diag Report", visibility_grants=None) is None
        )


@pytest.mark.asyncio
async def test_repository_see_before_overwrite_enforced(
    db_engine: AsyncEngine,
) -> None:
    """A confined caller cannot overwrite a note it cannot see, even via the repo."""
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Family Note",
            content="private family content",
            visibility_labels=["family"],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
        )
    async with DatabaseContext(engine=db_engine) as db:
        with pytest.raises(NoteWritePolicyError):
            await db.notes.add_or_update(
                title="Family Note",
                content="overwritten by ops",
                write_policy=_confined_policy(),
            )
    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Family Note", visibility_grants=None)
        assert note is not None
        assert note.content == "private family content"


@pytest.mark.asyncio
async def test_repository_refuses_relabeling_unrestricted_note(
    db_engine: AsyncEngine,
) -> None:
    """A confined writer colliding with an unrestricted note must not hijack it.

    The unrestricted note passes see-before-overwrite (empty labels are visible
    to everyone), but appending the required label would silently move the user's
    note into the quarantine space, so the write is refused instead.
    """
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Shopping List",
            content="user content",
            visibility_labels=[],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
        )
    async with DatabaseContext(engine=db_engine) as db:
        with pytest.raises(NoteWritePolicyError):
            await db.notes.add_or_update(
                title="Shopping List",
                content="ops findings",
                write_policy=_confined_policy(),
            )
    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Shopping List", visibility_grants=None)
        assert note is not None
        assert note.content == "user content"
        assert note.visibility_labels == []


@pytest.mark.asyncio
async def test_repository_rename_applies_policy(db_engine: AsyncEngine) -> None:
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Old Title",
            content="content",
            visibility_labels=["ops_diagnostics"],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
        )
    async with DatabaseContext(engine=db_engine) as db:
        with pytest.raises(NoteWritePolicyError):
            await db.notes.rename_and_update(
                "Old Title",
                "New Title",
                "content",
                True,  # noqa: FBT003 - positional include_in_prompt matches signature
                visibility_labels=["family"],
                write_policy=_confined_policy(),
            )


# ---------------------------------------------------------------------------
# Tool layer end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_confines_new_note(db_engine: AsyncEngine) -> None:
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(
            db,
            visibility_grants={"ops_diagnostics"},
            default_labels=["ops_diagnostics"],
            required_labels=["ops_diagnostics"],
            allowed_labels=["ops_diagnostics"],
        )
        result = await add_or_update_note_tool(
            exec_context=ctx,
            title="Auto Diag",
            content="body",
            visibility_labels=[],
        )
        assert "successfully" in result.lower()
    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Auto Diag", visibility_grants=None)
        assert note is not None
        assert note.visibility_labels == ["ops_diagnostics"]


@pytest.mark.asyncio
async def test_tool_ceiling_violation_returns_error(db_engine: AsyncEngine) -> None:
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(
            db,
            visibility_grants={"ops_diagnostics"},
            required_labels=["ops_diagnostics"],
            allowed_labels=["ops_diagnostics"],
        )
        result = await add_or_update_note_tool(
            exec_context=ctx,
            title="Bad Labels",
            content="body",
            visibility_labels=["family"],
        )
        assert result.lower().startswith("error")
    async with DatabaseContext(engine=db_engine) as db:
        assert await db.notes.get_by_title("Bad Labels", visibility_grants=None) is None


def test_context_note_write_policy_reflects_fields() -> None:
    """The exec-context helper carries the profile's confinement fields.

    This is the single derivation used by every context-construction site, so
    the threading is exercised through it.
    """
    ctx = _make_tool_context(
        db_context=None,  # type: ignore[arg-type]  # helper only reads label fields
        visibility_grants={"ops_diagnostics"},
        default_labels=["ops_diagnostics"],
        required_labels=["ops_diagnostics"],
        allowed_labels=["ops_diagnostics"],
    )
    policy = ctx.note_write_policy()
    assert policy.visibility_grants == {"ops_diagnostics"}
    assert policy.default_labels == ["ops_diagnostics"]
    assert policy.required_labels == ["ops_diagnostics"]
    assert policy.allowed_labels == ["ops_diagnostics"]


@pytest.mark.asyncio
async def test_confirmation_prompt_reports_hidden_note_rejection(
    db_engine: AsyncEngine,
) -> None:
    """The confirmation renderer mirrors see-before-overwrite: a confirm-gated
    write targeting a note the profile cannot see shows the rejection up front
    instead of effective labels the write will never reach."""
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Family Note",
            content="private",
            visibility_labels=["family"],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
        )
    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(
            db,
            visibility_grants={"ops_diagnostics"},
            default_labels=["ops_diagnostics"],
            required_labels=["ops_diagnostics"],
            allowed_labels=["ops_diagnostics"],
        )
        prompt = await render_add_or_update_note_confirmation(
            {"title": "Family Note", "content": "overwrite attempt"}, ctx
        )
        assert "REJECTED by profile policy" in prompt
        assert "insufficient visibility permissions" in prompt


@pytest.mark.asyncio
async def test_policy_enforced_atomically_on_title_race(db_engine: AsyncEngine) -> None:
    """A same-title note created after the preflight cannot be overwritten.

    Simulates the race deterministically: the note exists in the database, but
    the preflight read is patched to see nothing (as if another transaction
    inserted it between the preflight and the upsert). The policy is re-asserted
    as the conflict-update WHERE, so the write must be refused, not applied.
    """
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Raced Note",
            content="hidden family content",
            visibility_labels=["family"],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
        )
    with (
        mock.patch.object(
            NotesRepository, "get_by_title", new=mock.AsyncMock(return_value=None)
        ),
        pytest.raises(NoteWritePolicyError, match="concurrently"),
    ):
        async with DatabaseContext(engine=db_engine) as db:
            await db.notes.add_or_update(
                title="Raced Note",
                content="ops findings",
                write_policy=_confined_policy(),
            )
    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Raced Note", visibility_grants=None)
        assert note is not None
        assert note.content == "hidden family content"
        assert note.visibility_labels == ["family"]


@pytest.mark.asyncio
async def test_atomic_policy_allows_racing_in_confinement_note(
    db_engine: AsyncEngine,
) -> None:
    """The conflict-update WHERE permits overwriting a raced note that is
    already inside the writer's confinement."""
    await cleanup_notes(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Raced Ops Note",
            content="previous findings",
            visibility_labels=["ops_diagnostics"],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
        )
    with mock.patch.object(
        NotesRepository, "get_by_title", new=mock.AsyncMock(return_value=None)
    ):
        async with DatabaseContext(engine=db_engine) as db:
            await db.notes.add_or_update(
                title="Raced Ops Note",
                content="new findings",
                write_policy=_confined_policy(),
            )
    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Raced Ops Note", visibility_grants=None)
        assert note is not None
        assert note.content == "new findings"
        assert note.visibility_labels == ["ops_diagnostics"]
