"""Docker backend for local development.

This backend spawns Docker containers to run worker tasks,
useful for local development and testing without Kubernetes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from family_assistant.services.worker_backend import WorkerStatus, WorkerTaskResult

if TYPE_CHECKING:
    from family_assistant.config_models import DockerBackendConfig

from family_assistant.config_models import WorkerResourceLimits

logger = logging.getLogger(__name__)

# Default max turns for the AI agent
DEFAULT_MAX_TURNS = 50


@dataclass
class DockerTask:
    """Represents a Docker container running a worker task."""

    task_id: str
    container_id: str
    prompt_path: str
    output_dir: str
    model: str
    timeout_minutes: int
    status: WorkerStatus = WorkerStatus.SUBMITTED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    error_message: str | None = None


class DockerBackend:
    """Docker backend for running worker tasks in containers.

    Uses the `docker` CLI via asyncio.subprocess for container management.
    This avoids adding a heavy dependency like aiodocker while providing
    full async functionality.

    Example usage:
        backend = DockerBackend(config, workspace_root="/workspace")
        job_id = await backend.spawn_task(
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )
    """

    def __init__(
        self,
        config: DockerBackendConfig | None = None,
        workspace_root: str | None = None,
    ) -> None:
        """Initialize the Docker backend.

        Args:
            config: Docker-specific configuration. If None, uses defaults.
            workspace_root: Root path for workspace volume mount.
                           If None, uses current working directory.
        """
        self._config = config
        self._workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self._tasks: dict[str, DockerTask] = {}

    @property
    def image(self) -> str:
        """Get the container image to use."""
        if self._config:
            return self._config.image
        return "ghcr.io/werdnum/ai-coding-base:latest"

    @property
    def network(self) -> str:
        """Get the Docker network to use."""
        if self._config:
            return self._config.network
        return "bridge"

    @property
    def resources(self) -> WorkerResourceLimits:
        """Get the resource limits for worker containers."""
        if self._config:
            return self._config.resources
        return WorkerResourceLimits()

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
        """Spawn a worker task in a Docker container.

        Args:
            task_id: Unique identifier for this task
            prompt_path: Path to the prompt file (relative to workspace)
            output_dir: Path for output files (relative to workspace)
            webhook_url: URL to POST completion webhook
            model: AI model to use ("claude" or "gemini")
            timeout_minutes: Maximum execution time
            context_paths: Additional paths to include as context (unused for now)

        Returns:
            Container ID that can be used to track the task
        """
        # Build docker run command
        cmd = await self._build_docker_command(
            task_id=task_id,
            prompt_path=prompt_path,
            output_dir=output_dir,
            webhook_url=webhook_url,
            model=model,
            timeout_minutes=timeout_minutes,
        )

        logger.info(f"Starting Docker container for task {task_id}")
        logger.debug(f"Docker command: {' '.join(cmd)}")

        # Start the container
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() if stderr else "Unknown error"
            logger.error(f"Failed to start container for task {task_id}: {error_msg}")
            raise RuntimeError(f"Failed to start Docker container: {error_msg}")

        container_id = stdout.decode().strip()
        if not container_id:
            raise RuntimeError("Docker returned empty container ID")

        # Track the task
        task = DockerTask(
            task_id=task_id,
            container_id=container_id,
            prompt_path=prompt_path,
            output_dir=output_dir,
            model=model,
            timeout_minutes=timeout_minutes,
            status=WorkerStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        self._tasks[container_id] = task

        logger.info(f"Started container {container_id[:12]} for task {task_id}")
        return container_id

    def _convert_memory_to_docker(self, k8s_memory: str) -> str:
        """Convert Kubernetes memory format to Docker format.

        Args:
            k8s_memory: Memory in Kubernetes format (e.g., "512Mi", "2Gi", "2G")

        Returns:
            Memory in Docker format (e.g., "512m", "2g")
        """
        # Kubernetes uses Mi/Gi/Ki/M/G/K, Docker uses m/g/k (lowercase)
        # Order matters: check longer suffixes first
        result = k8s_memory
        for k8s_suffix, docker_suffix in [
            ("Mi", "m"),
            ("Gi", "g"),
            ("Ki", "k"),
            ("M", "m"),
            ("G", "g"),
            ("K", "k"),
        ]:
            if result.endswith(k8s_suffix):
                return result[: -len(k8s_suffix)] + docker_suffix
        return result

    def _convert_cpu_to_docker(self, k8s_cpu: str) -> str:
        """Convert Kubernetes CPU format to Docker format.

        Args:
            k8s_cpu: CPU in Kubernetes format (e.g., "500m" millicores, "2" cores)

        Returns:
            CPU in Docker format (e.g., "0.5", "2.0")

        Raises:
            ValueError: If the CPU value is malformed.
        """
        # Kubernetes uses millicores (e.g., "500m" = 0.5 cores) or whole cores
        if k8s_cpu.endswith("m"):
            try:
                millicores = int(k8s_cpu[:-1])
                return str(millicores / 1000)
            except ValueError as e:
                raise ValueError(
                    f"Invalid CPU millicore value: {k8s_cpu!r}. "
                    "Expected format like '500m' or '2000m'."
                ) from e
        # Already a number (e.g., "2" = 2 cores) - validate it's numeric
        try:
            float(k8s_cpu)
            return k8s_cpu
        except ValueError as e:
            raise ValueError(
                f"Invalid CPU value: {k8s_cpu!r}. "
                "Expected format like '500m' (millicores) or '2' (cores)."
            ) from e

    async def _build_docker_command(
        self,
        task_id: str,
        prompt_path: str,
        output_dir: str,
        webhook_url: str,
        model: str,
        timeout_minutes: int,
    ) -> list[str]:
        """Build the docker run command with all arguments."""
        cmd = [
            "docker",
            "run",
            "--detach",  # Run in background
            "--rm",  # Remove container when done
            f"--name=worker-{task_id}",
            f"--network={self.network}",
            # Resource limits
            f"--memory={self._convert_memory_to_docker(self.resources.memory_limit)}",
            f"--cpus={self._convert_cpu_to_docker(self.resources.cpu_limit)}",
        ]

        # Environment variables for the task runner
        env_vars = {
            "TASK_ID": task_id,
            "TASK_INPUT": f"/workspace/{prompt_path}",
            "TASK_OUTPUT_DIR": f"/workspace/{output_dir}",
            "TASK_WEBHOOK_URL": webhook_url,
            "AI_AGENT": model,
            "MAX_TURNS": str(DEFAULT_MAX_TURNS),
            "TASK_TIMEOUT_MINUTES": str(timeout_minutes),
        }

        for key, value in env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Mount workspace volume
        workspace_abs = str(self._workspace_root.resolve())
        cmd.extend(["-v", f"{workspace_abs}:/workspace"])

        # Mount auth config paths if configured
        if self._config:
            self._add_auth_mounts(cmd, model)

        # Image and command
        cmd.append(self.image)
        cmd.append("run-task")  # The entrypoint command

        return cmd

    def _add_auth_mounts(self, cmd: list[str], model: str) -> None:
        """Add authentication config volume mounts based on model.

        Args:
            cmd: Command list to append to
            model: The AI model being used ("claude" or "gemini")
        """
        if model == "claude" and self._config and self._config.claude_config_path:
            config_path = Path(self._config.claude_config_path).expanduser()
            if config_path.exists():
                cmd.extend(["-v", f"{config_path}:/home/user/.claude:ro"])
                logger.debug(f"Mounting Claude config from {config_path}")

        elif model == "gemini" and self._config and self._config.gemini_config_path:
            config_path = Path(self._config.gemini_config_path).expanduser()
            if config_path.exists():
                cmd.extend(["-v", f"{config_path}:/home/user/.gemini:ro"])
                logger.debug(f"Mounting Gemini config from {config_path}")

    async def get_task_status(self, job_id: str) -> WorkerTaskResult:
        """Get the status of a Docker container task.

        Args:
            job_id: The container ID returned by spawn_task

        Returns:
            WorkerTaskResult with current status
        """
        task = self._tasks.get(job_id)
        if not task:
            return WorkerTaskResult(
                status=WorkerStatus.FAILED,
                error_message=f"Task not found: {job_id}",
            )

        # Check container status
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}:{{.State.ExitCode}}",
            job_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            # Container not found - might have been removed
            if task.status in {
                WorkerStatus.SUCCESS,
                WorkerStatus.FAILED,
                WorkerStatus.TIMEOUT,
            }:
                return WorkerTaskResult(
                    status=task.status,
                    exit_code=task.exit_code,
                    error_message=task.error_message,
                )
            return WorkerTaskResult(
                status=WorkerStatus.FAILED,
                error_message="Container not found",
            )

        output = stdout.decode().strip()
        status_str, exit_code_str = output.split(":")
        exit_code = int(exit_code_str) if exit_code_str else None

        # Map Docker status to WorkerStatus
        if status_str == "running":
            return WorkerTaskResult(status=WorkerStatus.RUNNING)
        elif status_str == "exited":
            task.completed_at = datetime.now(UTC)
            task.exit_code = exit_code
            if exit_code == 0:
                task.status = WorkerStatus.SUCCESS
            else:
                task.status = WorkerStatus.FAILED
                task.error_message = f"Container exited with code {exit_code}"
            return WorkerTaskResult(
                status=task.status,
                exit_code=exit_code,
                error_message=task.error_message,
            )
        elif status_str in {"created", "restarting"}:
            return WorkerTaskResult(status=WorkerStatus.SUBMITTED)
        else:
            return WorkerTaskResult(
                status=WorkerStatus.FAILED,
                error_message=f"Unknown container status: {status_str}",
            )

    async def cancel_task(self, job_id: str) -> bool:
        """Cancel a running Docker container.

        Args:
            job_id: The container ID to cancel

        Returns:
            True if cancelled successfully, False otherwise
        """
        task = self._tasks.get(job_id)
        if not task:
            logger.warning(f"Cannot cancel unknown task: {job_id}")
            return False

        if task.status in {
            WorkerStatus.SUCCESS,
            WorkerStatus.FAILED,
            WorkerStatus.TIMEOUT,
            WorkerStatus.CANCELLED,
        }:
            logger.debug(f"Task {job_id} already in terminal state: {task.status}")
            return False

        logger.info(f"Cancelling container {job_id[:12]}")

        proc = await asyncio.create_subprocess_exec(
            "docker",
            "stop",
            "--time=10",  # Grace period
            job_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() if stderr else "Unknown error"
            logger.error(f"Failed to stop container {job_id}: {error_msg}")
            return False

        task.status = WorkerStatus.CANCELLED
        task.completed_at = datetime.now(UTC)
        logger.info(f"Cancelled container {job_id[:12]}")
        return True

    async def get_container_logs(self, job_id: str, tail: int = 100) -> str | None:
        """Get logs from a container.

        Args:
            job_id: The container ID
            tail: Number of lines to retrieve from the end

        Returns:
            Log output or None if not available
        """
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            f"--tail={tail}",
            job_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return None

        return stdout.decode()

    async def wait_for_completion(
        self,
        job_id: str,
        poll_interval: float = 2.0,
        timeout: float | None = None,
    ) -> WorkerTaskResult:
        """Wait for a container to complete.

        Args:
            job_id: The container ID to wait for
            poll_interval: Seconds between status checks
            timeout: Maximum seconds to wait (None for no limit)

        Returns:
            Final WorkerTaskResult
        """
        start_time = datetime.now(UTC)

        while True:
            result = await self.get_task_status(job_id)

            if result.status not in {WorkerStatus.SUBMITTED, WorkerStatus.RUNNING}:
                return result

            if timeout:
                elapsed = (datetime.now(UTC) - start_time).total_seconds()
                if elapsed >= timeout:
                    # Timeout reached - cancel and return
                    await self.cancel_task(job_id)
                    task = self._tasks.get(job_id)
                    if task:
                        task.status = WorkerStatus.TIMEOUT
                        task.error_message = f"Task timed out after {timeout}s"
                    return WorkerTaskResult(
                        status=WorkerStatus.TIMEOUT,
                        error_message=f"Task timed out after {timeout}s",
                    )

            await asyncio.sleep(poll_interval)

    def get_task(self, job_id: str) -> DockerTask | None:
        """Get a task by container ID (for inspection in tests)."""
        return self._tasks.get(job_id)

    def get_task_by_task_id(self, task_id: str) -> DockerTask | None:
        """Get a task by its task ID (for inspection in tests)."""
        for task in self._tasks.values():
            if task.task_id == task_id:
                return task
        return None

    def clear(self) -> None:
        """Clear all tracked tasks (for test cleanup)."""
        self._tasks.clear()

    async def cleanup_containers(self) -> int:
        """Stop and remove all containers created by this backend.

        Returns:
            Number of containers cleaned up
        """
        count = 0
        for container_id, task in list(self._tasks.items()):
            if task.status in {WorkerStatus.SUBMITTED, WorkerStatus.RUNNING}:
                await self.cancel_task(container_id)
                count += 1
        return count
