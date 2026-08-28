"""Unit tests for resolving async-delegation config when registering a remote A2A profile."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from family_assistant.a2a.remote_service import RemoteA2AService
from family_assistant.assistant import Assistant
from family_assistant.config_models import (
    DEFAULT_REMOTE_MAX_ASYNC_SECONDS,
    RemoteA2AConfig,
    ServiceProfile,
)


def _register(profile: ServiceProfile) -> RemoteA2AService:
    """Run ``_setup_remote_a2a_profile`` against a minimal stand-in self.

    The method reads only the registries it wires the service from, so a
    lightweight namespace is sufficient and avoids constructing a full Assistant.
    """
    fake_self = SimpleNamespace(
        processing_services_registry={},
        attachment_registry=MagicMock(),
        _database=MagicMock(),
    )
    # _setup_remote_a2a_profile reads only these attributes, so a SimpleNamespace
    # stand-in exercises the real method without constructing a full
    # Assistant; the arg-type suppression covers that deliberate duck-typed self.
    Assistant._setup_remote_a2a_profile(fake_self, profile)  # type: ignore[arg-type]
    return fake_self.processing_services_registry[profile.id]


def test_max_async_seconds_defaults_to_one_hour_not_call_timeout() -> None:
    """Unset max_async_seconds resolves to the 1-hour default, decoupled from timeout_seconds."""
    profile = ServiceProfile(
        id="remote_agent",
        remote_a2a=RemoteA2AConfig(
            agent_url="https://agent.example.com/a2a",
            timeout_seconds=120.0,
        ),
    )

    service = _register(profile)

    assert service.service_config.max_async_seconds == DEFAULT_REMOTE_MAX_ASYNC_SECONDS
    assert service.service_config.max_async_seconds == 3600.0
    # The per-HTTP-call timeout is preserved independently of the wall-clock cap.
    assert service.service_config.timeout_seconds == 120.0


def test_explicit_max_async_seconds_is_preserved() -> None:
    """An explicit max_async_seconds overrides the default."""
    profile = ServiceProfile(
        id="remote_agent",
        remote_a2a=RemoteA2AConfig(
            agent_url="https://agent.example.com/a2a",
            timeout_seconds=120.0,
            max_async_seconds=7200.0,
        ),
    )

    service = _register(profile)

    assert service.service_config.max_async_seconds == 7200.0
