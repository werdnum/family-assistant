"""Kubernetes backend for production deployments.

This backend creates Kubernetes Jobs to run worker tasks,
providing isolation and scalability in production environments.

Uses kubectl CLI via asyncio.subprocess to avoid heavy dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from family_assistant.services.worker_backend import WorkerStatus, WorkerTaskResult

if TYPE_CHECKING:
    from family_assistant.config_models import KubernetesBackendConfig

from family_assistant.config_models import WorkerResourceLimits

logger = logging.getLogger(__name__)

# Default max turns for the AI agent
DEFAULT_MAX_TURNS = 50


@dataclass
class KubernetesTask:
    """Represents a Kubernetes Job running a worker task."""

    task_id: str
    job_name: str
    namespace: str
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


class KubernetesBackend:
    """Kubernetes backend for running worker tasks as Jobs.

    Uses `kubectl` CLI via asyncio.subprocess for Kubernetes operations.
    This avoids adding a heavy dependency (kubernetes-client) while
    providing full async functionality.

    Example usage:
        backend = KubernetesBackend(config)
        job_id = await backend.spawn_task(
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="https://app.example.com/webhook/event",
            model="claude",
            timeout_minutes=30,
        )
    """

    def __init__(
        self,
        config: KubernetesBackendConfig | None = None,
        workspace_pvc_name: str = "workspace",
    ) -> None:
        """Initialize the Kubernetes backend.

        Args:
            config: Kubernetes-specific configuration. If None, uses defaults.
            workspace_pvc_name: Name of the PVC for workspace volume.
        """
        self._config = config
        self._workspace_pvc_name = workspace_pvc_name
        self._tasks: dict[str, KubernetesTask] = {}

    @property
    def namespace(self) -> str:
        """Get the Kubernetes namespace to use."""
        if self._config:
            return self._config.namespace
        return "ml-bot"

    @property
    def image(self) -> str:
        """Get the container image to use."""
        if self._config:
            return self._config.ai_coder_image
        return "ghcr.io/werdnum/ai-coding-base:latest"

    @property
    def service_account(self) -> str:
        """Get the service account to use."""
        if self._config:
            return self._config.service_account
        return "ai-worker"

    @property
    def runtime_class(self) -> str:
        """Get the runtime class for sandboxing."""
        if self._config:
            return self._config.runtime_class
        return "gvisor"

    @property
    def job_ttl_seconds(self) -> int:
        """Get the TTL for completed jobs."""
        if self._config:
            return self._config.job_ttl_seconds
        return 3600

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
        callback_token: str | None = None,
    ) -> str:
        """Spawn a worker task as a Kubernetes Job.

        Args:
            task_id: Unique identifier for this task
            prompt_path: Path to the prompt file (relative to workspace)
            output_dir: Path for output files (relative to workspace)
            webhook_url: URL to POST completion webhook
            model: AI model to use ("claude" or "gemini")
            timeout_minutes: Maximum execution time
            context_paths: Additional paths to include as context (unused for now)
            callback_token: Security token for webhook verification

        Returns:
            Job name that can be used to track the task
        """
        job_name = f"worker-{task_id}"

        # Build job manifest
        job_manifest = self._build_job_manifest(
            job_name=job_name,
            task_id=task_id,
            prompt_path=prompt_path,
            output_dir=output_dir,
            webhook_url=webhook_url,
            model=model,
            timeout_minutes=timeout_minutes,
            callback_token=callback_token,
        )

        logger.info(f"Creating Kubernetes Job {job_name} for task {task_id}")
        logger.debug(f"Job manifest: {json.dumps(job_manifest, indent=2)}")

        # Apply the job manifest
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "apply",
            "-f",
            "-",
            "-n",
            self.namespace,
            "-o",
            "json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        manifest_json = json.dumps(job_manifest).encode()
        stdout, stderr = await proc.communicate(input=manifest_json)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() if stderr else "Unknown error"
            logger.error(f"Failed to create job {job_name}: {error_msg}")
            raise RuntimeError(f"Failed to create Kubernetes Job: {error_msg}")

        # Track the task
        task = KubernetesTask(
            task_id=task_id,
            job_name=job_name,
            namespace=self.namespace,
            prompt_path=prompt_path,
            output_dir=output_dir,
            model=model,
            timeout_minutes=timeout_minutes,
            status=WorkerStatus.SUBMITTED,
        )
        self._tasks[job_name] = task

        logger.info(f"Created Kubernetes Job {job_name} for task {task_id}")
        return job_name

    def _build_job_manifest(
        self,
        job_name: str,
        task_id: str,
        prompt_path: str,
        output_dir: str,
        webhook_url: str,
        model: str,
        timeout_minutes: int,
        callback_token: str | None = None,
    ) -> dict:
        """Build the Kubernetes Job manifest.

        Returns a dictionary that can be serialized to JSON/YAML for kubectl apply.
        """
        # Extract the filename from prompt_path (e.g., "tasks/{task_id}/prompt.md" -> "prompt.md")
        # The workspace volume uses subPath to isolate each task, so paths are relative to /task
        prompt_filename = prompt_path.rsplit("/", maxsplit=1)[-1]  # "prompt.md"
        output_dirname = output_dir.rsplit("/", maxsplit=1)[-1]  # "output"

        # Environment variables for the task runner
        # Paths are relative to /task since we mount only the task's directory
        # ast-grep-ignore: no-dict-any - Kubernetes env vars have mixed structures (value vs valueFrom)
        env_vars: list[dict[str, Any]] = [
            {"name": "TASK_ID", "value": task_id},
            {"name": "TASK_INPUT", "value": f"/task/{prompt_filename}"},
            {"name": "TASK_OUTPUT_DIR", "value": f"/task/{output_dirname}"},
            {"name": "TASK_WEBHOOK_URL", "value": webhook_url},
            {"name": "AI_AGENT", "value": model},
            {"name": "MAX_TURNS", "value": str(DEFAULT_MAX_TURNS)},
            {"name": "TASK_TIMEOUT_MINUTES", "value": str(timeout_minutes)},
        ]

        # Add callback token for webhook verification
        if callback_token:
            env_vars.append({"name": "TASK_CALLBACK_TOKEN", "value": callback_token})

        # Build envFrom to inject all API keys from the secret
        # ast-grep-ignore: no-dict-any - Kubernetes envFrom has nested optional fields
        env_from: list[dict[str, Any]] = []
        if self._config and self._config.api_keys_secret:
            env_from.append({
                "secretRef": {
                    "name": self._config.api_keys_secret,
                    "optional": True,
                }
            })

        # Volume mounts with isolation - mount only the task's directory at /task
        # ast-grep-ignore: no-dict-any - Kubernetes volume mounts have mixed types (str vs bool for readOnly)
        volume_mounts: list[dict[str, Any]] = [
            {
                "name": "workspace",
                "mountPath": "/task",
                "subPath": f"tasks/{task_id}",
            }
        ]

        # Volumes
        # ast-grep-ignore: no-dict-any - Kubernetes volume specs have mixed types
        volumes: list[dict[str, Any]] = [
            {
                "name": "workspace",
                "persistentVolumeClaim": {"claimName": self._workspace_pvc_name},
            }
        ]

        # Add optional config volume mounts (for full ~/.claude or ~/.gemini directories)
        if model == "claude" and self._config and self._config.claude_config_volume:
            volume_mounts.append({
                "name": "claude-config",
                "mountPath": "/home/coder/.claude",
                "readOnly": True,
            })
            volumes.append({
                **self._config.claude_config_volume,
                "name": "claude-config",
            })
        elif model == "gemini" and self._config and self._config.gemini_config_volume:
            volume_mounts.append({
                "name": "gemini-config",
                "mountPath": "/home/coder/.gemini",
                "readOnly": True,
            })
            volumes.append({
                **self._config.gemini_config_volume,
                "name": "gemini-config",
            })

        # Build the Job manifest
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.namespace,
                "labels": {
                    "app": "ai-worker",
                    "task-id": task_id,
                    "model": model,
                },
            },
            "spec": {
                "ttlSecondsAfterFinished": self.job_ttl_seconds,
                "backoffLimit": 0,  # No retries
                "activeDeadlineSeconds": timeout_minutes * 60,
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "ai-worker",
                            "task-id": task_id,
                        },
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": self.service_account,
                        "runtimeClassName": self.runtime_class,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "runAsGroup": 1000,
                            "fsGroup": 1000,
                        },
                        "containers": [
                            {
                                "name": "worker",
                                "image": self.image,
                                "command": ["run-task"],
                                "env": env_vars,
                                "envFrom": env_from,
                                "volumeMounts": volume_mounts,
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": False,
                                },
                                "resources": {
                                    "requests": {
                                        "memory": self.resources.memory_request,
                                        "cpu": self.resources.cpu_request,
                                    },
                                    "limits": {
                                        "memory": self.resources.memory_limit,
                                        "cpu": self.resources.cpu_limit,
                                    },
                                },
                            }
                        ],
                        "volumes": volumes,
                    },
                },
            },
        }

        return manifest

    async def get_task_status(self, job_id: str) -> WorkerTaskResult:
        """Get the status of a Kubernetes Job.

        Args:
            job_id: The job name returned by spawn_task

        Returns:
            WorkerTaskResult with current status
        """
        task = self._tasks.get(job_id)
        if not task:
            return WorkerTaskResult(
                status=WorkerStatus.FAILED,
                error_message=f"Task not found: {job_id}",
            )

        # Get job status from Kubernetes
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "get",
            "job",
            job_id,
            "-n",
            self.namespace,
            "-o",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            # Job not found - check if we have a cached status
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
                error_message="Job not found in cluster",
            )

        job_data = json.loads(stdout.decode())
        status = job_data.get("status", {})

        # Parse job status
        if status.get("succeeded", 0) > 0:
            task.status = WorkerStatus.SUCCESS
            task.completed_at = datetime.now(UTC)
            task.exit_code = 0
            return WorkerTaskResult(status=WorkerStatus.SUCCESS, exit_code=0)

        if status.get("failed", 0) > 0:
            task.status = WorkerStatus.FAILED
            task.completed_at = datetime.now(UTC)
            # Don't set exit_code here - let webhook provide actual value

            # Check conditions for failure details
            conditions = status.get("conditions", [])
            failure_reason = None
            for condition in conditions:
                if condition.get("type") == "Failed":
                    reason = condition.get("reason", "")
                    message = condition.get("message", "")
                    if reason == "DeadlineExceeded":
                        task.status = WorkerStatus.TIMEOUT
                        task.error_message = "Job exceeded deadline"
                        return WorkerTaskResult(
                            status=WorkerStatus.TIMEOUT,
                            error_message="Job exceeded deadline",
                        )
                    # Capture failure reason for error message
                    failure_reason = f"{reason}: {message}" if message else reason

            task.error_message = failure_reason or "Job failed (details via webhook)"
            return WorkerTaskResult(
                status=WorkerStatus.FAILED,
                error_message=task.error_message,
            )

        if status.get("active", 0) > 0:
            if task.started_at is None:
                task.started_at = datetime.now(UTC)
            task.status = WorkerStatus.RUNNING
            return WorkerTaskResult(status=WorkerStatus.RUNNING)

        # Job exists but no active/succeeded/failed - still pending
        return WorkerTaskResult(status=WorkerStatus.SUBMITTED)

    async def cancel_task(self, job_id: str) -> bool:
        """Cancel a running Kubernetes Job.

        Args:
            job_id: The job name to cancel

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

        logger.info(f"Cancelling Kubernetes Job {job_id}")

        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "delete",
            "job",
            job_id,
            "-n",
            self.namespace,
            "--grace-period=30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() if stderr else "Unknown error"
            logger.error(f"Failed to delete job {job_id}: {error_msg}")
            return False

        task.status = WorkerStatus.CANCELLED
        task.completed_at = datetime.now(UTC)
        logger.info(f"Cancelled Kubernetes Job {job_id}")
        return True

    async def get_job_logs(self, job_id: str, tail: int = 100) -> str | None:
        """Get logs from a job's pod.

        Args:
            job_id: The job name
            tail: Number of lines to retrieve from the end

        Returns:
            Log output or None if not available
        """
        # First, find the pod for this job
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "get",
            "pods",
            "-n",
            self.namespace,
            "-l",
            f"job-name={job_id}",
            "-o",
            "jsonpath={.items[0].metadata.name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await proc.communicate()

        if proc.returncode != 0 or not stdout:
            return None

        pod_name = stdout.decode().strip()
        if not pod_name:
            return None

        # Get logs from the pod
        proc = await asyncio.create_subprocess_exec(
            "kubectl",
            "logs",
            pod_name,
            "-n",
            self.namespace,
            f"--tail={tail}",
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
        poll_interval: float = 5.0,
        timeout: float | None = None,
    ) -> WorkerTaskResult:
        """Wait for a job to complete.

        Args:
            job_id: The job name to wait for
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

    def get_task(self, job_id: str) -> KubernetesTask | None:
        """Get a task by job name (for inspection in tests)."""
        return self._tasks.get(job_id)

    def get_task_by_task_id(self, task_id: str) -> KubernetesTask | None:
        """Get a task by its task ID (for inspection in tests)."""
        for task in self._tasks.values():
            if task.task_id == task_id:
                return task
        return None

    def clear(self) -> None:
        """Clear all tracked tasks (for test cleanup)."""
        self._tasks.clear()

    async def cleanup_jobs(self) -> int:
        """Delete all jobs created by this backend.

        Returns:
            Number of jobs cleaned up
        """
        count = 0
        for job_name, task in list(self._tasks.items()):
            if task.status in {WorkerStatus.SUBMITTED, WorkerStatus.RUNNING}:
                await self.cancel_task(job_name)
                count += 1
        return count
