"""The review timing settings, as configuration and as a recurrence rule.

Slice 4 of docs/design/conversation-memory.md. The numbers themselves are a
deployment's to change; what is pinned here is that the configuration reaches
the value the predicate reads, that the shipped defaults are the ones the
design names, and that the sweep interval is expressible as a recurrence the
task worker can actually parse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from dateutil import rrule

from family_assistant.config_models import AppConfig, MemoryConfig
from family_assistant.memory.review_settings import MemoryReviewSettings

pytestmark = pytest.mark.no_db


def test_the_shipped_windows_are_the_ones_the_design_names() -> None:
    settings = MemoryConfig().to_review_settings()

    assert settings.enabled is True
    assert settings.idle_window("web") == timedelta(minutes=30)
    assert settings.idle_window("telegram") == timedelta(minutes=90)
    assert settings.max_deferral == timedelta(hours=24)
    assert settings.sweep_interval_minutes == 5


def test_an_unnamed_interface_takes_the_default_window() -> None:
    settings = MemoryReviewSettings(
        idle_window_minutes={"telegram": 90}, default_idle_window_minutes=30
    )

    assert settings.idle_window("web") == timedelta(minutes=30)


def test_the_spoken_interfaces_are_not_shipped_as_contributors() -> None:
    """Pinned so the exclusion stays visible rather than being quietly widened."""
    settings = MemoryConfig().to_review_settings()

    assert settings.contributing_interfaces == frozenset({"telegram", "web"})


def test_the_operator_overrides_reach_the_predicate_value() -> None:
    config = MemoryConfig(
        enabled=False,
        sweep_interval_minutes=15,
        idle_window_minutes={"web": 5},
        default_idle_window_minutes=7,
        max_deferral_hours=3,
        contributing_interfaces=["web"],
    )

    settings = config.to_review_settings()

    assert settings.enabled is False
    assert settings.sweep_interval_minutes == 15
    assert settings.idle_window("web") == timedelta(minutes=5)
    assert settings.idle_window("telegram") == timedelta(minutes=7)
    assert settings.max_deferral == timedelta(hours=3)
    assert settings.contributing_interfaces == frozenset({"web"})


def test_the_memory_config_reaches_the_app_config() -> None:
    config = AppConfig(memory_config=MemoryConfig(sweep_interval_minutes=11))

    assert config.memory_config.to_review_settings().sweep_interval_minutes == 11


def test_the_sweep_interval_renders_a_recurrence_the_worker_can_parse() -> None:
    """The worker resolves the next occurrence with `dateutil.rrule.rrulestr`."""
    interval = MemoryConfig().sweep_interval_minutes
    start = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

    rule = rrule.rrulestr(f"FREQ=MINUTELY;INTERVAL={interval}", dtstart=start)

    assert rule.after(start) == start + timedelta(minutes=interval)
