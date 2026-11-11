"""The `db_independent` marker CI uses to run backend-agnostic tests once."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine


def test_a_test_using_no_database_fixture_is_marked(
    request: pytest.FixtureRequest,
) -> None:
    assert request.node.get_closest_marker("db_independent") is not None


def test_a_test_using_a_database_fixture_is_not_marked(
    request: pytest.FixtureRequest, db_engine: AsyncEngine
) -> None:
    assert request.node.get_closest_marker("db_independent") is None
