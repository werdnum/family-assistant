"""Kubernetes backend for production deployments.

This backend creates Kubernetes Jobs to run worker tasks,
providing isolation and scalability in production environments.

Uses the kubernetes-asyncio client library for native async Kubernetes API access.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import config as kube_config
from kubernetes_asyncio.client import (
    ApiClient,
    BatchV1Api,
    Configuration,
    CoreV1Api,
    V1Capabilities,
    V1ConfigMapVolumeSource,
    V1Container,
    V1EnvFromSource,
    V1EnvVar,
    V1Job,
    V1JobSpec,
    V1ObjectMeta,
    V1PersistentVolumeClaimVolumeSource,
    V1PodSecurityContext,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
    V1SecretEnvSource,
    V1SecretVolumeSource,
    V1SecurityContext,
    V1Volume,
    V1VolumeMount,
)
from kubernetes_asyncio.client.exceptions import ApiException

from family_assistant.config_models import WorkerResourceLimits
from family_assistant.services.worker_backend import WorkerStatus, WorkerTaskResult

if TYPE_CHECKING:
    from family_assistant.config_models import KubernetesBackendConfig

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


async def _load_kube_config(
    kubeconfig_path: str | None = None,
) -> Configuration:
    """Load Kubernetes configuration, trying in-cluster first, then kubeconfig.

    Returns a Configuration object with disable_ssl_x509_strict=True to work around
    k3s (and other distributions) issuing certificates without the Authority Key
    Identifier extension, which OpenSSL 3.4+ rejects under strict verification.

    Args:
        kubeconfig_path: Optional explicit path to kubeconfig file.
    """
    config = Configuration()
    config.disable_ssl_x509_strict = True
    try:
        kube_config.load_incluster_config(client_configuration=config)
        logger.debug("Loaded in-cluster Kubernetes config")
    except kube_config.ConfigException:
        await kube_config.load_kube_config(
            config_file=kubeconfig_path, client_configuration=config
        )
        logger.debug("Loaded kubeconfig file")
    return config


# ast-grep-ignore: no-dict-any - Raw Kubernetes YAML volume config from user settings
def _config_dict_to_volume(config_dict: dict[str, Any], name: str) -> V1Volume:
    """Convert a raw Kubernetes volume config dict to a V1Volume.

    The config dict uses camelCase keys matching Kubernetes YAML format.
    Any "name" key in the config dict is ignored; the provided name is used.

    Supported volume source types: persistentVolumeClaim, configMap, secret.
    """
    if "persistentVolumeClaim" in config_dict:
        pvc = config_dict["persistentVolumeClaim"]
        if "claimName" not in pvc:
            msg = (
                f"Missing required 'claimName' in persistentVolumeClaim volume '{name}'"
            )
            raise ValueError(msg)
        return V1Volume(
            name=name,
            persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                claim_name=pvc["claimName"],
                read_only=pvc.get("readOnly"),
            ),
        )
    if "configMap" in config_dict:
        cm = config_dict["configMap"]
        if "name" not in cm:
            msg = f"Missing required 'name' in configMap volume '{name}'"
            raise ValueError(msg)
        return V1Volume(
            name=name,
            config_map=V1ConfigMapVolumeSource(
                name=cm["name"],
            ),
        )
    if "secret" in config_dict:
        secret = config_dict["secret"]
        if "secretName" not in secret:
            msg = f"Missing required 'secretName' in secret volume '{name}'"
            raise ValueError(msg)
        return V1Volume(
            name=name,
            secret=V1SecretVolumeSource(
                secret_name=secret["secretName"],
            ),
        )
    volume_keys = [k for k in config_dict if k != "name"]
    msg = (
        f"Unsupported config volume source type: {volume_keys}. "
        f"Supported: persistentVolumeClaim, configMap, secret"
    )
    raise ValueError(msg)


class KubernetesBackend:
    """Kubernetes backend for running worker tasks as Jobs.

    Uses the kubernetes-asyncio client library for native async Kubernetes
    API access, providing full async functionality without subprocess calls.

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
        self._config_loaded = False
        self._config_lock = asyncio.Lock()
        self._kube_config: Configuration | None = None

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

    async def _ensure_config_loaded(self) -> None:
        """Ensure Kubernetes configuration is loaded (once)."""
        if not self._config_loaded:
            async with self._config_lock:
                if not self._config_loaded:
                    kubeconfig_path = (
                        self._config.kubeconfig_path if self._config else None
                    )
                    self._kube_config = await _load_kube_config(kubeconfig_path)
                    self._config_loaded = True

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

        await self._ensure_config_loaded()
        async with ApiClient(configuration=self._kube_config) as api_client:
            batch_api = BatchV1Api(api_client)
            try:
                await batch_api.create_namespaced_job(
                    namespace=self.namespace,
                    body=job_manifest,
                )
            except Exception as e:
                logger.error(f"Failed to create job {job_name}: {e}")
                raise RuntimeError(f"Failed to create Kubernetes Job: {e}") from e

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
    ) -> V1Job:
        """Build the Kubernetes Job manifest.

        Returns a V1Job object for the kubernetes-asyncio API.
        """
        prompt_filename = prompt_path.rsplit("/", maxsplit=1)[-1]
        output_dirname = output_dir.rsplit("/", maxsplit=1)[-1]

        env_vars = [
            V1EnvVar(name="TASK_ID", value=task_id),
            V1EnvVar(name="TASK_INPUT", value=f"/task/{prompt_filename}"),
            V1EnvVar(name="TASK_OUTPUT_DIR", value=f"/task/{output_dirname}"),
            V1EnvVar(name="TASK_WEBHOOK_URL", value=webhook_url),
            V1EnvVar(name="AI_AGENT", value=model),
            V1EnvVar(name="MAX_TURNS", value=str(DEFAULT_MAX_TURNS)),
            V1EnvVar(name="TASK_TIMEOUT_MINUTES", value=str(timeout_minutes)),
        ]

        if callback_token:
            env_vars.append(V1EnvVar(name="TASK_CALLBACK_TOKEN", value=callback_token))

        env_from: list[V1EnvFromSource] = []
        if self._config and self._config.api_keys_secret:
            env_from.append(
                V1EnvFromSource(
                    secret_ref=V1SecretEnvSource(
                        name=self._config.api_keys_secret,
                        optional=True,
                    )
                )
            )

        volume_mounts = [
            V1VolumeMount(
                name="workspace",
                mount_path="/task",
                sub_path=f"tasks/{task_id}",
            )
        ]

        volumes: list[V1Volume] = [
            V1Volume(
                name="workspace",
                persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                    claim_name=self._workspace_pvc_name,
                ),
            )
        ]

        if model == "claude" and self._config and self._config.claude_config_volume:
            volume_mounts.append(
                V1VolumeMount(
                    name="claude-config",
                    mount_path="/home/coder/.claude",
                    read_only=True,
                )
            )
            volumes.append(
                _config_dict_to_volume(
                    self._config.claude_config_volume, "claude-config"
                )
            )
        elif model == "gemini" and self._config and self._config.gemini_config_volume:
            volume_mounts.append(
                V1VolumeMount(
                    name="gemini-config",
                    mount_path="/home/coder/.gemini",
                    read_only=True,
                )
            )
            volumes.append(
                _config_dict_to_volume(
                    self._config.gemini_config_volume, "gemini-config"
                )
            )

        return V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=V1ObjectMeta(
                name=job_name,
                namespace=self.namespace,
                labels={
                    "app": "ai-worker",
                    "task-id": task_id,
                    "model": model,
                },
            ),
            spec=V1JobSpec(
                ttl_seconds_after_finished=self.job_ttl_seconds,
                backoff_limit=0,
                active_deadline_seconds=timeout_minutes * 60,
                template=V1PodTemplateSpec(
                    metadata=V1ObjectMeta(
                        labels={
                            "app": "ai-worker",
                            "task-id": task_id,
                        },
                    ),
                    spec=V1PodSpec(
                        restart_policy="Never",
                        service_account_name=self.service_account,
                        runtime_class_name=self.runtime_class,
                        security_context=V1PodSecurityContext(
                            run_as_non_root=True,
                            run_as_user=1000,
                            run_as_group=1000,
                            fs_group=1000,
                        ),
                        containers=[
                            V1Container(
                                name="worker",
                                image=self.image,
                                command=["run-task"],
                                env=env_vars,
                                env_from=env_from,
                                volume_mounts=volume_mounts,
                                security_context=V1SecurityContext(
                                    allow_privilege_escalation=False,
                                    capabilities=V1Capabilities(drop=["ALL"]),
                                    read_only_root_filesystem=False,
                                ),
                                resources=V1ResourceRequirements(
                                    requests={
                                        "memory": self.resources.memory_request,
                                        "cpu": self.resources.cpu_request,
                                    },
                                    limits={
                                        "memory": self.resources.memory_limit,
                                        "cpu": self.resources.cpu_limit,
                                    },
                                ),
                            )
                        ],
                        volumes=volumes,
                    ),
                ),
            ),
        )

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

        await self._ensure_config_loaded()
        async with ApiClient(configuration=self._kube_config) as api_client:
            batch_api = BatchV1Api(api_client)
            try:
                job = await batch_api.read_namespaced_job(
                    name=job_id,
                    namespace=self.namespace,
                )
            except ApiException as e:
                if e.status != 404:
                    raise
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

        status = job.status

        # Parse job status
        if (status.succeeded or 0) > 0:
            task.status = WorkerStatus.SUCCESS
            task.completed_at = datetime.now(UTC)
            task.exit_code = 0
            return WorkerTaskResult(status=WorkerStatus.SUCCESS, exit_code=0)

        if (status.failed or 0) > 0:
            task.status = WorkerStatus.FAILED
            task.completed_at = datetime.now(UTC)

            # Check conditions for failure details
            conditions = status.conditions or []
            failure_reason = None
            for condition in conditions:
                if condition.type == "Failed":
                    reason = condition.reason or ""
                    message = condition.message or ""
                    if reason == "DeadlineExceeded":
                        task.status = WorkerStatus.TIMEOUT
                        task.error_message = "Job exceeded deadline"
                        return WorkerTaskResult(
                            status=WorkerStatus.TIMEOUT,
                            error_message="Job exceeded deadline",
                        )
                    failure_reason = f"{reason}: {message}" if message else reason

            task.error_message = failure_reason or "Job failed (details via webhook)"
            return WorkerTaskResult(
                status=WorkerStatus.FAILED,
                error_message=task.error_message,
            )

        if (status.active or 0) > 0:
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

        await self._ensure_config_loaded()
        async with ApiClient(configuration=self._kube_config) as api_client:
            batch_api = BatchV1Api(api_client)
            try:
                await batch_api.delete_namespaced_job(
                    name=job_id,
                    namespace=self.namespace,
                    propagation_policy="Foreground",
                )
            except Exception as e:
                logger.error(f"Failed to delete job {job_id}: {e}")
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
        await self._ensure_config_loaded()
        async with ApiClient(configuration=self._kube_config) as api_client:
            core_api = CoreV1Api(api_client)
            try:
                # Find the pod for this job
                pod_list = await core_api.list_namespaced_pod(
                    namespace=self.namespace,
                    label_selector=f"job-name={job_id}",
                )

                if not pod_list.items:
                    return None

                pod_name = pod_list.items[0].metadata.name
                if not pod_name:
                    return None

                # Get logs from the pod
                return await core_api.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=self.namespace,
                    tail_lines=tail,
                )
            except ApiException as e:
                if e.status in {403, 500, 502, 503}:
                    raise
                logger.warning(f"Failed to fetch logs for job {job_id}", exc_info=True)
                return None

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
