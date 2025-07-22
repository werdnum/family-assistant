"""Root conftest.py for tests - imports fixtures from the testing module."""

# Import all fixtures from the testing module so they're available to all tests
# pylint: disable=wildcard-import,unused-wildcard-import
from family_assistant.testing.conftest import *  # noqa: F403, F401
