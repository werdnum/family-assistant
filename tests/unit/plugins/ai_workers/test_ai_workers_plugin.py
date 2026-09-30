"""The AI worker plugin's config, startup hook, task handlers and tool gating."""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from family_assistant.config_loader import load_config
from family_assistant.plugins.ai_workers.config import AIWorkersConfig
from family_assistant.plugins.ai_workers.instance import (
    CLEANUP_TASK_ID,
    AIWorkersInstance,
)
from family_assistant.plugins.ai_workers.tasks import WORKER_TASK_CLEANUP_TASK_TYPE
from family_assistant.plugins.ai_workers.tools import (
    NOT_CONFIGURED_ERROR,
    spawn_worker_tool,
)
from family_assistant.plugins.base import PluginInstance, PluginStartupContext
from family_assistant.plugins.config import PluginsConfig
from family_assistant.plugins.registry import plugin_task_handlers
from family_assistant.plugins.runtime import PluginRuntime, ProfilePlugins
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import tasks_table
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.config_models import AppConfig


def _load(tmp_path: Path, yaml_text: str) -> AppConfig:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_text)
    return load_config(
        defaults_file_path=str(tmp_path / "missing_defaults.yaml"),
        config_file_path=str(config_file),
        prompts_file_path=str(tmp_path / "missing_prompts.yaml"),
        load_dotenv_file=False,
    )


class TestLegacyConfig:
    """Deployed config still written as ai_worker_config keeps loading."""

    def test_disabled_block_is_dropped_but_keeps_its_workspace(
        self, tmp_path: Path
    ) -> None:
        config = _load(
            tmp_path,
            """
ai_worker_config:
  enabled: false
  webhook_url: "http://fa.local/webhook/event"
  workspace_mount_path: "/srv/workspace"
  kubernetes:
    namespace: ml-bot
""",
        )
        assert config.plugins.ai_workers == {}
        assert config.shared_workspace_path == "/srv/workspace"

    def test_enabled_block_becomes_the_default_instance(self, tmp_path: Path) -> None:
        config = _load(
            tmp_path,
            """
ai_worker_config:
  enabled: true
  backend_type: docker
  available_agents: [claude]
""",
        )
        default = config.plugins.ai_workers["default"]
        assert default.backend_type == "docker"
        assert default.available_agents == ["claude"]

    def test_the_new_location_wins(self, tmp_path: Path) -> None:
        config = _load(
            tmp_path,
            """
shared_workspace_path: "/new"
ai_worker_config:
  enabled: true
  workspace_mount_path: "/old"
  max_concurrent_workers: 1
plugins:
  ai_workers:
    default:
      max_concurrent_workers: 5
""",
        )
        assert config.shared_workspace_path == "/new"
        assert config.plugins.ai_workers["default"].max_concurrent_workers == 5


def test_a_second_sandbox_is_refused() -> None:
    """Worker tasks don't record their sandbox, so reconciling two would clash."""
    with pytest.raises(ValidationError, match="supports one instance"):
        PluginsConfig(
            ai_workers={"default": AIWorkersConfig(), "other": AIWorkersConfig()}
        )


def test_the_cleanup_handler_is_contributed_by_the_plugin() -> None:
    assert WORKER_TASK_CLEANUP_TASK_TYPE in plugin_task_handlers()


@pytest.mark.asyncio
async def test_startup_seeds_cleanup_and_settles_orphaned_tasks(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    db = Database(db_engine)
    await db.worker_tasks.create_task(
        task_id="orphan",
        conversation_id="conv",
        interface_type="test",
        task_description="never submitted",
    )
    await db.worker_tasks.update_task_status(task_id="orphan", status="submitted")
    sandbox = AIWorkersInstance(
        AIWorkersConfig(backend_type="mock", task_retention_hours=12)
    )

    await sandbox.on_startup(
        PluginStartupContext(database=db, shared_workspace_path=tmp_path)
    )

    rows = await db.fetch_all(
        select(tasks_table.c.task_type, tasks_table.c.payload).where(
            tasks_table.c.task_id == CLEANUP_TASK_ID
        )
    )
    assert [(row["task_type"], row["payload"]) for row in rows] == [
        (
            WORKER_TASK_CLEANUP_TASK_TYPE,
            {"retention_hours": 12, "workspace_path": str(tmp_path)},
        )
    ]
    orphan = await db.worker_tasks.get_task("orphan")
    assert orphan is not None
    assert orphan["status"] == "failed"


class _FailingInstance(PluginInstance):
    async def on_startup(self, context: PluginStartupContext) -> None:
        raise RuntimeError("backend unreachable")


class _RecordingInstance(PluginInstance):
    def __init__(self) -> None:
        self.started = False

    async def on_startup(self, context: PluginStartupContext) -> None:
        self.started = True


@pytest.mark.asyncio
async def test_one_failing_startup_hook_does_not_stop_the_others(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    runtime = PluginRuntime(PluginsConfig())
    recorder = _RecordingInstance()
    runtime._instances = {
        ("a", "default"): _FailingInstance(),
        ("b", "default"): recorder,
    }

    await runtime.on_startup(
        PluginStartupContext(
            database=Database(db_engine), shared_workspace_path=tmp_path
        )
    )

    assert recorder.started


@pytest.mark.asyncio
async def test_a_profile_without_a_sandbox_is_told_so(db_engine: AsyncEngine) -> None:
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conv",
        user_name="tester",
        turn_id=None,
        db_context=Database(db_engine),
        processing_service=None,
        clock=None,
        plugins=ProfilePlugins(()),
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )

    result = await spawn_worker_tool(context, task_description="write a script")

    assert result.get_data() == {"error": NOT_CONFIGURED_ERROR}
