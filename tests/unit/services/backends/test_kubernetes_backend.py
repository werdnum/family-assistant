"""Tests for the Kubernetes backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
        claude_settings_secret="test-claude-secret",
        gemini_settings_secret="test-gemini-secret",
    )


@pytest.fixture
def backend(kubernetes_config: KubernetesBackendConfig) -> KubernetesBackend:
    """Create a KubernetesBackend instance for testing."""
    return KubernetesBackend(config=kubernetes_config, workspace_pvc_name="test-pvc")


class TestKubernetesBackendInit:
    """Tests for KubernetesBackend initialization."""

    def test_init_with_config(self, kubernetes_config: KubernetesBackendConfig) -> None:
        """Test backend initializes with provided config."""
        backend = KubernetesBackend(
            config=kubernetes_config, workspace_pvc_name="my-pvc"
        )
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
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b'{"metadata": {}}', b"")
            mock_exec.return_value = mock_proc

            job_id = await backend.spawn_task(
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

            assert job_id == "worker-task-123"
            assert job_id in backend._tasks
            task = backend._tasks[job_id]
            assert task.task_id == "task-123"
            assert task.status == WorkerStatus.SUBMITTED
            assert task.model == "claude"

    @pytest.mark.asyncio
    async def test_spawn_task_failure(self, backend: KubernetesBackend) -> None:
        """Test spawn task handles kubectl failure."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate.return_value = (b"", b"Error: forbidden\n")
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError, match="Failed to create Kubernetes Job"):
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
            job_name="worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        assert manifest["apiVersion"] == "batch/v1"
        assert manifest["kind"] == "Job"
        assert manifest["metadata"]["name"] == "worker-task-123"
        assert manifest["metadata"]["namespace"] == "test-namespace"
        assert manifest["metadata"]["labels"]["task-id"] == "task-123"
        assert manifest["metadata"]["labels"]["model"] == "claude"

        spec = manifest["spec"]
        assert spec["ttlSecondsAfterFinished"] == 7200
        assert spec["backoffLimit"] == 0
        assert spec["activeDeadlineSeconds"] == 30 * 60

        pod_spec = spec["template"]["spec"]
        assert pod_spec["serviceAccountName"] == "test-sa"
        assert pod_spec["runtimeClassName"] == "test-runtime"
        assert pod_spec["restartPolicy"] == "Never"

        container = pod_spec["containers"][0]
        assert container["name"] == "worker"
        assert container["image"] == "test-image:latest"
        assert container["command"] == ["run-task"]

    def test_build_manifest_env_vars(self, backend: KubernetesBackend) -> None:
        """Test job manifest includes correct environment variables."""
        manifest = backend._build_job_manifest(
            job_name="worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=45,
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        env_vars = {e["name"]: e for e in container["env"]}

        assert env_vars["TASK_ID"]["value"] == "task-123"
        assert env_vars["TASK_INPUT"]["value"] == "/workspace/tasks/task-123/prompt.md"
        assert (
            env_vars["TASK_OUTPUT_DIR"]["value"] == "/workspace/tasks/task-123/output"
        )
        assert (
            env_vars["TASK_WEBHOOK_URL"]["value"]
            == "http://localhost:8000/webhook/event"
        )
        assert env_vars["AI_AGENT"]["value"] == "claude"
        assert env_vars["MAX_TURNS"]["value"] == "50"
        assert env_vars["TASK_TIMEOUT_MINUTES"]["value"] == "45"

        # Check API key from secret
        assert "ANTHROPIC_API_KEY" in env_vars
        api_key_env = env_vars["ANTHROPIC_API_KEY"]
        assert "valueFrom" in api_key_env
        assert api_key_env["valueFrom"]["secretKeyRef"]["name"] == "test-claude-secret"

    def test_build_manifest_gemini_model(self, backend: KubernetesBackend) -> None:
        """Test job manifest for gemini model."""
        manifest = backend._build_job_manifest(
            job_name="worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="gemini",
            timeout_minutes=30,
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        env_vars = {e["name"]: e for e in container["env"]}

        assert env_vars["AI_AGENT"]["value"] == "gemini"
        assert "GOOGLE_API_KEY" in env_vars
        assert "ANTHROPIC_API_KEY" not in env_vars

    def test_build_manifest_security_context(self, backend: KubernetesBackend) -> None:
        """Test job manifest includes security context."""
        manifest = backend._build_job_manifest(
            job_name="worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        pod_spec = manifest["spec"]["template"]["spec"]
        security_context = pod_spec["securityContext"]
        assert security_context["runAsNonRoot"] is True
        assert security_context["runAsUser"] == 1000
        assert security_context["runAsGroup"] == 1000
        assert security_context["fsGroup"] == 1000

        container_security = pod_spec["containers"][0]["securityContext"]
        assert container_security["allowPrivilegeEscalation"] is False
        assert container_security["capabilities"]["drop"] == ["ALL"]

    def test_build_manifest_volume_mounts(self, backend: KubernetesBackend) -> None:
        """Test job manifest includes volume mounts."""
        manifest = backend._build_job_manifest(
            job_name="worker-task-123",
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        pod_spec = manifest["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        volume_mounts = {v["name"]: v for v in container["volumeMounts"]}

        assert "workspace" in volume_mounts
        assert volume_mounts["workspace"]["mountPath"] == "/workspace"

        # Claude settings mount
        assert "claude-settings" in volume_mounts
        assert volume_mounts["claude-settings"]["mountPath"] == "/home/user/.claude"
        assert volume_mounts["claude-settings"]["readOnly"] is True

        volumes = {v["name"]: v for v in pod_spec["volumes"]}
        assert volumes["workspace"]["persistentVolumeClaim"]["claimName"] == "test-pvc"
        assert (
            volumes["claude-settings"]["secret"]["secretName"] == "test-claude-secret"
        )


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
        backend._tasks["worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.SUBMITTED,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (
                b'{"status": {"active": 1}}',
                b"",
            )
            mock_exec.return_value = mock_proc

            result = await backend.get_task_status("worker-task-123")
            assert result.status == WorkerStatus.RUNNING

    @pytest.mark.asyncio
    async def test_get_status_success(self, backend: KubernetesBackend) -> None:
        """Test getting status of successful job."""
        backend._tasks["worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (
                b'{"status": {"succeeded": 1}}',
                b"",
            )
            mock_exec.return_value = mock_proc

            result = await backend.get_task_status("worker-task-123")
            assert result.status == WorkerStatus.SUCCESS
            assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_get_status_failed(self, backend: KubernetesBackend) -> None:
        """Test getting status of failed job."""
        backend._tasks["worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (
                b'{"status": {"failed": 1}}',
                b"",
            )
            mock_exec.return_value = mock_proc

            result = await backend.get_task_status("worker-task-123")
            assert result.status == WorkerStatus.FAILED
            # exit_code not set here - webhook provides actual value
            assert result.exit_code is None
            assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_get_status_timeout(self, backend: KubernetesBackend) -> None:
        """Test getting status of timed out job."""
        backend._tasks["worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (
                b'{"status": {"failed": 1, "conditions": [{"type": "Failed", "reason": "DeadlineExceeded"}]}}',
                b"",
            )
            mock_exec.return_value = mock_proc

            result = await backend.get_task_status("worker-task-123")
            assert result.status == WorkerStatus.TIMEOUT
            assert result.error_message == "Job exceeded deadline"


class TestKubernetesBackendCancelTask:
    """Tests for KubernetesBackend.cancel_task()."""

    @pytest.mark.asyncio
    async def test_cancel_running_task(self, backend: KubernetesBackend) -> None:
        """Test cancelling a running task."""
        backend._tasks["worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"", b"")
            mock_exec.return_value = mock_proc

            result = await backend.cancel_task("worker-task-123")
            assert result is True
            assert backend._tasks["worker-task-123"].status == WorkerStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_unknown_task(self, backend: KubernetesBackend) -> None:
        """Test cancelling unknown task returns False."""
        result = await backend.cancel_task("unknown-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_already_completed(self, backend: KubernetesBackend) -> None:
        """Test cancelling already completed task returns False."""
        backend._tasks["worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.SUCCESS,
        )

        result = await backend.cancel_task("worker-task-123")
        assert result is False


class TestKubernetesBackendHelperMethods:
    """Tests for KubernetesBackend helper methods."""

    def test_get_task(self, backend: KubernetesBackend) -> None:
        """Test get_task returns task by job name."""
        task = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
        )
        backend._tasks["worker-task-123"] = task

        assert backend.get_task("worker-task-123") == task
        assert backend.get_task("unknown") is None

    def test_get_task_by_task_id(self, backend: KubernetesBackend) -> None:
        """Test get_task_by_task_id returns task by task ID."""
        task = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
            namespace="test-namespace",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
        )
        backend._tasks["worker-task-123"] = task

        assert backend.get_task_by_task_id("task-123") == task
        assert backend.get_task_by_task_id("unknown") is None

    def test_clear(self, backend: KubernetesBackend) -> None:
        """Test clear removes all tasks."""
        backend._tasks["worker-task-123"] = KubernetesTask(
            task_id="task-123",
            job_name="worker-task-123",
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
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            # First call gets pod name
            mock_proc1 = AsyncMock()
            mock_proc1.returncode = 0
            mock_proc1.communicate.return_value = (b"worker-task-123-abc123", b"")

            # Second call gets logs
            mock_proc2 = AsyncMock()
            mock_proc2.returncode = 0
            mock_proc2.communicate.return_value = (b"Log line 1\nLog line 2", b"")

            mock_exec.side_effect = [mock_proc1, mock_proc2]

            logs = await backend.get_job_logs("worker-task-123")
            assert logs == "Log line 1\nLog line 2"

    @pytest.mark.asyncio
    async def test_get_job_logs_no_pod(self, backend: KubernetesBackend) -> None:
        """Test getting logs when pod not found."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate.return_value = (b"", b"")
            mock_exec.return_value = mock_proc

            logs = await backend.get_job_logs("worker-task-123")
            assert logs is None
