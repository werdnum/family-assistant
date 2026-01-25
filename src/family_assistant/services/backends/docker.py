"""Docker backend for local development.

This backend spawns Docker containers to run worker tasks,
useful for local development and testing without Kubernetes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.services.worker_backend import WorkerTaskResult


class DockerBackend:
    """Docker backend for running worker tasks in containers.

    Note: This is a placeholder. Full implementation is in Milestone 3.
    """

    async def spawn_task(
        self,
        task_id: str,
        prompt_path: str,
        output_dir: str,
        webhook_url: str,
        model: str,
        timeout_minutes: int,
        context_paths: list[str] | None = None,
    ) -> str:
        """Spawn a worker task in a Docker container."""
        raise NotImplementedError("Docker backend not yet implemented")

    async def get_task_status(self, job_id: str) -> WorkerTaskResult:
        """Get the status of a Docker container task."""
        raise NotImplementedError("Docker backend not yet implemented")

    async def cancel_task(self, job_id: str) -> bool:
        """Cancel a running Docker container."""
        raise NotImplementedError("Docker backend not yet implemented")
