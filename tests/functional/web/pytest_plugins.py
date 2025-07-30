"""Pytest plugin to configure session-scoped event loops for web tests."""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio to use session scope for web tests."""
    print("Web pytest plugin: Configuring session scope for event loops")
    # Override the default event loop scope for this test session
    config.option.asyncio_default_fixture_loop_scope = "session"
    config.option.asyncio_default_test_loop_scope = "session"
    print(
        f"Web pytest plugin: fixture_loop_scope = {config.option.asyncio_default_fixture_loop_scope}"
    )
    print(
        f"Web pytest plugin: test_loop_scope = {config.option.asyncio_default_test_loop_scope}"
    )
