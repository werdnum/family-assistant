"""What a deployment that changes nothing gets at startup.

Milestone 7 of docs/design/conversation-memory.md turns contribution on by
default, so the shipped configuration is now the configuration that runs
memory. The unit tests pin the two settings as configuration; this one pins
what startup does with them -- the enablement boundary it stamps and the sweep
it schedules -- through the methods `Assistant.run` itself calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_loader import load_config
from family_assistant.context_providers import NotesContextProvider
from family_assistant.memory.invariants import MEMORY_LABEL
from family_assistant.memory.limits import MemoryLimits
from family_assistant.memory.sweep import (
    MEMORY_REVIEW_SWEEP_TASK_ID,
    MEMORY_REVIEW_SWEEP_TASK_TYPE,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteWritePolicy

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.config_models import AppConfig
    from family_assistant.storage.repositories.tasks import TaskDict

HOUSEHOLD_PROFILES = {"default_assistant", "complex_tasks"}
CORE_MEMORY_SENTENCE = "The household eats dinner at 6"


async def _seed_core_memory_note(engine: AsyncEngine) -> None:
    """A curated core note, written the way the curator writes one."""
    await Database(engine).notes.add_or_update(
        title=MemoryLimits.DEFAULTS.core_note_title,
        content=f"- {CORE_MEMORY_SENTENCE} (2026-09-01, Alice).",
        include_in_prompt=True,
        visibility_labels=[MEMORY_LABEL],
        # ast-grep-ignore: no-unconstrained-note-write-policy - test seeding the note the curator would have written, with no profile in play
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )


async def _rendered_notes_context(
    assistant: Assistant, tmp_path: Path, engine: AsyncEngine
) -> str:
    """The notes context `default_assistant` would carry into a turn.

    Built through the two methods `_initialize_processing_service` itself
    calls, so what is measured is the profile production would run.
    """
    assistant.attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=engine, config=None
    )
    profile = next(
        p for p in assistant.config.service_profiles if p.id == "default_assistant"
    )
    # Calling the private methods on purpose: they are the ones startup calls.
    providers = assistant._build_profile_context_providers(  # pylint: disable=protected-access
        profile,
        None,
        assistant._profile_note_read_policy(profile),  # pylint: disable=protected-access
    )
    notes_provider = next(p for p in providers if isinstance(p, NotesContextProvider))
    fragments = await notes_provider.get_context_fragments(acting_user_id=None)
    return "\n".join(fragments)


async def _seeded_sweep(db: Database) -> TaskDict | None:
    """The recurring sweep row, if startup left one behind."""
    tasks = await db.tasks.get_all(task_type=MEMORY_REVIEW_SWEEP_TASK_TYPE)
    return next(
        (task for task in tasks if task["task_id"] == MEMORY_REVIEW_SWEEP_TASK_ID), None
    )


@pytest.fixture(name="shipped_assistant")
def shipped_assistant_fixture(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> Assistant:
    """An assistant over the shipped defaults, wired to the test database.

    Constructing the real clients reads each provider's key from the
    environment; nothing here issues a request.
    """
    for env_var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(env_var, f"fake-{env_var.lower()}-for-tests")
    config: AppConfig = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )
    assistant = Assistant(config, llm_client_overrides={})
    assistant.database_engine = db_engine
    return assistant


@pytest.mark.asyncio
async def test_startup_stamps_an_enablement_moment_for_the_household_profiles(
    shipped_assistant: Assistant, db_engine: AsyncEngine
) -> None:
    """The boundary an upgrading deployment gets: nothing older is reviewed.

    `record_enablement` runs on every startup, so this is also what decides
    that months of existing conversation stay uncurated when a deployment
    upgrades into the default.
    """
    before = datetime.now(UTC)

    # Calling the private method on purpose: it is the one `Assistant.run`
    # calls, so the assertion is about startup rather than about a stand-in.
    await shipped_assistant._record_memory_enablement()  # pylint: disable=protected-access

    enablement = await Database(db_engine).memory_review.get_enablement()
    assert set(enablement) == HOUSEHOLD_PROFILES
    assert all(moment >= before for moment in enablement.values())


@pytest.mark.asyncio
async def test_startup_schedules_the_review_sweep(
    shipped_assistant: Assistant, db_engine: AsyncEngine
) -> None:
    """Seeding is gated on `enabled` and on there being a contributor.

    Both hold in the shipped configuration, so an operator who configures
    nothing gets the recurring sweep that makes memory actually happen.
    """
    db = Database(db_engine)

    # Calling the private method on purpose: it is the one `_setup_system_tasks`
    # calls, so the assertion is about startup rather than about a stand-in.
    await shipped_assistant._seed_memory_review_sweep(db)  # pylint: disable=protected-access

    sweep = await _seeded_sweep(db)
    assert sweep is not None
    interval = shipped_assistant.config.memory_config.sweep_interval_minutes
    assert sweep["recurrence_rule"] == f"FREQ=MINUTELY;INTERVAL={interval}"


@pytest.mark.asyncio
async def test_the_household_profile_reads_the_core_memory_note(
    shipped_assistant: Assistant, db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The shipped default: memory reaches the prompt of the profile people use."""
    await _seed_core_memory_note(db_engine)

    rendered = await _rendered_notes_context(shipped_assistant, tmp_path, db_engine)

    assert CORE_MEMORY_SENTENCE in rendered


@pytest.mark.asyncio
async def test_the_master_switch_keeps_memory_out_of_the_profile_prompt(
    shipped_assistant: Assistant, db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """`memory_config.enabled: false` is a master switch, not a sweep switch.

    The core note exists and `default_assistant` still carries
    `memory_read: true`, and the profile's context must carry no memory anyway
    -- a deployment that turned memory off would otherwise still be running on
    curated memory in every foreground turn.
    """
    shipped_assistant.config.memory_config.enabled = False
    await _seed_core_memory_note(db_engine)

    rendered = await _rendered_notes_context(shipped_assistant, tmp_path, db_engine)

    assert CORE_MEMORY_SENTENCE not in rendered


@pytest.mark.asyncio
async def test_turning_the_master_switch_off_schedules_no_sweep(
    shipped_assistant: Assistant, db_engine: AsyncEngine
) -> None:
    """`memory_config.enabled: false` is the one switch that covers everything.

    It leaves the profiles' settings alone and still seeds nothing, which is
    what makes it the answer for a deployment that wants no memory at all.
    """
    shipped_assistant.config.memory_config.enabled = False
    db = Database(db_engine)

    # Calling the private method on purpose: it is the one `_setup_system_tasks`
    # calls, so the assertion is about startup rather than about a stand-in.
    await shipped_assistant._seed_memory_review_sweep(db)  # pylint: disable=protected-access

    assert await _seeded_sweep(db) is None
