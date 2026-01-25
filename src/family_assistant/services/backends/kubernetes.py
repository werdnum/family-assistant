"""Kubernetes backend for production deployments.

This backend creates Kubernetes Jobs to run worker tasks,
providing isolation and scalability in production environments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.services.worker_backend import WorkerTaskResult


class KubernetesBackend:
    """Kubernetes backend for running worker tasks as Jobs.

    Note: This is a placeholder. Full implementation is in Milestone 4.
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
        """Spawn a worker task as a Kubernetes Job."""
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def get_task_status(self, job_id: str) -> WorkerTaskResult:
        """Get the status of a Kubernetes Job."""
        raise NotImplementedError("Kubernetes backend not yet implemented")

    async def cancel_task(self, job_id: str) -> bool:
        """Cancel a running Kubernetes Job."""
        raise NotImplementedError("Kubernetes backend not yet implemented")
