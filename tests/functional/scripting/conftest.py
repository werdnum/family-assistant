"""Shared fixtures for scripting tests."""

import pytest

from family_assistant.scripting.monty_engine import MontyEngine


@pytest.fixture
def engine_class() -> type:
    """Fixture that yields the MontyEngine class for scripting tests."""
    return MontyEngine
