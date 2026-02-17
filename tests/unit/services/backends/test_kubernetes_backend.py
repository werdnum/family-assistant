"""Tests for the Kubernetes backend."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import cloudcoil.models.kubernetes.core.v1 as k8s
import pytest

from family_assistant.config_models import KubernetesBackendConfig
from family_assistant.services.backends.kubernetes import (
    KubernetesBackend,
    KubernetesTask,
)
from family_assistant.services.worker_backend import WorkerStatus


@pytest.fixture
def kubernetes_config() -> KubernetesBackendConfig:
    """Create a test Kubernetes config."""
    return KubernetesBackendConfig(
        namespace="test-namespace",
        ai_coder_image="test-image:latest",
        service_account="test-sa",
        runtime_class="test-runtime",
        job_ttl_seconds=7200,
        workspace_pvc_name="test-pvc",
        api_keys_secret="test-api-keys",
        claude_config_volume=k8s.Volume(
            name="claude-config",
            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                claim_name="claude-config-pvc",
            ),
        ),
        gemini_config_volume=k8s.Volume(
            name="gemini-config",
            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                claim_name="gemini-config-pvc",
            ),
        ),
    )


@pytest.fixture
def backend(kubernetes_config: KubernetesBackendConfig) -> KubernetesBackend:
    """Create a KubernetesBackend instance for testing."""
    backend = KubernetesBackend(
        config=kubernetes_config,
    )
    # Skip actual kube config loading in tests
    backend._config_loaded = True
    return backend


def _mock_api_client() -> MagicMock:
    """Create a mock ApiClient that supports async context manager."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


class TestKubernetesBackendInit:
    """Tests for KubernetesBackend initialization."""

    def test_init_with_config(self, kubernetes_config: KubernetesBackendConfig) -> None:
        """Test backend initializes with provided config."""
        backend = KubernetesBackend(config=kubernetes_config)
        assert backend.namespace == "test-namespace"
        assert backend.image == "test-image:latest"
        assert backend.service_account == "test-sa"
        assert backend.runtime_class == "test-runtime"
        assert backend.job_ttl_seconds == 7200

    def test_init_without_config(self) -> None:
        """Test backend uses defaults when no config provided."""
        backend = KubernetesBackend()
        assert backend.namespace == "ml-bot"
        assert backend.image == "ghcr.io/werdnum/ai-coding-base:latest"
        assert backend.service_account == "ai-worker"
        assert backend.runtime_class == "gvisor"
        assert backend.job_ttl_seconds == 3600


class TestKubernetesBackendSpawnTask:
    """Tests for KubernetesBackend.spawn_task()."""

    @pytest.mark.asyncio
    async def test_spawn_task_success(self, backend: KubernetesBackend) -> None:
        """Test successful task spawn."""
        mock_client = _mock_api_client()
        mock_batch_api = AsyncMock()
        mock_batch_api.create_namespaced_job = AsyncMock()

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.BatchV1Api",
                return_value=mock_batch_api,
            ),
        ):
            job_id = await backend.spawn_task(
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

            assert job_id == "ai-worker-task-123"
            assert job_id in backend._tasks
            task = backend._tasks[job_id]
            assert task.task_id == "task-123"
            assert task.status == WorkerStatus.SUBMITTED
            assert task.model == "claude"
            mock_batch_api.create_namespaced_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_task_failure(self, backend: KubernetesBackend) -> None:
        """Test spawn task handles API failure."""
        mock_client = _mock_api_client()
        mock_batch_api = AsyncMock()
        mock_batch_api.create_namespaced_job = AsyncMock(
            side_effect=Exception("forbidden")
        )

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.BatchV1Api",
                return_value=mock_batch_api,
            ),
            pytest.raises(RuntimeError, match="Failed to create Kubernetes Job"),
        ):
            await backend.spawn_task(
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )


class TestKubernetesBackendBuildJobManifest:
    """Tests for KubernetesBackend._build_job_manifest()."""

    def test_build_manifest_basic(self, backend: KubernetesBackend) -> None:
        """Test building basic job manifest."""
        manifest = backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        assert manifest.api_version == "batch/v1"
        assert manifest.kind == "Job"
        assert manifest.metadata.name == "ai-worker-task-123"
        assert manifest.metadata.namespace == "test-namespace"
        assert manifest.metadata.labels["task-id"] == "task-123"
        assert manifest.metadata.labels["model"] == "claude"

        spec = manifest.spec
        assert spec.ttl_seconds_after_finished == 7200
        assert spec.backoff_limit == 0
        assert spec.active_deadline_seconds == 30 * 60

        pod_spec = spec.template.spec
        assert pod_spec.service_account_name == "test-sa"
        assert pod_spec.runtime_class_name == "test-runtime"
        assert pod_spec.restart_policy == "Never"

        container = pod_spec.containers[0]
        assert container.name == "worker"
        assert container.image == "test-image:latest"
        assert container.command is None
        assert container.args == ["sh", "-c", 'run-task < "$TASK_INPUT"']

    def test_build_manifest_env_vars(self, backend: KubernetesBackend) -> None:
        """Test job manifest includes correct environment variables."""
        manifest = backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=45,
        )

        container = manifest.spec.template.spec.containers[0]
        env_vars = {e.name: e for e in container.env}

        assert env_vars["TASK_ID"].value == "task-123"
        # Paths are relative to /task since we mount only the task's directory
        assert env_vars["TASK_INPUT"].value == "/task/prompt.md"
        assert env_vars["TASK_OUTPUT_DIR"].value == "/task/output"
        assert (
            env_vars["TASK_WEBHOOK_URL"].value == "http://localhost:8000/webhook/event"
        )
        assert env_vars["AI_AGENT"].value == "claude"
        assert env_vars["MAX_TURNS"].value == "50"
        assert env_vars["TASK_TIMEOUT_MINUTES"].value == "45"

        # Check envFrom includes the API keys secret
        env_from = container.env_from
        assert len(env_from) == 1
        assert env_from[0].secret_ref.name == "test-api-keys"

    def test_build_manifest_gemini_model(self, backend: KubernetesBackend) -> None:
        """Test job manifest for gemini model uses gemini config volume."""
        manifest = backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="gemini",
            timeout_minutes=30,
        )

        container = manifest.spec.template.spec.containers[0]
        env_vars = {e.name: e for e in container.env}
        assert env_vars["AI_AGENT"].value == "gemini"

        # Check envFrom is still present (same secret for all API keys)
        env_from = container.env_from
        assert len(env_from) == 1
        assert env_from[0].secret_ref.name == "test-api-keys"

        # Check gemini config volume is mounted instead of claude
        pod_spec = manifest.spec.template.spec
        volume_mounts = {v.name: v for v in container.volume_mounts}
        assert "gemini-config" in volume_mounts
        assert volume_mounts["gemini-config"].mount_path == "/home/coder/.gemini"
        assert "claude-config" not in volume_mounts

        volumes = {v.name: v for v in pod_spec.volumes}
        assert (
            volumes["gemini-config"].persistent_volume_claim.claim_name
            == "gemini-config-pvc"
        )

    def test_build_manifest_security_context(self, backend: KubernetesBackend) -> None:
        """Test job manifest includes security context."""
        manifest = backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        pod_spec = manifest.spec.template.spec
        security_context = pod_spec.security_context
        assert security_context.run_as_non_root is True
        assert security_context.run_as_user == 1000
        assert security_context.run_as_group == 1000
        assert security_context.fs_group == 1000

        container_security = pod_spec.containers[0].security_context
        assert container_security.allow_privilege_escalation is False
        assert container_security.capabilities.drop == ["ALL"]

    def test_build_manifest_custom_uid_gid(self) -> None:
        """Test job manifest uses custom uid/gid from config."""
        config = KubernetesBackendConfig(
            namespace="test-namespace",
            ai_coder_image="test-image:latest",
            service_account="test-sa",
            run_as_user=2000,
            run_as_group=2001,
            fs_group=2002,
        )
        custom_backend = KubernetesBackend(config=config)
        custom_backend._config_loaded = True

        manifest = custom_backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        security_context = manifest.spec.template.spec.security_context
        assert security_context.run_as_non_root is True
        assert security_context.run_as_user == 2000
        assert security_context.run_as_group == 2001
        assert security_context.fs_group == 2002

    def test_build_manifest_none_uid_gid(self) -> None:
        """Test job manifest omits uid/gid when set to None."""
        config = KubernetesBackendConfig(
            namespace="test-namespace",
            ai_coder_image="test-image:latest",
            service_account="test-sa",
            run_as_user=None,
            run_as_group=None,
            fs_group=None,
        )
        custom_backend = KubernetesBackend(config=config)
        custom_backend._config_loaded = True

        manifest = custom_backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        security_context = manifest.spec.template.spec.security_context
        assert security_context.run_as_non_root is False
        assert security_context.run_as_user is None
        assert security_context.run_as_group is None
        assert security_context.fs_group is None

    def test_build_manifest_volume_mounts(self, backend: KubernetesBackend) -> None:
        """Test job manifest includes volume mounts."""
        manifest = backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        pod_spec = manifest.spec.template.spec
        container = pod_spec.containers[0]
        volume_mounts = {v.name: v for v in container.volume_mounts}

        assert "workspace" in volume_mounts
        # Volume is mounted at /task with subPath for task isolation
        assert volume_mounts["workspace"].mount_path == "/task"
        assert volume_mounts["workspace"].sub_path == "tasks/task-123"

        # Claude config mount from PVC
        assert "claude-config" in volume_mounts
        assert volume_mounts["claude-config"].mount_path == "/home/coder/.claude"
        assert volume_mounts["claude-config"].read_only is True

        volumes = {v.name: v for v in pod_spec.volumes}
        assert volumes["workspace"].persistent_volume_claim.claim_name == "test-pvc"
        assert (
            volumes["claude-config"].persistent_volume_claim.claim_name
            == "claude-config-pvc"
        )

    def test_config_volume_uses_configured_name(self) -> None:
        """Test that the volume name from config is used in the manifest."""
        config = KubernetesBackendConfig(
            namespace="test-namespace",
            ai_coder_image="test-image:latest",
            service_account="test-sa",
            api_keys_secret="test-api-keys",
            claude_config_volume=k8s.Volume(
                name="my-claude-cfg",
                persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                    claim_name="claude-config-pvc",
                ),
            ),
        )
        backend = KubernetesBackend(config=config)
        backend._config_loaded = True

        manifest = backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        pod_spec = manifest.spec.template.spec
        volumes = {v.name: v for v in pod_spec.volumes}
        assert "my-claude-cfg" in volumes
        volume_mounts = {v.name: v for v in pod_spec.containers[0].volume_mounts}
        assert "my-claude-cfg" in volume_mounts
        assert volume_mounts["my-claude-cfg"].mount_path == "/home/coder/.claude"

    def test_extra_volumes_and_mounts(self) -> None:
        """Test extra volumes and volume mounts appear in the manifest."""
        config = KubernetesBackendConfig(
            namespace="test-namespace",
            ai_coder_image="test-image:latest",
            service_account="test-sa",
            extra_volumes=[
                k8s.Volume(
                    name="my-secret",
                    secret=k8s.SecretVolumeSource(secret_name="my-secret"),
                ),
                k8s.Volume(
                    name="my-config",
                    config_map=k8s.ConfigMapVolumeSource(name="my-cm"),
                ),
            ],
            extra_volume_mounts=[
                k8s.VolumeMount(
                    name="my-secret", mount_path="/etc/secrets", read_only=True
                ),
                k8s.VolumeMount(name="my-config", mount_path="/etc/config"),
            ],
        )
        backend = KubernetesBackend(config=config)
        backend._config_loaded = True

        manifest = backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        pod_spec = manifest.spec.template.spec
        volumes = {v.name: v for v in pod_spec.volumes}
        assert "my-secret" in volumes
        assert volumes["my-secret"].secret.secret_name == "my-secret"
        assert "my-config" in volumes
        assert volumes["my-config"].config_map.name == "my-cm"

        mounts = {v.name: v for v in pod_spec.containers[0].volume_mounts}
        assert mounts["my-secret"].mount_path == "/etc/secrets"
        assert mounts["my-secret"].read_only is True
        assert mounts["my-config"].mount_path == "/etc/config"

    def test_extra_volume_name_collision_raises(self) -> None:
        """Test that extra volume names colliding with existing volumes raise an error."""
        config = KubernetesBackendConfig(
            namespace="test-namespace",
            ai_coder_image="test-image:latest",
            service_account="test-sa",
            extra_volumes=[
                k8s.Volume(
                    name="workspace",
                    secret=k8s.SecretVolumeSource(secret_name="sneaky"),
                ),
            ],
        )
        backend = KubernetesBackend(config=config)
        backend._config_loaded = True

        with pytest.raises(ValueError, match="conflicts with existing volume"):
            backend._build_job_manifest(
                job_name="ai-worker-task-123",
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

    def test_extra_mount_path_collision_raises(self) -> None:
        """Test that extra volume mount paths colliding with existing mounts raise an error."""
        config = KubernetesBackendConfig(
            namespace="test-namespace",
            ai_coder_image="test-image:latest",
            service_account="test-sa",
            extra_volume_mounts=[
                k8s.VolumeMount(name="workspace", mount_path="/task"),
            ],
        )
        backend = KubernetesBackend(config=config)
        backend._config_loaded = True

        with pytest.raises(ValueError, match="conflicts with existing mount"):
            backend._build_job_manifest(
                job_name="ai-worker-task-123",
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

    def test_extra_mount_references_nonexistent_volume_raises(self) -> None:
        """Test that extra volume mounts referencing non-existent volumes raise an error."""
        config = KubernetesBackendConfig(
            namespace="test-namespace",
            ai_coder_image="test-image:latest",
            service_account="test-sa",
            extra_volume_mounts=[
                k8s.VolumeMount(name="no-such-volume", mount_path="/mnt/missing"),
            ],
        )
        backend = KubernetesBackend(config=config)
        backend._config_loaded = True

        with pytest.raises(ValueError, match="references non-existent volume"):
            backend._build_job_manifest(
                job_name="ai-worker-task-123",
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

    def test_none_extra_volumes_no_change(self, backend: KubernetesBackend) -> None:
        """Test that None extra_volumes doesn't change existing behavior."""
        manifest = backend._build_job_manifest(
            job_name="ai-worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        pod_spec = manifest.spec.template.spec
        volume_names = {v.name for v in pod_spec.volumes}
        assert volume_names == {"workspace", "claude-config"}


class TestKubernetesBackendGetTaskStatus:
    """Tests for KubernetesBackend.get_task_status()."""

    @pytest.mark.asyncio
    async def test_get_status_unknown_task(self, backend: KubernetesBackend) -> None:
        """Test getting status of unknown task."""
        result = await backend.get_task_status("unknown-id")
        assert result.status == WorkerStatus.FAILED
        assert result.error_message is not None
        assert "not found" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_get_status_running(self, backend: KubernetesBackend) -> None:
        """Test getting status of running job."""
        backend._tasks["ai-worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.SUBMITTED,
        )

        mock_client = _mock_api_client()
        mock_job = SimpleNamespace(
            status=SimpleNamespace(
                active=1, succeeded=None, failed=None, conditions=None
            )
        )
        mock_batch_api = AsyncMock()
        mock_batch_api.read_namespaced_job = AsyncMock(return_value=mock_job)

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.BatchV1Api",
                return_value=mock_batch_api,
            ),
        ):
            result = await backend.get_task_status("ai-worker-task-123")
            assert result.status == WorkerStatus.RUNNING

    @pytest.mark.asyncio
    async def test_get_status_success(self, backend: KubernetesBackend) -> None:
        """Test getting status of successful job."""
        backend._tasks["ai-worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        mock_client = _mock_api_client()
        mock_job = SimpleNamespace(
            status=SimpleNamespace(
                succeeded=1, active=None, failed=None, conditions=None
            )
        )
        mock_batch_api = AsyncMock()
        mock_batch_api.read_namespaced_job = AsyncMock(return_value=mock_job)

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.BatchV1Api",
                return_value=mock_batch_api,
            ),
        ):
            result = await backend.get_task_status("ai-worker-task-123")
            assert result.status == WorkerStatus.SUCCESS
            assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_get_status_failed(self, backend: KubernetesBackend) -> None:
        """Test getting status of failed job."""
        backend._tasks["ai-worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        mock_client = _mock_api_client()
        mock_job = SimpleNamespace(
            status=SimpleNamespace(
                succeeded=None, active=None, failed=1, conditions=None
            )
        )
        mock_batch_api = AsyncMock()
        mock_batch_api.read_namespaced_job = AsyncMock(return_value=mock_job)

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.BatchV1Api",
                return_value=mock_batch_api,
            ),
        ):
            result = await backend.get_task_status("ai-worker-task-123")
            assert result.status == WorkerStatus.FAILED
            # exit_code not set here - webhook provides actual value
            assert result.exit_code is None
            assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_get_status_timeout(self, backend: KubernetesBackend) -> None:
        """Test getting status of timed out job."""
        backend._tasks["ai-worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        mock_client = _mock_api_client()
        deadline_condition = SimpleNamespace(
            type="Failed", reason="DeadlineExceeded", message=""
        )
        mock_job = SimpleNamespace(
            status=SimpleNamespace(
                succeeded=None, active=None, failed=1, conditions=[deadline_condition]
            )
        )
        mock_batch_api = AsyncMock()
        mock_batch_api.read_namespaced_job = AsyncMock(return_value=mock_job)

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.BatchV1Api",
                return_value=mock_batch_api,
            ),
        ):
            result = await backend.get_task_status("ai-worker-task-123")
            assert result.status == WorkerStatus.TIMEOUT
            assert result.error_message == "Job exceeded deadline"


class TestKubernetesBackendCancelTask:
    """Tests for KubernetesBackend.cancel_task()."""

    @pytest.mark.asyncio
    async def test_cancel_running_task(self, backend: KubernetesBackend) -> None:
        """Test cancelling a running task."""
        backend._tasks["ai-worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        mock_client = _mock_api_client()
        mock_batch_api = AsyncMock()
        mock_batch_api.delete_namespaced_job = AsyncMock()

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.BatchV1Api",
                return_value=mock_batch_api,
            ),
        ):
            result = await backend.cancel_task("ai-worker-task-123")
            assert result is True
            assert backend._tasks["ai-worker-task-123"].status == WorkerStatus.CANCELLED
            mock_batch_api.delete_namespaced_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_unknown_task(self, backend: KubernetesBackend) -> None:
        """Test cancelling unknown task returns False."""
        result = await backend.cancel_task("unknown-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_already_completed(self, backend: KubernetesBackend) -> None:
        """Test cancelling already completed task returns False."""
        backend._tasks["ai-worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.SUCCESS,
        )

        result = await backend.cancel_task("ai-worker-task-123")
        assert result is False


class TestKubernetesBackendHelperMethods:
    """Tests for KubernetesBackend helper methods."""

    def test_get_task(self, backend: KubernetesBackend) -> None:
        """Test get_task returns task by job name."""
        task = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
        )
        backend._tasks["ai-worker-task-123"] = task

        assert backend.get_task("ai-worker-task-123") == task
        assert backend.get_task("unknown") is None

    def test_get_task_by_task_id(self, backend: KubernetesBackend) -> None:
        """Test get_task_by_task_id returns task by task ID."""
        task = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
        )
        backend._tasks["ai-worker-task-123"] = task

        assert backend.get_task_by_task_id("task-123") == task
        assert backend.get_task_by_task_id("unknown") is None

    def test_clear(self, backend: KubernetesBackend) -> None:
        """Test clear removes all tasks."""
        backend._tasks["ai-worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="ai-worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
        )

        backend.clear()
        assert len(backend._tasks) == 0


class TestKubernetesBackendGetJobLogs:
    """Tests for KubernetesBackend.get_job_logs()."""

    @pytest.mark.asyncio
    async def test_get_job_logs_success(self, backend: KubernetesBackend) -> None:
        """Test getting logs from a job."""
        mock_client = _mock_api_client()

        mock_pod = SimpleNamespace(
            metadata=SimpleNamespace(name="ai-worker-task-123-abc123")
        )
        mock_pod_list = SimpleNamespace(items=[mock_pod])

        mock_core_api = AsyncMock()
        mock_core_api.list_namespaced_pod = AsyncMock(return_value=mock_pod_list)
        mock_core_api.read_namespaced_pod_log = AsyncMock(
            return_value="Log line 1\nLog line 2"
        )

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.CoreV1Api",
                return_value=mock_core_api,
            ),
        ):
            logs = await backend.get_job_logs("ai-worker-task-123")
            assert logs == "Log line 1\nLog line 2"

    @pytest.mark.asyncio
    async def test_get_job_logs_no_pod(self, backend: KubernetesBackend) -> None:
        """Test getting logs when pod not found."""
        mock_client = _mock_api_client()

        mock_pod_list = SimpleNamespace(items=[])
        mock_core_api = AsyncMock()
        mock_core_api.list_namespaced_pod = AsyncMock(return_value=mock_pod_list)

        with (
            patch(
                "family_assistant.services.backends.kubernetes.ApiClient",
                return_value=mock_client,
            ),
            patch(
                "family_assistant.services.backends.kubernetes.CoreV1Api",
                return_value=mock_core_api,
            ),
        ):
            logs = await backend.get_job_logs("ai-worker-task-123")
            assert logs is None
