"""Runtime instrumentation for database engines.

The transactional design of this application depends on two invariants that
are invisible to the type checker: transactions stay short (nothing parks
inside one across an LLM call or a network round trip), and no unit of work
leaks a pooled connection. Both are properties of what actually happened at
runtime, so they are asserted by the test suite rather than reviewed by hand.

Instrumentation is attached opt-in by
:func:`family_assistant.storage.base.create_engine_with_sqlite_optimizations`
and enabled by the shared ``db_engine`` fixture — the chokepoint every
database test funnels through — so the whole dual-backend suite carries the
checks.
"""

from __future__ import annotations

import time
import traceback
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import greenlet
from sqlalchemy import event

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncEngine

# A transaction that outlives this bound is holding a connection across
# something it should not (an LLM call, a network request, a sleep). The bound
# is deliberately generous: it catches parked transactions, not slow queries.
DEFAULT_MAX_TRANSACTION_SECONDS = 10.0

# How many application frames to keep for a transaction's origin. Enough to
# identify the opening call site and its caller chain without retaining
# pytest's own frames.
_STACK_DEPTH = 15


def _capture_origin() -> str:
    """Render the application frames that opened a transaction.

    Connection events fire on SQLAlchemy's worker greenlet, whose own stack is
    just the driver's plumbing — the awaiting application frames are on the
    parent greenlet. So the chain is walked outwards and third-party frames are
    dropped, rather than the stack simply being truncated by depth, which would
    keep exactly the frames that say nothing about which call site opened the
    transaction.
    """
    stacks = [traceback.extract_stack()]
    current: greenlet.greenlet | None = greenlet.getcurrent()
    while current is not None:
        frame = current.gr_frame
        if frame is not None:
            stacks.append(traceback.extract_stack(frame))
        current = current.parent

    frames = [
        frame
        for stack in reversed(stacks)
        for frame in stack
        if "site-packages" not in frame.filename and frame.filename != __file__
    ]
    return "".join(traceback.format_list(frames[-_STACK_DEPTH:]))


@dataclass(frozen=True)
class LongTransaction:
    """A transaction that stayed open longer than the configured bound."""

    duration_seconds: float
    opened_at_stack: str

    def describe(self) -> str:
        """Render a human-readable report for a test failure message."""
        return (
            f"transaction held open for {self.duration_seconds:.2f}s, opened at:\n"
            f"{self.opened_at_stack}"
        )


@dataclass
class _OpenTransaction:
    started_at: float
    stack: str


@dataclass
class EngineInstrumentation:
    """Records transaction lifetimes and pool checkouts for one engine.

    The recorded state is cumulative for the engine's lifetime; the fixture
    that owns the engine inspects it at teardown.
    """

    max_transaction_seconds: float = DEFAULT_MAX_TRANSACTION_SECONDS
    monotonic: Callable[[], float] = time.monotonic

    long_transactions: list[LongTransaction] = field(default_factory=list)
    _open: dict[int, _OpenTransaction] = field(default_factory=dict)
    _checked_out: int = 0

    @property
    def checked_out_connections(self) -> int:
        """Connections currently checked out of the pool."""
        return self._checked_out

    @property
    def open_transactions(self) -> int:
        """Transactions currently open (begun but neither committed nor rolled back)."""
        return len(self._open)

    def _on_begin(self, conn: Connection) -> None:
        self._open[id(conn)] = _OpenTransaction(
            started_at=self.monotonic(),
            stack=_capture_origin(),
        )

    def _on_end(self, conn: Connection) -> None:
        started = self._open.pop(id(conn), None)
        if started is None:
            return
        duration = self.monotonic() - started.started_at
        if duration > self.max_transaction_seconds:
            self.long_transactions.append(
                LongTransaction(
                    duration_seconds=duration, opened_at_stack=started.stack
                )
            )

    def _on_checkout(self, *_args: object) -> None:
        self._checked_out += 1

    def _on_checkin(self, *_args: object) -> None:
        self._checked_out -= 1

    def violations(self) -> list[str]:
        """Return descriptions of every invariant this engine has violated."""
        problems = [txn.describe() for txn in self.long_transactions]
        if self._checked_out != 0:
            problems.append(
                f"{self._checked_out} connection(s) still checked out of the pool"
            )
        return problems


_ENGINE_INSTRUMENTATION: weakref.WeakKeyDictionary[
    AsyncEngine, EngineInstrumentation
] = weakref.WeakKeyDictionary()


def attach_instrumentation(
    engine: AsyncEngine,
    max_transaction_seconds: float = DEFAULT_MAX_TRANSACTION_SECONDS,
) -> EngineInstrumentation:
    """Attach transaction/pool instrumentation to ``engine`` and return it."""
    instrumentation = EngineInstrumentation(
        max_transaction_seconds=max_transaction_seconds
    )
    sync_engine = engine.sync_engine
    event.listen(sync_engine, "begin", instrumentation._on_begin)
    event.listen(sync_engine, "commit", instrumentation._on_end)
    event.listen(sync_engine, "rollback", instrumentation._on_end)
    event.listen(sync_engine.pool, "checkout", instrumentation._on_checkout)
    event.listen(sync_engine.pool, "checkin", instrumentation._on_checkin)
    _ENGINE_INSTRUMENTATION[engine] = instrumentation
    return instrumentation


def get_instrumentation(engine: AsyncEngine) -> EngineInstrumentation | None:
    """Return the instrumentation attached to ``engine``, if any."""
    return _ENGINE_INSTRUMENTATION.get(engine)
