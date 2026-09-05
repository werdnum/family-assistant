"""Unit tests for resolving async-delegation config when registering a remote A2A profile."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from family_assistant.a2a.remote_service import RemoteA2AService
from family_assistant.assistant import Assistant
from family_assistant.config_loader import load_config
from family_assistant.config_models import (
    DEFAULT_REMOTE_MAX_ASYNC_SECONDS,
    RemoteA2AConfig,
    ServiceProfile,
)

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.config_models import AppConfig


def _stand_in_assistant(config: AppConfig | None = None) -> SimpleNamespace:
    """A duck-typed `self` for the profile-setup methods under test.

    They read only the registries they wire the service from, so a lightweight
    namespace exercises the real methods without constructing a full Assistant.
    `_setup_remote_a2a_profile` is bound onto it because
    `_setup_processing_profile` reaches it through `self`.
    """
    fake_self = SimpleNamespace(
        config=config,
        processing_services_registry={},
        attachment_registry=MagicMock(),
        _database=MagicMock(),
    )
    # The arg-type suppression covers the deliberate duck-typed stand-in.
    fake_self._setup_remote_a2a_profile = partial(
        Assistant._setup_remote_a2a_profile,
        fake_self,  # type: ignore[arg-type]
    )
    return fake_self


def _register(profile: ServiceProfile) -> RemoteA2AService:
    """Run ``_setup_remote_a2a_profile`` against a minimal stand-in self."""
    fake_self = _stand_in_assistant()
    fake_self._setup_remote_a2a_profile(profile)
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


async def test_remote_profile_from_shipped_defaults_reaches_registration(
    tmp_path: Path,
) -> None:
    """Setup validates the tier before the remote early return, so it must pass.

    A remote profile names no model of its own, so it used to inherit the
    shipped `default_profile_settings.model_tier` -- which
    `validate_profile_model_tier` refuses on a remote profile, failing startup
    for the entire application. `_register` above starts after that check; this
    goes through the same entry point the assistant uses.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "service_profiles:\n"
        '  - id: "k8s_agent"\n'
        '    description: "Remote Kubernetes agent"\n'
        "    remote_a2a:\n"
        '      agent_url: "http://k8s-agent:9000/a2a"\n'
    )
    config = load_config(config_file_path=str(config_file), load_dotenv_file=False)
    profile = next(p for p in config.service_profiles if p.id == "k8s_agent")

    fake_self = _stand_in_assistant(config)
    # The arg-type suppression covers the duck-typed stand-in for `self`.
    await Assistant._setup_processing_profile(
        fake_self,  # type: ignore[arg-type]
        profile,
        None,
        {},
        None,
    )

    assert isinstance(
        fake_self.processing_services_registry["k8s_agent"], RemoteA2AService
    )


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
