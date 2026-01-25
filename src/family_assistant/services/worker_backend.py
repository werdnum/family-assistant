"""Abstract protocol for AI worker execution backends.

This module defines the protocol that all worker backends must implement,
allowing pluggable execution strategies (Kubernetes, Docker, Mock).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from family_assistant.config_models import (
        DockerBackendConfig,
        KubernetesBackendConfig,
    )


class WorkerStatus(Enum):
    """Status of a worker task."""

    PENDING = "pending"  # Task created, not yet submitted
    SUBMITTED = "submitted"  # Submitted to backend, waiting to start
    RUNNING = "running"  # Currently executing
    SUCCESS = "success"  # Completed successfully
    FAILED = "failed"  # Completed with error
    TIMEOUT = "timeout"  # Exceeded time limit
    CANCELLED = "cancelled"  # Manually cancelled


@dataclass
class WorkerTaskResult:
    """Result from getting task status from backend."""

    status: WorkerStatus
    exit_code: int | None = None
    error_message: str | None = None


class WorkerBackend(Protocol):
    """Protocol for worker execution backends.

    Implementations include:
    - KubernetesBackend: Creates Kubernetes Jobs (production)
    - DockerBackend: Spawns Docker containers (local development)
    - MockBackend: For testing without infrastructure
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
        """Spawn a worker task.

        Args:
            task_id: Unique identifier for this task
            prompt_path: Path to the prompt file (relative to workspace)
            output_dir: Directory for worker output (relative to workspace)
            webhook_url: URL to POST completion notification
            model: AI model to use (claude, gemini)
            timeout_minutes: Maximum execution time
            context_paths: Optional list of paths to mount as context (relative to workspace)

        Returns:
            Job/container identifier from the backend
        """
        ...

    async def get_task_status(self, job_id: str) -> WorkerTaskResult:
        """Get the current status of a task.

        Args:
            job_id: The job/container ID returned by spawn_task

        Returns:
            WorkerTaskResult with current status
        """
        ...

    async def cancel_task(self, job_id: str) -> bool:
        """Cancel a running task.

        Args:
            job_id: The job/container ID to cancel

        Returns:
            True if successfully cancelled, False if task not found or already complete
        """
        ...


def get_worker_backend(
    backend_type: str,
    workspace_root: str | None = None,
    docker_config: DockerBackendConfig | None = None,
    kubernetes_config: KubernetesBackendConfig | None = None,
) -> WorkerBackend:
    """Factory function to get the appropriate backend implementation.

    Args:
        backend_type: One of 'kubernetes', 'docker', 'mock'
        workspace_root: Root path for workspace volume (for docker/mock backends)
        docker_config: DockerBackendConfig for docker backend
        kubernetes_config: KubernetesBackendConfig for kubernetes backend

    Returns:
        WorkerBackend implementation

    Raises:
        ValueError: If backend_type is unknown
    """
    # Lazy imports to avoid circular dependencies and optional dependency issues.
    # Each backend may have its own heavy dependencies (kubernetes, docker libs).
    if backend_type == "mock":
        from family_assistant.services.backends.mock import (  # noqa: PLC0415
            MockBackend,
        )

        return MockBackend()
    elif backend_type == "docker":
        from family_assistant.services.backends.docker import (  # noqa: PLC0415
            DockerBackend,
        )

        return DockerBackend(config=docker_config, workspace_root=workspace_root)
    elif backend_type == "kubernetes":
        from family_assistant.services.backends.kubernetes import (  # noqa: PLC0415
            KubernetesBackend,
        )

        return KubernetesBackend()
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")
