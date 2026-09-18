"""The two memory settings, as configuration.

Slice 4 of docs/design/conversation-memory.md, "Two settings, one convenience
default". Reading memory and contributing to it are separate profile settings;
contributing implies reading, and startup rejects a profile that claims one
without the other. Both ship on for the two household profiles, and off for
every other profile, which is the code default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import ValidationError

from family_assistant.assistant import Assistant
from family_assistant.config_loader import load_config
from family_assistant.config_models import AppConfig, ProcessingConfig, ServiceProfile
from tests.unit.conftest import shipped_profile

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


def test_only_the_household_profiles_contribute_to_memory(
    shipped_config: AppConfig,
) -> None:
    """The two profiles a person talks to directly, and nothing else.

    Every other shipped profile handles email, automations, delegated work or
    media, which is either untrusted input or machine traffic; none of it is
    household conversation, and none of it should teach the notebook.
    """
    contributing = sorted(
        profile.id
        for profile in shipped_config.service_profiles
        if profile.processing_config.memory_contribute
    )

    assert contributing == ["complex_tasks", "default_assistant"]


def test_the_shipped_memory_readers_are_the_household_profiles_and_the_curator(
    shipped_config: AppConfig,
) -> None:
    """Contribution implies reading, and the curator reads to curate."""
    reading = sorted(
        profile.id
        for profile in shipped_config.service_profiles
        if profile.processing_config.memory_read
    )

    assert reading == ["complex_tasks", "default_assistant", "memory_curator"]


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
def test_the_household_profiles_carry_both_settings_explicitly(
    shipped_config: AppConfig, profile_id: str
) -> None:
    """Written down rather than inherited, so the state is visible.

    An operator reading `defaults.yaml` finds both switches on the profile,
    beside the comment saying how to turn memory off -- per profile, or for
    everything through `memory_config.enabled`. Leaving either to the code
    default would hide the household's memory behaviour from the file an
    operator reads.
    """
    processing_config = shipped_profile(shipped_config, profile_id).processing_config

    assert processing_config.model_fields_set >= {"memory_read", "memory_contribute"}
    assert processing_config.memory_read is True
    assert processing_config.memory_contribute is True


def test_the_application_counts_the_shipped_contributors(
    shipped_config: AppConfig, provider_api_keys: None
) -> None:
    """The shipped defaults, read through the helper production uses.

    `_memory_contributing_profiles` is what the enablement boundary and the
    sweep are built from, so this is the statement that a deployment which
    changes nothing contributes these two profiles and no others.
    """
    del provider_api_keys

    assistant = Assistant(shipped_config, llm_client_overrides={})

    # Reaching past the private name on purpose: asserting through the helper
    # the application itself calls is what makes this a statement about
    # production rather than about a re-derivation in a test.
    contributing = assistant._memory_contributing_profiles()  # pylint: disable=protected-access

    assert contributing == {"default_assistant", "complex_tasks"}


def test_a_read_only_profile_does_not_feed_reviews(
    shipped_config: AppConfig, provider_api_keys: None
) -> None:
    """Reading is not contributing, at the point the application counts them.

    Turning contribution off on the household profiles leaves them reading
    memory, and `_memory_contributing_profiles` -- what the enablement boundary
    and the sweep are built from -- then finds nobody, so their conversations
    produce no eligible rows and no review is enqueued for them.
    """
    del provider_api_keys
    for profile_id in ("default_assistant", "complex_tasks"):
        shipped_profile(
            shipped_config, profile_id
        ).processing_config.memory_contribute = False

    assistant = Assistant(shipped_config, llm_client_overrides={})

    # Reaching past the private name on purpose: asserting through the helper
    # the application itself calls is what makes this a statement about
    # production rather than about a re-derivation in a test.
    contributing = assistant._memory_contributing_profiles()  # pylint: disable=protected-access

    assert contributing == set()


def test_the_master_switch_leaves_no_contributing_profiles(
    shipped_config: AppConfig, provider_api_keys: None
) -> None:
    """`memory_config.enabled: false` empties the set the sweep is built from.

    The profiles keep `memory_contribute: true` -- the switch is a deployment
    saying "not right now" rather than an edit to the profiles -- and nothing
    contributes while it is off. The enablement boundary reads the configured
    setting instead, so turning the switch back on resumes from the moments
    already recorded.
    """
    del provider_api_keys
    shipped_config.memory_config.enabled = False

    assistant = Assistant(shipped_config, llm_client_overrides={})

    # Reaching past the private names on purpose: asserting through the helpers
    # the application itself calls is what makes this a statement about
    # production rather than about a re-derivation in a test.
    assert assistant._memory_contributing_profiles() == set()  # pylint: disable=protected-access
    assert assistant._configured_memory_contributing_profiles() == {  # pylint: disable=protected-access
        "default_assistant",
        "complex_tasks",
    }
