"""Shared fixtures for scripting tests."""

from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.scripting.monty_engine import MontyEngine


@pytest.fixture
def engine_class() -> Any:  # noqa: ANN401 - returns a callable that constructs MontyEngine
    """Fixture that yields a MontyEngine constructor with default timezone.

    Returns a partial that always supplies a default timezone so that the
    time API (enabled by default) doesn't raise due to missing timezone context.
    """
    return partial(MontyEngine, default_timezone=ZoneInfo("Australia/Sydney"))
