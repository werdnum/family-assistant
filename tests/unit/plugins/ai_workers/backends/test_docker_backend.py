"""Tests for the Docker backend."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from family_assistant.plugins.ai_workers.backend import WorkerStatus
from family_assistant.plugins.ai_workers.backends.docker import DockerBackend
from family_assistant.plugins.ai_workers.config import DockerBackendConfig

if TYPE_CHECKING:
    from collections.abc import Generator


class FakeDockerProcess:
    """A finished ``docker`` CLI process with canned output."""

    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class FakeDockerCli:
    """Stands in for ``asyncio.create_subprocess_exec``, recording each argv."""

    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self.commands: list[tuple[str, ...]] = []

    async def __call__(self, *argv: str, **_kwargs: object) -> FakeDockerProcess:
        self.commands.append(argv)
        return FakeDockerProcess(self._stdout, self._stderr, self._returncode)


@contextmanager
def fake_docker(
    stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
) -> Generator[FakeDockerCli]:
    cli = FakeDockerCli(stdout, stderr, returncode)
    with patch.object(asyncio, "create_subprocess_exec", new=cli):
        yield cli


@pytest.fixture
def docker_config() -> DockerBackendConfig:
    """Create a test Docker config."""
    return DockerBackendConfig(
        image="test-image:latest",
        network="test-network",
        anthropic_api_key_env="ANTHROPIC_API_KEY",
        gemini_api_key_env="GOOGLE_API_KEY",
        claude_config_volume=None,
        gemini_config_volume=None,
    )


@pytest.fixture
def backend(docker_config: DockerBackendConfig, tmp_path: Path) -> DockerBackend:
    """Create a DockerBackend instance for testing."""
    return DockerBackend(config=docker_config, workspace_root=str(tmp_path))


async def spawn_tracked_task(
    backend: DockerBackend,
    tmp_path: Path,
    task_id: str = "task-123",
    container_id: str = "container-123",
) -> str:
    """Spawn a task through the public API so it is tracked by the backend."""
    (tmp_path / "tasks" / task_id).mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / task_id / "output").mkdir(exist_ok=True)
    (tmp_path / "tasks" / task_id / "prompt.md").write_text("Test prompt")

    with fake_docker(stdout=f"{container_id}\n".encode()) as docker:
        job_id = await backend.spawn_task(
            task_id=task_id,
            prompt_path=f"tasks/{task_id}/prompt.md",
            output_dir=f"tasks/{task_id}/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )

    assert job_id == container_id
    assert docker.commands[0][:2] == ("docker", "run")
    return job_id


class TestDockerBackendInit:
    """Tests for DockerBackend initialization."""

    @pytest.mark.asyncio
    async def test_init_without_config_uses_defaults_in_command(
        self, tmp_path: Path
    ) -> None:
        """Test backend falls back to default image/network in the docker command."""
        backend = DockerBackend(workspace_root=str(tmp_path))
        cmd = await backend._build_docker_command(
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )
        assert "--network=bridge" in cmd
        assert "ghcr.io/werdnum/ai-coding-base:latest" in cmd

    @pytest.mark.asyncio
    async def test_init_without_workspace_root(
        self, docker_config: DockerBackendConfig
    ) -> None:
        """Test backend defaults to cwd for the task volume mount."""
        backend = DockerBackend(config=docker_config)
        cmd = await backend._build_docker_command(
            task_id="task-123",
            prompt_path="tasks/task-123/prompt.md",
            output_dir="tasks/task-123/output",
            webhook_url="http://localhost:8000/webhook/event",
            model="claude",
            timeout_minutes=30,
        )
        cmd_str = " ".join(cmd)
        assert f"{Path.cwd().resolve()}/tasks/task-123:/task" in cmd_str


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

        with fake_docker(stdout=b"abc123containerid\n") as docker:
            job_id = await backend.spawn_task(
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

            assert job_id == "abc123containerid"
            task = backend.get_task(job_id)
            assert task is not None
            assert task.task_id == "task-123"
            assert task.status == WorkerStatus.RUNNING
            assert task.model == "claude"
            assert len(docker.commands) == 1
            assert docker.commands[0][:2] == ("docker", "run")

    @pytest.mark.asyncio
    async def test_spawn_task_failure(self, backend: DockerBackend) -> None:
        """Test spawn task handles docker failure."""
        with (
            fake_docker(stderr=b"Error: image not found\n", returncode=1),
            pytest.raises(RuntimeError, match="Failed to start Docker container"),
        ):
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
        with (
            fake_docker(),
            pytest.raises(RuntimeError, match="Docker returned empty container ID"),
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
        assert cmd[-3:] == ["sh", "-c", 'run-task < "$TASK_INPUT"']

        # Check environment variables
        cmd_str = " ".join(cmd)
        assert "TASK_ID=task-123" in cmd_str
        # Paths are relative to /task since we mount only the task's directory
        assert "TASK_INPUT=/task/prompt.md" in cmd_str
        assert "TASK_OUTPUT_DIR=/task/output" in cmd_str
        assert "TASK_WEBHOOK_URL=http://localhost:8000/webhook/event" in cmd_str
        assert "AI_AGENT=claude" in cmd_str
        assert "MAX_TURNS=50" in cmd_str

        # Check task-specific mount (only task directory, not full workspace)
        assert "-v" in cmd
        assert f"{tmp_path}/tasks/task-123:/task" in cmd_str

    @pytest.mark.asyncio
    async def test_build_command_with_claude_config_volume(
        self, tmp_path: Path
    ) -> None:
        """Test command includes Claude config volume mount when configured."""
        config = DockerBackendConfig(
            image="test-image:latest",
            network="test-network",
            claude_config_volume="claude-config:/home/user/.claude:ro",
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
        assert "claude-config:/home/user/.claude:ro" in cmd_str

    @pytest.mark.asyncio
    async def test_build_command_with_api_key_env(self, tmp_path: Path) -> None:
        """Test command includes API key env var from host when available."""
        config = DockerBackendConfig(
            image="test-image:latest",
            network="test-network",
            anthropic_api_key_env="ANTHROPIC_API_KEY",
        )
        backend = DockerBackend(config=config, workspace_root=str(tmp_path))

        # Patch os.environ.get to return a fake API key
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test123"}):
            cmd = await backend._build_docker_command(
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

        cmd_str = " ".join(cmd)
        assert "ANTHROPIC_API_KEY=sk-ant-test123" in cmd_str

    @pytest.mark.asyncio
    async def test_build_command_warns_missing_api_key(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test warning is logged when configured API key env var is missing."""
        config = DockerBackendConfig(
            image="test-image:latest",
            network="test-network",
            anthropic_api_key_env="ANTHROPIC_API_KEY",
        )
        backend = DockerBackend(config=config, workspace_root=str(tmp_path))

        with patch.dict("os.environ", {}, clear=True):
            cmd = await backend._build_docker_command(
                task_id="task-123",
                prompt_path="tasks/task-123/prompt.md",
                output_dir="tasks/task-123/output",
                webhook_url="http://localhost:8000/webhook/event",
                model="claude",
                timeout_minutes=30,
            )

        cmd_str = " ".join(cmd)
        assert "ANTHROPIC_API_KEY" not in cmd_str
        assert "not found in environment" in caplog.text


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
    async def test_get_status_running(
        self, backend: DockerBackend, tmp_path: Path
    ) -> None:
        """Test getting status of running container."""
        container_id = await spawn_tracked_task(backend, tmp_path)

        with fake_docker(stdout=b"running:0\n") as docker:
            result = await backend.get_task_status(container_id)
            assert result.status == WorkerStatus.RUNNING
            assert docker.commands == [
                (
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}}:{{.State.ExitCode}}",
                    container_id,
                )
            ]

    @pytest.mark.asyncio
    async def test_get_status_exited_success(
        self, backend: DockerBackend, tmp_path: Path
    ) -> None:
        """Test getting status of successfully exited container."""
        container_id = await spawn_tracked_task(backend, tmp_path)

        with fake_docker(stdout=b"exited:0\n") as docker:
            result = await backend.get_task_status(container_id)
            assert result.status == WorkerStatus.SUCCESS
            assert result.exit_code == 0
            assert docker.commands == [
                (
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}}:{{.State.ExitCode}}",
                    container_id,
                )
            ]

        task = backend.get_task(container_id)
        assert task is not None
        assert task.status == WorkerStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_get_status_exited_failure(
        self, backend: DockerBackend, tmp_path: Path
    ) -> None:
        """Test getting status of container that exited with error."""
        container_id = await spawn_tracked_task(backend, tmp_path)

        with fake_docker(stdout=b"exited:1\n") as docker:
            result = await backend.get_task_status(container_id)
            assert result.status == WorkerStatus.FAILED
            assert result.exit_code == 1
            assert docker.commands == [
                (
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}}:{{.State.ExitCode}}",
                    container_id,
                )
            ]


class TestDockerBackendCancelTask:
    """Tests for DockerBackend.cancel_task()."""

    @pytest.mark.asyncio
    async def test_cancel_running_task(
        self, backend: DockerBackend, tmp_path: Path
    ) -> None:
        """Test cancelling a running task."""
        container_id = await spawn_tracked_task(backend, tmp_path)

        with fake_docker(stdout=f"{container_id}\n".encode()) as docker:
            result = await backend.cancel_task(container_id)
            assert result is True
            assert docker.commands == [("docker", "stop", "--time=10", container_id)]

        task = backend.get_task(container_id)
        assert task is not None
        assert task.status == WorkerStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_unknown_task(self, backend: DockerBackend) -> None:
        """Test cancelling unknown task returns False."""
        result = await backend.cancel_task("unknown-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_already_completed(
        self, backend: DockerBackend, tmp_path: Path
    ) -> None:
        """Test cancelling already completed task returns False."""
        container_id = await spawn_tracked_task(backend, tmp_path)

        with fake_docker(stdout=b"exited:0\n"):
            status_result = await backend.get_task_status(container_id)
        assert status_result.status == WorkerStatus.SUCCESS

        result = await backend.cancel_task(container_id)
        assert result is False


class TestDockerBackendHelperMethods:
    """Tests for DockerBackend helper methods."""

    @pytest.mark.asyncio
    async def test_get_task(self, backend: DockerBackend, tmp_path: Path) -> None:
        """Test get_task returns task by container ID."""
        container_id = await spawn_tracked_task(backend, tmp_path)

        task = backend.get_task(container_id)
        assert task is not None
        assert task.task_id == "task-123"
        assert task.container_id == container_id
        assert backend.get_task("unknown") is None

    @pytest.mark.asyncio
    async def test_get_task_by_task_id(
        self, backend: DockerBackend, tmp_path: Path
    ) -> None:
        """Test get_task_by_task_id returns task by task ID."""
        container_id = await spawn_tracked_task(backend, tmp_path)

        task = backend.get_task_by_task_id("task-123")
        assert task is not None
        assert task.container_id == container_id
        assert backend.get_task_by_task_id("unknown") is None

    @pytest.mark.asyncio
    async def test_clear(self, backend: DockerBackend, tmp_path: Path) -> None:
        """Test clear removes all tasks."""
        container_id = await spawn_tracked_task(backend, tmp_path)
        assert backend.get_task(container_id) is not None

        backend.clear()
        assert backend.get_task(container_id) is None
        assert backend.get_task_by_task_id("task-123") is None
