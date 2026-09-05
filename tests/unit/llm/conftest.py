"""Shared access to the configuration the application actually ships.

Several suites here assert against `defaults.yaml` rather than a fixture file,
because what they pin is that the shipped profiles and tiers still mean what the
code assumes. Loading it lives here so the "config path that cannot exist" trick
that selects defaults-only is written once.
"""

import pytest

from family_assistant.config_loader import load_config
from family_assistant.config_models import AppConfig, ServiceProfile


@pytest.fixture(name="shipped_config")
def shipped_config_fixture() -> AppConfig:
    """The application configuration with no operator `config.yaml` applied."""
    return load_config(
        config_file_path="nonexistent-so-only-defaults.yaml",
        load_dotenv_file=False,
    )


def shipped_profile(config: AppConfig, profile_id: str) -> ServiceProfile:
    """One profile from a loaded configuration, failing if it is not shipped."""
    matches = [p for p in config.service_profiles if p.id == profile_id]
    assert matches, f"no shipped profile with id {profile_id!r}"
    return matches[0]
