"""Memory-store invariants enforced at the notes repository.

Slice 1 of docs/design/conversation-memory.md: every writer of a memory note --
the notes UI, the foreground tool, and (later) the curator -- is held to the
same shape, because the enforcement lives at the repository rather than in any
one caller's policy.
"""

from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.memory.invariants import (
    MEMORY_LABEL,
    MemoryStoreRevisionConflict,
    MemoryWriteError,
)
from family_assistant.memory.limits import MemoryLimits
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.database import Database
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.tools.notes import add_or_update_note_tool, delete_note_tool
from family_assistant.tools.types import ToolExecutionContext

CORE_TITLE = MemoryLimits.DEFAULTS.core_note_title

# Small enough to write an over-cap note inline, so the tests read as
# statements about the rule rather than about a wall of text. The core note's
# cap leaves room for the derived topic index these tests are not about, since
# it is regenerated into the core note on every memory write.
TIGHT_LIMITS = MemoryLimits(core_note_max_chars=300, topic_note_max_chars=60)


def _db(engine: AsyncEngine, *, limits: MemoryLimits = TIGHT_LIMITS) -> Database:
    return Database(engine=engine, memory_limits=limits)


def _tool_context(db_context: Database) -> ToolExecutionContext:
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
        visibility_grants=None,
        # These are the foreground whole-note paths, exercised as a profile
        # that reads memory; the refusal a non-reading profile gets instead has
        # its own tests.
        memory_read=True,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


async def _write_topic(
    db: Database,
    title: str,
    content: str,
    *,
    include_in_prompt: bool = False,
    labels: list[str] | None = None,
    # ast-grep-ignore: no-dict-any - provenance metadata stores compact runtime taint JSON
    provenance_metadata: dict[str, object] | None = None,
) -> None:
    await db.notes.add_or_update(
        title,
        content,
        include_in_prompt,
        visibility_labels=[MEMORY_LABEL] if labels is None else labels,
        # Admin surface equivalent: this suite is about the memory invariants,
        # not about visibility confinement.
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance_metadata=provenance_metadata,
    )


def _external_provenance() -> dict[str, object]:
    state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="web-fetch",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="turn read an untrusted web page",
        )
    )
    return {"taint_metadata": state.to_metadata()}


# ---------------------------------------------------------------------------
# Bootstrap and the always-loaded singleton
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_memory_write_bootstraps_the_core_note(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")

    core = await db.notes.get_by_title(
        CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert core is not None
    assert core.include_in_prompt is True
    assert core.visibility_labels == [MEMORY_LABEL]

    core_row = await db.notes.get_by_id(
        await db.memory_store.get_core_note_id() or 0,
        read_policy=NoteReadPolicy.UNRESTRICTED,
    )
    assert core_row is not None
    assert core_row.title == CORE_TITLE


@pytest.mark.asyncio
async def test_existing_non_memory_note_with_core_title_blocks_bootstrap(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await db.notes.add_or_update(
        CORE_TITLE,
        "my own note",
        False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )

    with pytest.raises(MemoryWriteError, match="Rename it"):
        await _write_topic(db, "Sam", "- likes trams")


@pytest.mark.asyncio
async def test_second_always_loaded_memory_note_refused(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")

    with pytest.raises(MemoryWriteError, match="Only the core memory note"):
        await _write_topic(db, "Routines", "- bins on Tuesday", include_in_prompt=True)


@pytest.mark.asyncio
async def test_core_note_cannot_be_excluded_from_the_prompt(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")

    with pytest.raises(MemoryWriteError, match="cannot be excluded"):
        await _write_topic(db, CORE_TITLE, "- small", include_in_prompt=False)


@pytest.mark.asyncio
async def test_core_note_cannot_lose_the_memory_label(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")

    with pytest.raises(MemoryWriteError, match="must keep"):
        await _write_topic(db, CORE_TITLE, "- small", include_in_prompt=True, labels=[])


@pytest.mark.asyncio
async def test_renaming_the_core_note_keeps_it_the_core(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")
    core_id = await db.memory_store.get_core_note_id()

    await db.notes.rename_and_update(
        CORE_TITLE,
        "Our Household",
        "- small",
        True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )

    assert await db.memory_store.get_core_note_id() == core_id
    renamed = await db.notes.get_by_title(
        "Our Household", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert renamed is not None
    assert renamed.include_in_prompt is True

    # Still the core: it is the one memory note that may be always-loaded, and
    # a second one is still refused under its new title.
    with pytest.raises(MemoryWriteError, match="Only the core memory note"):
        await _write_topic(db, "Routines", "- bins", include_in_prompt=True)


@pytest.mark.asyncio
async def test_a_note_taking_the_renamed_core_title_is_a_topic_note(
    db_engine: AsyncEngine,
) -> None:
    """The configured title identifies the core note only until it has an id.

    A rename leaves the configured title free, and the title is what a writer
    controls: if it still meant "the core note", anyone could have a second
    always-loaded memory note by naming one after the shipped default.
    """
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")
    core_id = await db.memory_store.get_core_note_id()
    await db.notes.rename_and_update(
        CORE_TITLE,
        "Our Household",
        "- small",
        True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )

    with pytest.raises(MemoryWriteError, match="Only the core memory note"):
        await _write_topic(db, CORE_TITLE, "- mine", include_in_prompt=True)

    await _write_topic(db, CORE_TITLE, "- mine")

    impostor = await db.notes.get_by_title(
        CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert impostor is not None
    assert impostor.include_in_prompt is False
    assert await db.memory_store.get_core_note_id() == core_id


@pytest.mark.asyncio
async def test_exactly_one_memory_note_is_always_loaded_after_a_rename(
    db_engine: AsyncEngine,
) -> None:
    """The invariant the previous test is about, stated over the whole store."""
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")
    await db.notes.rename_and_update(
        CORE_TITLE,
        "Our Household",
        "- small",
        True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )
    await _write_topic(db, CORE_TITLE, "- mine")

    async with db_engine.connect() as connection:
        rows = (
            await connection.execute(
                select(notes_table.c.title).where(
                    notes_table.c.include_in_prompt.is_(True)
                )
            )
        ).fetchall()

    assert [row.title for row in rows] == ["Our Household"]


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_cap_core_write_refused(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")

    with pytest.raises(MemoryWriteError, match="over its 300-character limit"):
        await _write_topic(db, CORE_TITLE, "x" * 301, include_in_prompt=True)


@pytest.mark.asyncio
async def test_over_cap_topic_write_refused(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)

    with pytest.raises(MemoryWriteError, match="over its 60-character limit"):
        await _write_topic(db, "Trip", "y" * 61)


@pytest.mark.asyncio
async def test_append_that_crosses_the_cap_is_refused(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Trip", "y" * 50)

    with pytest.raises(MemoryWriteError, match="over its 60-character limit"):
        await db.notes.add_or_update(
            "Trip",
            "z" * 20,
            False,
            append=True,
            write_policy=NoteWritePolicy.UNCONSTRAINED,
        )


@pytest.mark.asyncio
async def test_memory_note_may_not_carry_frontmatter(db_engine: AsyncEngine) -> None:
    db = _db(db_engine, limits=MemoryLimits.DEFAULTS)

    with pytest.raises(MemoryWriteError, match="frontmatter"):
        await _write_topic(
            db,
            "Trip",
            "---\nname: trip\ndescription: a skill\n---\n\nbody",
        )


# ---------------------------------------------------------------------------
# Provenance floor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_externally_authored_turn_cannot_write_memory(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)

    with pytest.raises(MemoryWriteError, match="outside the household"):
        await _write_topic(
            db, "Trip", "- hotel", provenance_metadata=_external_provenance()
        )


@pytest.mark.asyncio
async def test_reviewed_material_may_be_written_to_memory(
    db_engine: AsyncEngine,
) -> None:
    """The provenance rule is the reuse predicate, not authorship."""
    db = _db(db_engine)
    reviewed = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id="Packing procedure",
            tier=SourceTrustTier.MACHINE_REVIEWED,
            labels=frozenset(),
            reason="prompt carried a reviewed note",
        )
    )
    await _write_topic(
        db,
        "Trip",
        "- hotel",
        provenance_metadata={"taint_metadata": reviewed.to_metadata()},
    )

    stored = await db.notes.get_by_title(
        "Trip", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert stored is not None


@pytest.mark.asyncio
async def test_unstamped_write_satisfies_the_provenance_rule(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Trip", "- hotel", provenance_metadata=None)

    stored = await db.notes.get_by_title(
        "Trip", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert stored is not None


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_note_cannot_be_deleted(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")

    with pytest.raises(MemoryWriteError, match="cannot be deleted"):
        await db.notes.delete(CORE_TITLE)

    assert (
        await db.notes.get_by_title(CORE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED)
        is not None
    )


@pytest.mark.asyncio
async def test_deleting_a_topic_note_is_allowed_and_bumps_the_revision(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")
    before = await db.memory_store.get_revision()

    assert await db.notes.delete("Sam") is True

    assert (
        await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )
    assert await db.memory_store.get_revision() == before + 1


# ---------------------------------------------------------------------------
# Store revision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_memory_write_bumps_the_revision(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    start = await db.memory_store.get_revision()

    await _write_topic(db, "Sam", "- likes trams")
    after_create = await db.memory_store.get_revision()
    assert after_create > start

    await _write_topic(db, "Sam", "- likes trams and buses")
    after_update = await db.memory_store.get_revision()
    assert after_update == after_create + 1

    await db.notes.rename_and_update(
        "Sam",
        "Sam (family)",
        "- likes trams",
        False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )
    assert await db.memory_store.get_revision() == after_update + 1


@pytest.mark.asyncio
async def test_removing_the_memory_label_is_a_memory_write(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")
    before = await db.memory_store.get_revision()

    await _write_topic(db, "Sam", "- likes trams", labels=[])

    stored = await db.notes.get_by_title("Sam", read_policy=NoteReadPolicy.UNRESTRICTED)
    assert stored is not None
    assert stored.visibility_labels == []
    assert await db.memory_store.get_revision() == before + 1


@pytest.mark.asyncio
async def test_conditional_bump_against_a_stale_revision_conflicts(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    read_revision = await db.memory_store.get_revision()
    await _write_topic(db, "Sam", "- likes trams")

    with pytest.raises(MemoryStoreRevisionConflict):
        await db.memory_store.bump_revision(expected_revision=read_revision)

    current = await db.memory_store.get_revision()
    assert await db.memory_store.bump_revision(expected_revision=current) == current + 1


# ---------------------------------------------------------------------------
# Notes that are not memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_note_is_untouched_by_the_memory_invariants(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    before = await db.memory_store.get_revision()

    await db.notes.add_or_update(
        "Shopping",
        "x" * 500,
        True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )
    await db.notes.add_or_update(
        "Another",
        "y" * 500,
        True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )
    assert await db.notes.delete("Another") is True

    assert await db.memory_store.get_revision() == before
    assert await db.memory_store.get_core_note_id() is None
    stored = await db.notes.get_by_title(
        "Shopping", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert stored is not None
    assert stored.include_in_prompt is True


# ---------------------------------------------------------------------------
# The same refusals through the tool and the web API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_path_reports_the_refusal(db_engine: AsyncEngine) -> None:
    db = _db(db_engine)
    exec_context = _tool_context(db)

    result = await add_or_update_note_tool(
        exec_context,
        title="Trip",
        content="y" * 61,
        visibility_labels=[MEMORY_LABEL],
    )

    assert "over its 60-character limit" in result
    assert (
        await db.notes.get_by_title("Trip", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )


@pytest.mark.asyncio
async def test_tool_path_reports_a_refused_core_deletion(
    db_engine: AsyncEngine,
) -> None:
    db = _db(db_engine)
    await _write_topic(db, "Sam", "- likes trams")

    result = await delete_note_tool(CORE_TITLE, _tool_context(db))

    assert result["success"] is False
    assert "cannot be deleted" in result["message"]


@pytest.mark.asyncio
async def test_web_api_maps_a_refusal_to_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    # The web path runs on the deployment's limits, so the note has to exceed
    # the shipped topic cap rather than a test-local one.
    response = await api_client.post(
        "/api/notes/",
        json={
            "title": "Trip",
            "content": "y" * (MemoryLimits.DEFAULTS.topic_note_max_chars + 1),
            "include_in_prompt": False,
            "visibility_labels": [MEMORY_LABEL],
        },
    )

    assert response.status_code == 422
    assert "over its" in response.json()["detail"]

    db = Database(engine=db_engine)
    assert (
        await db.notes.get_by_title("Trip", read_policy=NoteReadPolicy.UNRESTRICTED)
        is None
    )


@pytest.mark.asyncio
async def test_web_api_refuses_to_delete_the_core_note(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    await _write_topic(db, "Sam", "- likes trams")

    response = await api_client.delete(f"/api/notes/{CORE_TITLE}")

    assert response.status_code == 422
    assert "cannot be deleted" in response.json()["detail"]
    remaining = await db.fetch_one(
        select(notes_table.c.title).where(notes_table.c.title == CORE_TITLE)
    )
    assert remaining is not None
