"""Tests for Alembic migrations using pytest-alembic."""

import os
import tempfile
from collections.abc import Generator

import pytest
from alembic.config import Config

# Import pytest-alembic built-in tests
from pytest_alembic.tests import (
    test_model_definitions_match_ddl,
    test_single_head_revision,
    test_up_down_consistency,
    test_upgrade,
)
from sqlalchemy import Engine, create_engine

# Re-export the tests to ensure they're available
__all__ = [
    "test_model_definitions_match_ddl",
    "test_single_head_revision",
    "test_up_down_consistency",
    "test_upgrade",
]


@pytest.fixture
def alembic_engine() -> Generator[Engine, None, None]:
    """
    Override this fixture to provide a custom engine for pytest-alembic.

    The engine should point to an empty database for tests to work properly.
    """
    # Create a temporary database file for testing
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    # Use sync SQLite for alembic tests (pytest-alembic expects sync engines)
    engine = create_engine(f"sqlite:///{db_path}")

    yield engine

    # Clean up the temp file
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def alembic_config() -> Config:
    """
    Override this fixture to provide the alembic config object.
    """
    from pathlib import Path

    # Use relative path to alembic.ini
    config_path = Path(__file__).parent.parent / "alembic.ini"
    return Config(str(config_path))
