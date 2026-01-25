"""Tests for the Docker backend."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from family_assistant.config_models import DockerBackendConfig
from family_assistant.services.backends.docker import DockerBackend, DockerTask
from family_assistant.services.worker_backend import WorkerStatus


@pytest.fixture
def docker_config() -> DockerBackendConfig:
    """Create a test Docker config."""
    return DockerBackendConfig(
        image="test-image:latest",
        network="test-network",
        claude_config_path=None,
        gemini_config_path=None,
    )


@pytest.fixture
def backend(docker_config: DockerBackendConfig, tmp_path: Path) -> DockerBackend:
    """Create a DockerBackend instance for testing."""
    return DockerBackend(config=docker_config, workspace_root=str(tmp_path))


class TestDockerBackendInit:
    """Tests for DockerBackend initialization."""

    def test_init_with_config(
        self, docker_config: DockerBackendConfig, tmp_path: Path
    ) -> None:
        """Test backend initializes with provided config."""
        backend = DockerBackend(config=docker_config, workspace_root=str(tmp_path))
        assert backend.image == "test-image:latest"
        assert backend.network == "test-network"

    def test_init_without_config(self, tmp_path: Path) -> None:
        """Test backend uses defaults when no config provided."""
        backend = DockerBackend(workspace_root=str(tmp_path))
        assert backend.image == "ghcr.io/werdnum/ai-coding-base:latest"
        assert backend.network == "bridge"

    def test_init_without_workspace_root(
        self, docker_config: DockerBackendConfig
    ) -> None:
        """Test backend defaults to cwd when no workspace_root provided."""
        backend = DockerBackend(config=docker_config)
        assert backend._workspace_root == Path.cwd()


class TestDockerBackendSpawnTask:
    """Tests for DockerBackend.spawn_task()."""

    @pytest.mark.asyncio
    async def test_spawn_task_success(
        self, backend: DockerBackend, tmp_path: Path
    ) -> None:
        """Test successful task spawn."""
        # Create required directories
        (tmp_path / "tasks" / "task-123").mkdir(parents=True)
        (tmp_path / "tasks" / "task-123" / "output").mkdir()
        (tmp_path / "tasks" / "task-123" / "prompt.md").write_text("Test prompt")

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"abc123containerid\n", b"")
            mock_exec.return_value = mock_proc

            job_id = await backend.spawn_task(
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

            assert job_id == "abc123containerid"
            assert job_id in backend._tasks
            task = backend._tasks[job_id]
            assert task.task_id == "task-123"
            assert task.status == WorkerStatus.RUNNING
            assert task.model == "claude"

    @pytest.mark.asyncio
    async def test_spawn_task_failure(self, backend: DockerBackend) -> None:
        """Test spawn task handles docker failure."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate.return_value = (b"", b"Error: image not found\n")
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError, match="Failed to start Docker container"):
                await backend.spawn_task(
                    task_id="task-123",
                    prompt_path="tasks/task-123/prompt.md",
                    output_dir="tasks/task-123/output",
                    webhook_url="http://localhost:8000/webhook/event",
                    model="claude",
                    timeout_minutes=30,
                )

    @pytest.mark.asyncio
    async def test_spawn_task_empty_container_id(self, backend: DockerBackend) -> None:
        """Test spawn task handles empty container ID."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"", b"")
            mock_exec.return_value = mock_proc

            with pytest.raises(
                RuntimeError, match="Docker returned empty container ID"
            ):
                await backend.spawn_task(
                    task_id="task-123",
                    prompt_path="tasks/task-123/prompt.md",
                    output_dir="tasks/task-123/output",
                    webhook_url="http://localhost:8000/webhook/event",
                    model="claude",
                    timeout_minutes=30,
                )


class TestDockerBackendBuildCommand:
    """Tests for DockerBackend._build_docker_command()."""

    @pytest.mark.asyncio
    async def test_build_command_basic(
        self, backend: DockerBackend, tmp_path: Path
    ) -> None:
        """Test building basic docker command."""
        cmd = await backend._build_docker_command(
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert "--detach" in cmd
        assert "--rm" in cmd
        assert "--name=worker-task-123" in cmd
        assert "--network=test-network" in cmd
        assert "test-image:latest" in cmd
        assert "run-task" in cmd

        # Check environment variables
        cmd_str = " ".join(cmd)
        assert "TASK_ID=task-123" in cmd_str
        assert "TASK_INPUT=/workspace/tasks/task-123/prompt.md" in cmd_str
        assert "TASK_OUTPUT_DIR=/workspace/tasks/task-123/output" in cmd_str
        assert "TASK_WEBHOOK_URL=http://localhost:8000/webhook/event" in cmd_str
        assert "AI_AGENT=claude" in cmd_str
        assert "MAX_TURNS=50" in cmd_str

        # Check workspace mount
        assert "-v" in cmd
        assert f"{tmp_path}:/workspace" in cmd_str

    @pytest.mark.asyncio
    async def test_build_command_with_claude_config(self, tmp_path: Path) -> None:
        """Test command includes Claude config mount when configured."""
        # Create a mock claude config directory
        claude_config_dir = tmp_path / ".claude"
        claude_config_dir.mkdir()

        config = DockerBackendConfig(
            image="test-image:latest",
            network="test-network",
            claude_config_path=str(claude_config_dir),
        )
        backend = DockerBackend(config=config, workspace_root=str(tmp_path))

        cmd = await backend._build_docker_command(
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

        cmd_str = " ".join(cmd)
        assert f"{claude_config_dir}:/home/user/.claude:ro" in cmd_str


class TestDockerBackendGetTaskStatus:
    """Tests for DockerBackend.get_task_status()."""

    @pytest.mark.asyncio
    async def test_get_status_unknown_task(self, backend: DockerBackend) -> None:
        """Test getting status of unknown task."""
        result = await backend.get_task_status("unknown-id")
        assert result.status == WorkerStatus.FAILED
        assert result.error_message is not None
        assert "not found" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_get_status_running(self, backend: DockerBackend) -> None:
        """Test getting status of running container."""
        # Add a task to track
        backend._tasks["container-123"] = DockerTask(
            task_id="task-123",
            container_id="container-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"running:0\n", b"")
            mock_exec.return_value = mock_proc

            result = await backend.get_task_status("container-123")
            assert result.status == WorkerStatus.RUNNING

    @pytest.mark.asyncio
    async def test_get_status_exited_success(self, backend: DockerBackend) -> None:
        """Test getting status of successfully exited container."""
        backend._tasks["container-123"] = DockerTask(
            task_id="task-123",
            container_id="container-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"exited:0\n", b"")
            mock_exec.return_value = mock_proc

            result = await backend.get_task_status("container-123")
            assert result.status == WorkerStatus.SUCCESS
            assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_get_status_exited_failure(self, backend: DockerBackend) -> None:
        """Test getting status of container that exited with error."""
        backend._tasks["container-123"] = DockerTask(
            task_id="task-123",
            container_id="container-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"exited:1\n", b"")
            mock_exec.return_value = mock_proc

            result = await backend.get_task_status("container-123")
            assert result.status == WorkerStatus.FAILED
            assert result.exit_code == 1


class TestDockerBackendCancelTask:
    """Tests for DockerBackend.cancel_task()."""

    @pytest.mark.asyncio
    async def test_cancel_running_task(self, backend: DockerBackend) -> None:
        """Test cancelling a running task."""
        backend._tasks["container-123"] = DockerTask(
            task_id="task-123",
            container_id="container-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.RUNNING,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"container-123\n", b"")
            mock_exec.return_value = mock_proc

            result = await backend.cancel_task("container-123")
            assert result is True
            assert backend._tasks["container-123"].status == WorkerStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_unknown_task(self, backend: DockerBackend) -> None:
        """Test cancelling unknown task returns False."""
        result = await backend.cancel_task("unknown-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_already_completed(self, backend: DockerBackend) -> None:
        """Test cancelling already completed task returns False."""
        backend._tasks["container-123"] = DockerTask(
            task_id="task-123",
            container_id="container-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
            status=WorkerStatus.SUCCESS,
        )

        result = await backend.cancel_task("container-123")
        assert result is False


class TestDockerBackendHelperMethods:
    """Tests for DockerBackend helper methods."""

    def test_get_task(self, backend: DockerBackend) -> None:
        """Test get_task returns task by container ID."""
        task = DockerTask(
            task_id="task-123",
            container_id="container-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
        )
        backend._tasks["container-123"] = task

        assert backend.get_task("container-123") == task
        assert backend.get_task("unknown") is None

    def test_get_task_by_task_id(self, backend: DockerBackend) -> None:
        """Test get_task_by_task_id returns task by task ID."""
        task = DockerTask(
            task_id="task-123",
            container_id="container-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
        )
        backend._tasks["container-123"] = task

        assert backend.get_task_by_task_id("task-123") == task
        assert backend.get_task_by_task_id("unknown") is None

    def test_clear(self, backend: DockerBackend) -> None:
        """Test clear removes all tasks."""
        backend._tasks["container-123"] = DockerTask(
            task_id="task-123",
            container_id="container-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            model="claude",
            timeout_minutes=30,
        )

        backend.clear()
        assert len(backend._tasks) == 0
