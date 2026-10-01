"""The AI worker plugin's config, startup hook, task handlers and tool gating."""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from family_assistant.plugins.ai_workers.config import AIWorkersConfig
from family_assistant.plugins.ai_workers.instance import (
    CLEANUP_TASK_ID,
    AIWorkersInstance,
)
from family_assistant.plugins.ai_workers.tasks import WORKER_TASK_CLEANUP_TASK_TYPE
from family_assistant.plugins.ai_workers.tools import (
    NOT_CONFIGURED_ERROR,
    list_worker_tasks_tool,
    read_task_result_tool,
    render_cancel_worker_task_confirmation,
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
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )

    results = [
        await spawn_worker_tool(context, task_description="write a script"),
        await read_task_result_tool(context, task_id="task-1"),
        await list_worker_tasks_tool(context),
    ]

    for result in results:
        assert result.get_data() == {"error": NOT_CONFIGURED_ERROR}

    await context.db_context.worker_tasks.create_task(
        task_id="task-1",
        conversation_id="conv",
        interface_type="test",
        task_description="private task description",
    )
    prompt = await render_cancel_worker_task_confirmation(
        {"task_id": "task-1"}, context
    )
    assert "private task description" not in prompt
