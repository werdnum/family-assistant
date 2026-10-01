"""Configuration for one AI worker sandbox instance."""

from __future__ import annotations

from typing import Literal

import cloudcoil.models.kubernetes.core.v1 as k8s_models  # noqa: TC002 - Pydantic needs at runtime
from pydantic import BaseModel, ConfigDict, Field


class WorkerResourceLimits(BaseModel):
    """Resource limits for AI worker containers."""

    model_config = ConfigDict(extra="forbid")

    memory_request: str = "512Mi"
    memory_limit: str = "2Gi"
    cpu_request: str = "500m"
    cpu_limit: str = "2000m"


class KubernetesBackendConfig(BaseModel):
    """Kubernetes-specific configuration for AI workers."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = "ml-bot"
    ai_coder_image: str = "ghcr.io/werdnum/ai-coding-base:latest"
    service_account: str = "ai-worker"
    runtime_class: str = "gvisor"
    job_ttl_seconds: int = 3600

    # Secret containing API keys (keys should be ANTHROPIC_API_KEY, GOOGLE_API_KEY, etc.)
    # All keys from this secret are injected as environment variables
    api_keys_secret: str | None = None

    # Optional config volumes for ~/.claude and ~/.gemini
    claude_config_volume: k8s_models.Volume | None = None
    gemini_config_volume: k8s_models.Volume | None = None

    # Resource limits for worker containers
    resources: WorkerResourceLimits = Field(default_factory=WorkerResourceLimits)

    # Name of the PersistentVolumeClaim for workspace storage
    workspace_pvc_name: str = "workspace"

    # Optional explicit kubeconfig path (for local dev; in-cluster config used by default)
    kubeconfig_path: str | None = None

    # Security context for worker pods (None to inherit from container image)
    run_as_user: int | None = 1000
    run_as_group: int | None = 1000
    fs_group: int | None = 1000
    enable_rootless_podman: bool = False

    # Additional volumes and volume mounts to attach to worker pods
    extra_volumes: list[k8s_models.Volume] | None = None
    extra_volume_mounts: list[k8s_models.VolumeMount] | None = None

    # Additional environment variables to inject into worker containers
    extra_env: list[k8s_models.EnvVar] | None = None


class DockerBackendConfig(BaseModel):
    """Docker-specific configuration for AI workers (local development)."""

    model_config = ConfigDict(extra="forbid")

    image: str = "ghcr.io/werdnum/ai-coding-base:latest"
    network: str = "bridge"

    # API keys from host environment variables (names of env vars to pass through)
    # Set to None to disable passing the env var
    anthropic_api_key_env: str | None = "ANTHROPIC_API_KEY"
    gemini_api_key_env: str | None = "GOOGLE_API_KEY"

    # Optional config volume mounts for ~/.claude and ~/.gemini
    claude_config_volume: str | None = None
    gemini_config_volume: str | None = None

    # Resource limits for worker containers
    resources: WorkerResourceLimits = Field(default_factory=WorkerResourceLimits)


class AIWorkersConfig(BaseModel):
    """One AI worker sandbox: where isolated coding agents run and how.

    Configuring an instance is what enables ``spawn_worker``; there is no
    separate switch.
    """

    model_config = ConfigDict(extra="forbid")

    # Backend selection
    backend_type: Literal["kubernetes", "docker", "mock"] = "kubernetes"

    # Webhook URL for worker completion notifications
    # If not set, falls back to server_url + /webhook/event
    # For Kubernetes, use internal service URL like:
    # http://family-assistant.family-assistant.svc.cluster.local:8000/webhook/event
    webhook_url: str | None = None

    # Execution settings
    default_timeout_minutes: int = 30
    max_timeout_minutes: int = 120
    max_concurrent_workers: int = 3

    # Resource limits
    resources: WorkerResourceLimits = Field(default_factory=WorkerResourceLimits)

    # Available AI agent types (used to populate tool enum at runtime)
    available_agents: list[str] = Field(default_factory=lambda: ["claude", "gemini"])

    # Cleanup settings
    task_retention_hours: int = 48

    # Backend-specific configurations
    kubernetes: KubernetesBackendConfig | None = Field(
        default_factory=KubernetesBackendConfig
    )
    docker: DockerBackendConfig | None = Field(default_factory=DockerBackendConfig)
