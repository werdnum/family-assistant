"""A configured AI worker sandbox."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from family_assistant.plugins.ai_workers.backend import get_worker_backend
from family_assistant.plugins.ai_workers.lifecycle import reconcile_stale_tasks
from family_assistant.plugins.ai_workers.tasks import WORKER_TASK_CLEANUP_TASK_TYPE
from family_assistant.plugins.base import PluginInstance
from family_assistant.storage.tasks import TaskPriority

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.plugins.ai_workers.backend import WorkerBackend
    from family_assistant.plugins.ai_workers.config import AIWorkersConfig
    from family_assistant.plugins.base import PluginStartupContext

logger = logging.getLogger(__name__)

CLEANUP_TASK_ID = "system_worker_task_cleanup_daily"


class AIWorkersInstance(PluginInstance):
    """One sandbox that ``spawn_worker`` launches coding agents into."""

    def __init__(self, config: AIWorkersConfig) -> None:
        self.config = config

    def backend(self, workspace_root: Path) -> WorkerBackend:
        """The backend that runs, reports on and cancels this sandbox's jobs."""
        return get_worker_backend(
            self.config.backend_type,
            workspace_root=str(workspace_root),
            docker_config=self.config.docker,
            kubernetes_config=self.config.kubernetes,
        )

    async def on_startup(self, context: PluginStartupContext) -> None:
        """Seed the daily cleanup and settle tasks a restart left running."""
        await context.database.tasks.enqueue(
            task_id=CLEANUP_TASK_ID,
            task_type=WORKER_TASK_CLEANUP_TASK_TYPE,
            payload={
                "retention_hours": self.config.task_retention_hours,
                "workspace_path": str(context.shared_workspace_path),
            },
            scheduled_at=datetime.now(UTC),
            recurrence_rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0",
            max_retries_override=5,
            priority=TaskPriority.BACKGROUND,
        )
        reconciled = await reconcile_stale_tasks(
            context.database, self.backend(context.shared_workspace_path)
        )
        if reconciled:
            logger.info("Reconciled %s stale worker tasks on startup", reconciled)
