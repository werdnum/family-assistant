"""Tests for init_db's transient-vs-fatal error classification.

init_db retries only genuine connection-level failures. Deterministic errors
(bad DDL/DML, constraint or value-truncation failures) must fail fast so the
real error surfaces immediately instead of being masked by ~8 minutes of
backoff.
"""

import pytest
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)

import family_assistant.storage as storage_module
from family_assistant.storage import init_db


def _dbapi_error(
    cls: type[DBAPIError], *, connection_invalidated: bool = False
) -> DBAPIError:
    return cls(
        "stmt", {}, Exception("boom"), connection_invalidated=connection_invalidated
    )


@pytest.mark.parametrize(
    "exc",
    [
        _dbapi_error(OperationalError),
        _dbapi_error(InterfaceError),
        _dbapi_error(ProgrammingError, connection_invalidated=True),
    ],
)
def test_transient_errors_are_retryable(exc: BaseException) -> None:
    assert storage_module._is_transient_db_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _dbapi_error(ProgrammingError),
        _dbapi_error(DataError),
        _dbapi_error(IntegrityError),
        ValueError("not a db error"),
        RuntimeError("unexpected"),
    ],
)
def test_deterministic_errors_are_not_retryable(exc: BaseException) -> None:
    assert storage_module._is_transient_db_error(exc) is False


async def test_init_db_fails_fast_on_deterministic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deterministic error propagates immediately without any retry/backoff."""
    fatal = _dbapi_error(DataError)  # e.g. StringDataRightTruncation

    async def _raise_fatal(_engine: object) -> bool:
        raise fatal

    sleep_calls: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(storage_module, "_get_alembic_config", lambda _engine: object())
    monkeypatch.setattr(storage_module, "_is_alembic_managed", _raise_fatal)
    monkeypatch.setattr(storage_module.asyncio, "sleep", _record_sleep)

    with pytest.raises(DataError):
        await init_db(object())  # type: ignore[arg-type]

    assert sleep_calls == [], "deterministic error must not trigger any retry backoff"


async def test_init_db_retries_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient error retries, then succeeds once the condition clears."""
    attempts = {"count": 0}

    async def _flaky_is_managed(_engine: object) -> bool:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _dbapi_error(OperationalError)  # simulate DB not ready yet
        return True  # third attempt: treat as an existing, managed DB

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    sleep_calls: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(storage_module, "_get_alembic_config", lambda _engine: object())
    monkeypatch.setattr(storage_module, "_is_alembic_managed", _flaky_is_managed)
    monkeypatch.setattr(storage_module, "_log_current_revision", _noop)
    monkeypatch.setattr(storage_module, "_run_alembic_command", _noop)
    monkeypatch.setattr(storage_module.asyncio, "sleep", _record_sleep)

    await init_db(object())  # type: ignore[arg-type]

    assert attempts["count"] == 3
    assert len(sleep_calls) == 2, "should back off once per transient failure"
