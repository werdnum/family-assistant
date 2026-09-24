"""Whether startup schedules rotation of the stored sandbox egress credential.

Rotation keeps a live GitHub token in Google's credential store, so it must be
scheduled exactly when a profile's egress rules read one: never for the
shipped configuration, which gives `coder` no credential at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_loader import load_config
from family_assistant.config_models import AntigravityEnvironmentConfig
from family_assistant.llm.antigravity_egress import (
    EGRESS_CREDENTIAL_ROTATION_INTERVAL_MINUTES,
    EGRESS_CREDENTIAL_ROTATION_TASK_ID,
    EGRESS_CREDENTIAL_ROTATION_TASK_TYPE,
)
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.storage.repositories.tasks import TaskDict


async def _seeded_rotation(db: Database) -> TaskDict | None:
    tasks = await db.tasks.get_all(task_type=EGRESS_CREDENTIAL_ROTATION_TASK_TYPE)
    return next(
        (t for t in tasks if t["task_id"] == EGRESS_CREDENTIAL_ROTATION_TASK_ID), None
    )


@pytest.fixture(name="shipped_assistant")
def shipped_assistant_fixture(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> Assistant:
    for env_var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(env_var, f"fake-{env_var.lower()}-for-tests")
    config = load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )
    assistant = Assistant(config, llm_client_overrides={})
    assistant.database_engine = db_engine
    return assistant


@pytest.mark.asyncio
async def test_shipped_defaults_schedule_no_rotation(
    shipped_assistant: Assistant, db_engine: AsyncEngine
) -> None:
    # Calling the private method on purpose: it is the one `Assistant.run` calls.
    await shipped_assistant._seed_egress_credential_rotation()  # pylint: disable=protected-access

    assert await _seeded_rotation(Database(db_engine)) is None


@pytest.mark.asyncio
async def test_a_stored_github_rule_schedules_rotation(
    shipped_assistant: Assistant, db_engine: AsyncEngine
) -> None:
    coder = next(
        p for p in shipped_assistant.config.service_profiles if p.id == "coder"
    )
    antigravity_config = coder.processing_config.antigravity_config
    assert antigravity_config is not None
    antigravity_config.environment = AntigravityEnvironmentConfig.model_validate({
        "network": "allowlist",
        "allowlist": [
            {
                "domain": "api.github.com",
                "credential": {"type": "github_app", "scheme": "bearer"},
            },
            {
                "domain": "github.com",
                "credential": {"type": "github_app", "scheme": "basic"},
            },
        ],
    })

    # The private seeding method again, for the same reason: `Assistant.run` calls it.
    await shipped_assistant._seed_egress_credential_rotation()  # pylint: disable=protected-access

    rotation = await _seeded_rotation(Database(db_engine))
    assert rotation is not None
    assert rotation["recurrence_rule"] == (
        f"FREQ=MINUTELY;INTERVAL={EGRESS_CREDENTIAL_ROTATION_INTERVAL_MINUTES}"
    )
