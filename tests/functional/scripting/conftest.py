"""Shared fixtures for scripting tests."""

import pytest

from family_assistant.scripting.engine import StarlarkEngine
from family_assistant.scripting.monty_engine import MontyEngine


@pytest.fixture(params=["starlark", "monty"], ids=["starlark", "monty"])
def engine_class(request: pytest.FixtureRequest) -> type:
    """Parameterized fixture that yields both engine classes for dual-engine testing."""
    if request.param == "starlark":
        return StarlarkEngine
    return MontyEngine
