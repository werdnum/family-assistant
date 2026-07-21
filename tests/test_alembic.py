"""Tests for Alembic migrations using pytest-alembic.

The pytest-alembic built-in tests (``test_upgrade``, ``test_up_down_consistency``,
``test_model_definitions_match_ddl``, ``test_single_head_revision``) are
parametrized over both SQLite and PostgreSQL via the ``alembic_engine`` fixture.

Running them against real PostgreSQL is what makes these tests a general
validator rather than a smoke test: SQLite silently ignores constraints that
PostgreSQL enforces (``VARCHAR`` lengths, ``NOT NULL``/``CHECK``/``FK``
semantics, type affinity, server defaults). For example, ``test_upgrade`` on
PostgreSQL inserts every revision id into ``alembic_version.version_num``
(``VARCHAR(32)`` by default); a revision id longer than 32 characters fails the
upgrade with ``StringDataRightTruncation`` naming the offender, with no
hardcoded length check needed.
"""

import os
import tempfile
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.config import Config

# Import pytest-alembic built-in tests
from pytest_alembic.tests import (
    test_model_definitions_match_ddl,
    test_single_head_revision,
    test_up_down_consistency,
    test_upgrade,
)
from sqlalchemy import Engine, create_engine, text

from tests.conftest import ContainerProtocol

# Re-export the tests to ensure they're available
__all__ = [
    "test_model_definitions_match_ddl",
    "test_single_head_revision",
    "test_up_down_consistency",
    "test_upgrade",
]


def _sqlite_alembic_engine() -> Generator[Engine]:
    """Yield a sync SQLite engine over an empty temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        yield engine
    finally:
        engine.dispose()
        if os.path.exists(db_path):
            os.unlink(db_path)


def _postgres_alembic_engine(
    postgres_container: ContainerProtocol,
) -> Generator[Engine]:
    """Yield a sync PostgreSQL engine over a freshly created empty database.

    pytest-alembic drives migrations through ``alembic/env.py``, which runs the
    injected engine synchronously, so this hands it a sync (psycopg) engine
    rather than the app's asyncpg engine. A unique empty database is created for
    isolation and dropped on teardown.
    """
    admin_url = postgres_container.get_connection_url().replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    unique_db_name = f"test_alembic_{uuid.uuid4().hex[:12]}"

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{unique_db_name}"'))
    finally:
        admin_engine.dispose()

    # Preserve any Unix-socket query parameters (?host=/tmp/...) from pgserver.
    if "?" in admin_url:
        base_url, query_params = admin_url.rsplit("?", 1)
        db_part = base_url.rsplit("/", 1)[0]
        test_db_url = f"{db_part}/{unique_db_name}?{query_params}"
    else:
        test_db_url = admin_url.rsplit("/", 1)[0] + f"/{unique_db_name}"

    engine = create_engine(test_db_url)
    try:
        yield engine
    finally:
        engine.dispose()
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with admin_engine.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                    ),
                    {"dbname": unique_db_name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{unique_db_name}"'))
        finally:
            admin_engine.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def alembic_engine(request: pytest.FixtureRequest) -> Generator[Engine]:
    """Provide an empty database engine for pytest-alembic, per backend.

    Parametrized over SQLite and PostgreSQL so every pytest-alembic built-in
    test runs against both backends. PostgreSQL reuses the session-scoped
    ``postgres_container`` fixture (pgserver or ``TEST_DATABASE_URL``).
    """
    if request.param == "sqlite":
        yield from _sqlite_alembic_engine()
    else:
        postgres_container = request.getfixturevalue("postgres_container")
        yield from _postgres_alembic_engine(postgres_container)


@pytest.fixture
def alembic_config() -> Config:
    """
    Override this fixture to provide the alembic config object.
    """

    # Use relative path to alembic.ini
    config_path = Path(__file__).parent.parent / "alembic.ini"
    return Config(str(config_path))
