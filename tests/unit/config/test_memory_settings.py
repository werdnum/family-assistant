"""The two memory settings, as configuration.

Slice 4 of docs/design/conversation-memory.md, "Two settings, one convenience
default". Reading memory and contributing to it are separate profile settings;
contributing implies reading, and startup rejects a profile that claims one
without the other. Both ship off, so no deployment receives model-written
memory before milestone 7 turns contribution on deliberately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import ValidationError

from family_assistant.config_loader import load_config
from family_assistant.config_models import AppConfig, ProcessingConfig, ServiceProfile

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.no_db


def _profile(*, read: bool, contribute: bool) -> ServiceProfile:
    return ServiceProfile(
        id="probe_profile",
        processing_config=ProcessingConfig(
            memory_read=read, memory_contribute=contribute
        ),
    )


def test_contributing_without_reading_is_a_startup_error() -> None:
    """A profile teaching a notebook it may not open is a contradiction."""
    with pytest.raises(ValidationError, match="memory_contribute without memory_read"):
        AppConfig(service_profiles=[_profile(read=False, contribute=True)])


def test_contributing_with_reading_is_accepted() -> None:
    config = AppConfig(service_profiles=[_profile(read=True, contribute=True)])

    profile = config.service_profiles[0]
    assert profile.processing_config.memory_contribute is True
    assert profile.processing_config.memory_read is True


def test_reading_without_contributing_is_accepted() -> None:
    """Reading does not imply contributing; that is the whole point of two settings."""
    config = AppConfig(service_profiles=[_profile(read=True, contribute=False)])

    profile = config.service_profiles[0]
    assert profile.processing_config.memory_read is True
    assert profile.processing_config.memory_contribute is False


def test_both_settings_survive_the_config_loader(tmp_path: Path) -> None:
    """A field the loader does not copy across is silently dropped from a profile."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "service_profiles": [
                {
                    "id": "memory_reader_probe",
                    "description": "Probe",
                    "processing_config": {
                        "memory_read": True,
                        "memory_contribute": True,
                    },
                }
            ]
        })
    )

    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(config_path),
        load_dotenv_file=False,
    )

    profile = next(p for p in config.service_profiles if p.id == "memory_reader_probe")
    assert profile.processing_config.memory_read is True
    assert profile.processing_config.memory_contribute is True


def test_no_shipped_profile_contributes_to_memory(shipped_config: AppConfig) -> None:
    """Contribution ships off everywhere; milestone 7 is what turns it on."""
    contributing = sorted(
        profile.id
        for profile in shipped_config.service_profiles
        if profile.processing_config.memory_contribute
    )

    assert contributing == []


def test_the_curator_is_the_only_shipped_profile_that_reads_memory(
    shipped_config: AppConfig,
) -> None:
    """Milestone 2 decides which household profiles turn reading on."""
    reading = sorted(
        profile.id
        for profile in shipped_config.service_profiles
        if profile.processing_config.memory_read
    )

    assert reading == ["memory_curator"]
