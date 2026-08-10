"""Commit-as-you-go database access.

Two types share one query surface and differ in exactly one way: when the work
becomes durable.

``Database`` is a stateless handle. Every operation on it acquires a
connection, begins a transaction, executes, commits, and releases — so the
write is durable the moment the call returns, and the handle can be passed
freely across turns, tasks, and tool calls without holding anything open.

``DatabaseTransaction`` is the explicit atomic block, for the sequences where
rollback is load-bearing:

    async with db.transaction() as txn:
        run_id = await txn.delegation_runs.create_run(...)
        await txn.tasks.enqueue(DELEGATED_PROFILE_RUN_TASK_TYPE, ...)

The two are deliberately **not** subtypes of each other. A function that must
run inside its caller's transaction takes ``DatabaseTransaction``; everything
else takes ``Database``. ``ToolExecutionContext.db_context`` is typed
``Database``, which makes "transaction held across a tool call"
unrepresentable rather than a code-review catch.

See docs/design/db-commit-as-you-go.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import random
import sqlite3
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import (
    DBAPIError,
    InvalidRequestError,
    MultipleResultsFound,
    NoResultFound,
)
from sqlalchemy.sql import Delete, Insert, Update

from family_assistant.request_side_effects import mark_state_changed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from datetime import datetime
    from types import TracebackType

    from sqlalchemy import TextClause
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
    from sqlalchemy.sql import Select

    from family_assistant.storage.repositories import (
        AutomationsRepository,
        ConfirmationRequestsRepository,
        ConversationSharesRepository,
        DelegationRunsRepository,
        EmailRepository,
        ErrorLogsRepository,
        EventsRepository,
        IosPushTokenRepository,
        MessageHistoryRepository,
        NotesRepository,
        OAuthConnectionsRepository,
        PushSubscriptionRepository,
        ScheduleAutomationsRepository,
        ScriptsRepository,
        TaintAuditEventsRepository,
        TasksRepository,
        VectorRepository,
        WorkerTasksRepository,
    )
    from family_assistant.storage.repositories.a2a_tasks import A2ATasksRepository

logger = logging.getLogger(__name__)

# PostgreSQL SQLSTATE codes - the authoritative way to identify error types
# See: https://www.postgresql.org/docs/current/errcodes-appendix.html
# The only failures a replay can fix: the transaction rolled back cleanly and
# nothing it wrote reached the database.
PGCODE_SERIALIZATION_FAILURE = "40001"  # Concurrent transaction conflict
PGCODE_DEADLOCK_DETECTED = "40P01"  # Two processes blocked each other


class AmbientTransactionError(RuntimeError):
    """A ``Database`` handle was used inside an open transaction.

    Opening a second transaction scope from inside ``async with
    db.transaction():`` is a hard deadlock on SQLite (whose engine lock is
    already held by the enclosing block) and, on PostgreSQL, a silent write on
    another connection that escapes the enclosing rollback. Both are worse than
    an exception, so the handle refuses.

    The fix is one of: use the transaction object (``txn.notes...``) instead of
    the handle; hoist the handle work to before the block; or, for genuine
    fire-and-forget work, spawn it with :func:`spawn_detached`.
    """


# Set for the duration of an explicit transaction. Deliberately inherited by
# child tasks with no identity-based exemption: an *awaited* child that reaches
# the handle is exactly the dangerous case -- on SQLite the parent waits on the
# child while the child waits on the parent's engine lock.
_AMBIENT_TRANSACTION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "family_assistant_ambient_db_transaction", default=False
)


def in_transaction() -> bool:
    """Whether an explicit transaction is open in this context.

    Exposed so tests can assert the boundaries the guard itself cannot see --
    that no LLM call or tool dispatch happens with a transaction open.
    """
    return _AMBIENT_TRANSACTION.get()


def spawn_detached[T](
    coro: Coroutine[Any, Any, T],
    *,
    name: str | None = None,
) -> asyncio.Task[T]:
    """Start ``coro`` in a task that does not inherit an ambient transaction.

    For fire-and-forget work that must not be tied to the caller's transaction
    — the database log handler is the canonical user. Its writes then merely
    queue behind the enclosing block on SQLite's engine lock instead of being
    rejected by the ambient-transaction guard.
    """
    context = contextvars.copy_context()
    context.run(_AMBIENT_TRANSACTION.set, False)
    return asyncio.create_task(coro, name=name, context=context)


# Deployment-level history taint epoch (taint_policy.history_taint_epoch),
# registered per engine. Database handles are created from a bare engine at
# dozens of call sites, so the epoch is attached to the engine once at startup
# instead of being re-threaded through every creation site. Weak keys keep
# short-lived test engines from accumulating entries.
_ENGINE_HISTORY_TAINT_EPOCHS: weakref.WeakKeyDictionary[AsyncEngine, datetime] = (
    weakref.WeakKeyDictionary()
)

# SQLite funnels every connection through one StaticPool connection, so two
# transaction scopes on one engine would otherwise silently share a single
# transaction. A per-engine lock held from begin to commit serializes them
# instead. All transactions are short under commit-as-you-go, so the
# serialization is cheap.
_ENGINE_TRANSACTION_LOCKS: weakref.WeakKeyDictionary[AsyncEngine, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def set_engine_history_taint_epoch(
    engine: AsyncEngine,
    epoch: datetime | None,
) -> None:
    """Attach the deployment history taint epoch to a database engine.

    Called once at application startup with
    ``taint_policy.history_taint_epoch``. ``None`` (the default for engines
    that were never configured) disables the pre-epoch amnesty, preserving the
    conservative legacy-fallback behavior.
    """
    if epoch is None:
        _ENGINE_HISTORY_TAINT_EPOCHS.pop(engine, None)
        return
    if epoch.tzinfo is None:
        msg = "history taint epoch must be timezone-aware"
        raise ValueError(msg)
    _ENGINE_HISTORY_TAINT_EPOCHS[engine] = epoch


def _engine_history_taint_epoch(engine: AsyncEngine) -> datetime | None:
    """Return the history taint epoch configured on an engine, if any."""
    return _ENGINE_HISTORY_TAINT_EPOCHS.get(engine)


def _engine_transaction_lock(engine: AsyncEngine) -> asyncio.Lock | None:
    """Return the transaction-scope lock for ``engine``, or None if unneeded."""
    if engine.dialect.name != "sqlite":
        return None
    lock = _ENGINE_TRANSACTION_LOCKS.get(engine)
    if lock is None:
        lock = asyncio.Lock()
        _ENGINE_TRANSACTION_LOCKS[engine] = lock
    return lock


def sanitize_text_for_postgres(text: str | None) -> str | None:
    """
    Sanitize text content for storage in PostgreSQL TEXT columns.

    PostgreSQL TEXT columns don't allow null bytes (\\x00) which can appear in:
    - Browser console output from Playwright
    - Binary data accidentally treated as text
    - External API responses with embedded null bytes

    This function:
    1. Removes null bytes (PostgreSQL doesn't allow them in TEXT)
    2. Handles invalid UTF-8 surrogate characters by replacing them
    3. Preserves valid control characters (tabs, newlines, ANSI escapes)

    Args:
        text: The text to sanitize, or None

    Returns:
        Sanitized text safe for PostgreSQL, or None if input was None
    """
    if text is None:
        return None

    # Remove null bytes - PostgreSQL doesn't allow them in TEXT columns
    text = text.replace("\x00", "")

    # Handle potential surrogate characters or other encoding issues
    # by round-tripping through UTF-8 with error replacement
    # This catches lone surrogates (U+D800-U+DFFF) and replaces them
    try:
        text = text.encode("utf-8", errors="surrogatepass").decode(
            "utf-8", errors="replace"
        )
    except (UnicodeDecodeError, UnicodeEncodeError):
        # If encoding fails completely, replace all problematic chars
        text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

    return text


def _is_sqlite_lock_error(orig: BaseException | None) -> bool:
    """Whether SQLite refused the write because something else held the lock.

    Reported before the transaction does any work, so a replay is safe.
    """
    if not isinstance(orig, sqlite3.OperationalError):
        return False
    message = str(orig).lower()
    return "database is locked" in message or "database table is locked" in message


def _is_retryable(exc: DBAPIError) -> bool:
    """Whether a failed unit of work is worth replaying.

    An allowlist, deliberately. ``atomic()`` replays the entire closure, so a
    retry is only correct for failures the database guarantees rolled back:
    PostgreSQL serialization failures and deadlocks, and SQLite lock
    contention. Everything else is either deterministic (a replay reproduces
    it) or of unknown outcome -- a connection dropped mid-commit above all,
    where the server may well have committed. Replaying that would write a
    second message-history row, or a second task under a fresh uuid, instead
    of surfacing the uncertainty to the caller.
    """
    pgcode = getattr(exc.orig, "pgcode", None)
    if pgcode is not None:
        return pgcode in {PGCODE_SERIALIZATION_FAILURE, PGCODE_DEADLOCK_DETECTED}
    return _is_sqlite_lock_error(exc.orig)


@dataclass(frozen=True)
class ExecuteResult:
    """The parts of a statement's result that outlive its connection.

    A handle operation commits and releases its connection before returning, so
    a live ``CursorResult`` would already be closed by the time the caller read
    it. Results are therefore materialized at execution time, which also makes
    the value safe to hold across an ``await``.
    """

    rowcount: int
    # ast-grep-ignore: no-dict-any - database rows have dynamic columns from query
    rows: list[dict[str, Any]] = field(default_factory=list)
    inserted_primary_key: tuple[Any, ...] | None = None
    lastrowid: int | None = None

    # The accessors below mirror SQLAlchemy's ``Result`` names and strictness
    # for the materialized subset, so a RETURNING statement reads the same way
    # it did against a live cursor.

    # ast-grep-ignore: no-dict-any - database rows have dynamic columns from query
    def all(self) -> list[dict[str, Any]]:
        """Every returned row."""
        return self.rows

    # ast-grep-ignore: no-dict-any - database rows have dynamic columns from query
    def one(self) -> dict[str, Any]:
        """The single returned row, or raise."""
        if not self.rows:
            raise NoResultFound("No row was returned")
        if len(self.rows) > 1:
            raise MultipleResultsFound("Multiple rows were returned for one()")
        return self.rows[0]

    # ast-grep-ignore: no-dict-any - database rows have dynamic columns from query
    def one_or_none(self) -> dict[str, Any] | None:
        """The single returned row, None if there were none, or raise if many."""
        if len(self.rows) > 1:
            raise MultipleResultsFound("Multiple rows were returned for one_or_none()")
        return self.rows[0] if self.rows else None

    def scalar_one(self) -> Any:  # noqa: ANN401 - the value's type is the caller's column type
        """The first column of the single returned row, or raise."""
        return next(iter(self.one().values()))

    def scalar_one_or_none(self) -> Any:  # noqa: ANN401 - the value's type is the caller's column type
        """The first column of the single returned row, or None."""
        row = self.one_or_none()
        return next(iter(row.values())) if row else None


def _materialize(result: CursorResult[Any]) -> ExecuteResult:
    """Read everything from ``result`` that must survive the commit."""
    rowcount = result.rowcount
    rows = (
        [dict(mapping) for mapping in result.mappings().all()]
        if result.returns_rows
        else []
    )

    inserted_primary_key: tuple[Any, ...] | None = None
    lastrowid: int | None = None
    if result.is_insert:
        # Both raise InvalidRequestError for inserts they don't apply to
        # (executemany, RETURNING, backends without a rowid).
        with contextlib.suppress(InvalidRequestError, AttributeError):
            primary_key = result.inserted_primary_key
            if primary_key is not None:
                inserted_primary_key = tuple(primary_key)
        with contextlib.suppress(InvalidRequestError, AttributeError):
            lastrowid = result.lastrowid

    return ExecuteResult(
        rowcount=rowcount,
        rows=rows,
        inserted_primary_key=inserted_primary_key,
        lastrowid=lastrowid,
    )


class DatabaseExecutor(ABC):
    """The query surface repositories are written against.

    Implemented by both :class:`Database` and :class:`DatabaseTransaction`, so
    a repository method works identically whether it was reached through the
    handle (own transaction, committed on return) or from inside a caller's
    explicit block (joins it).
    """

    def __init__(self) -> None:
        self._repositories: dict[type, object] = {}

    # --- Query surface -----------------------------------------------------

    @property
    @abstractmethod
    def engine(self) -> AsyncEngine:
        """The engine this executor runs against."""

    @property
    def dialect_name(self) -> str:
        """The SQLAlchemy dialect name, e.g. ``postgresql`` or ``sqlite``."""
        return self.engine.dialect.name

    @property
    def history_taint_epoch(self) -> datetime | None:
        """The deployment history taint epoch configured on this engine, if any."""
        return _engine_history_taint_epoch(self.engine)

    @abstractmethod
    async def execute(
        self,
        query: Select | Insert | Update | Delete | TextClause,
        # ast-grep-ignore: no-dict-any - raw SQL bind parameters with dynamic column names
        params: dict[str, Any] | None = None,
    ) -> ExecuteResult:
        """Execute a statement and return its materialized result."""

    @abstractmethod
    async def atomic[T](
        self,
        body: Callable[[DatabaseTransaction], Awaitable[T]],
    ) -> T:
        """Run ``body`` as one atomic unit.

        On a :class:`Database` handle this opens a transaction, runs the
        closure, and commits — replaying the whole closure after a rollback if
        the failure was retryable. On a :class:`DatabaseTransaction` it joins:
        the closure runs once against the ambient transaction and retry belongs
        to whoever opened it.

        The closure form (rather than ``async with``) exists precisely so the
        body *can* be re-entered on retry, which means a closure must confine
        its side effects to the transaction.
        """

    async def fetch_all(
        self,
        query: Select | TextClause,
        # ast-grep-ignore: no-dict-any - raw SQL bind parameters with dynamic column names
        params: dict[str, Any] | None = None,
        # ast-grep-ignore: no-dict-any - database rows have dynamic columns from query
    ) -> list[dict[str, Any]]:
        """Execute a query and return all rows as dictionaries."""
        return (await self.execute(query, params)).rows

    async def fetch_one(
        self,
        query: Select | TextClause,
        # ast-grep-ignore: no-dict-any - raw SQL bind parameters with dynamic column names
        params: dict[str, Any] | None = None,
        # ast-grep-ignore: no-dict-any - database row has dynamic columns from query
    ) -> dict[str, Any] | None:
        """Execute a query and return the first row, or None."""
        rows = (await self.execute(query, params)).rows
        return rows[0] if rows else None

    async def fetch_value(
        self,
        query: Select | TextClause,
        # ast-grep-ignore: no-dict-any - raw SQL bind parameters with dynamic column names
        params: dict[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401 - the value's type is the caller's column type
        """Execute a query and return the first column of the first row."""
        row = await self.fetch_one(query, params)
        if row is None:
            return None
        return next(iter(row.values()), None)

    async def init_vector_db(self) -> None:
        """Initialize vector database components."""
        await self.vector.init_db()

    # --- Repository namespace ---------------------------------------------

    def _repository[T](self, repository_class: type[T]) -> T:
        cached = self._repositories.get(repository_class)
        if cached is None:
            cached = repository_class(self)  # type: ignore[call-arg] # every repository takes an executor
            self._repositories[repository_class] = cached
        return cached  # type: ignore[return-value] # keyed by its own class

    @property
    def notes(self) -> NotesRepository:
        """Get the notes repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            NotesRepository,
        )

        return self._repository(NotesRepository)

    @property
    def oauth_connections(self) -> OAuthConnectionsRepository:
        """Get the OAuth connections repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            OAuthConnectionsRepository,
        )

        return self._repository(OAuthConnectionsRepository)

    @property
    def tasks(self) -> TasksRepository:
        """Get the tasks repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            TasksRepository,
        )

        return self._repository(TasksRepository)

    @property
    def message_history(self) -> MessageHistoryRepository:
        """Get the message history repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            MessageHistoryRepository,
        )

        return self._repository(MessageHistoryRepository)

    @property
    def email(self) -> EmailRepository:
        """Get the email repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            EmailRepository,
        )

        return self._repository(EmailRepository)

    @property
    def delegation_runs(self) -> DelegationRunsRepository:
        """Get the delegation runs repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            DelegationRunsRepository,
        )

        return self._repository(DelegationRunsRepository)

    @property
    def confirmation_requests(self) -> ConfirmationRequestsRepository:
        """Get the confirmation requests repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            ConfirmationRequestsRepository,
        )

        return self._repository(ConfirmationRequestsRepository)

    @property
    def conversation_shares(self) -> ConversationSharesRepository:
        """Get the conversation shares repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            ConversationSharesRepository,
        )

        return self._repository(ConversationSharesRepository)

    @property
    def error_logs(self) -> ErrorLogsRepository:
        """Get the error logs repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            ErrorLogsRepository,
        )

        return self._repository(ErrorLogsRepository)

    @property
    def events(self) -> EventsRepository:
        """Get the events repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            EventsRepository,
        )

        return self._repository(EventsRepository)

    @property
    def vector(self) -> VectorRepository:
        """Get the vector repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            VectorRepository,
        )

        return self._repository(VectorRepository)

    @property
    def schedule_automations(self) -> ScheduleAutomationsRepository:
        """Get the schedule automations repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            ScheduleAutomationsRepository,
        )

        return self._repository(ScheduleAutomationsRepository)

    @property
    def automations(self) -> AutomationsRepository:
        """Get the unified automations repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            AutomationsRepository,
        )

        return self._repository(AutomationsRepository)

    @property
    def push_subscriptions(self) -> PushSubscriptionRepository:
        """Get the push subscriptions repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            PushSubscriptionRepository,
        )

        return self._repository(PushSubscriptionRepository)

    @property
    def ios_push_tokens(self) -> IosPushTokenRepository:
        """Get the iOS push tokens repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            IosPushTokenRepository,
        )

        return self._repository(IosPushTokenRepository)

    @property
    def worker_tasks(self) -> WorkerTasksRepository:
        """Get the worker tasks repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            WorkerTasksRepository,
        )

        return self._repository(WorkerTasksRepository)

    @property
    def a2a_tasks(self) -> A2ATasksRepository:
        """Get the A2A tasks repository instance."""
        from family_assistant.storage.repositories.a2a_tasks import (  # noqa: PLC0415
            A2ATasksRepository,
        )

        return self._repository(A2ATasksRepository)

    @property
    def scripts(self) -> ScriptsRepository:
        """Get the scripts repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            ScriptsRepository,
        )

        return self._repository(ScriptsRepository)

    @property
    def taint_audit_events(self) -> TaintAuditEventsRepository:
        """Get the runtime taint audit events repository instance."""
        from family_assistant.storage.repositories import (  # noqa: PLC0415
            TaintAuditEventsRepository,
        )

        return self._repository(TaintAuditEventsRepository)


class Database(DatabaseExecutor):
    """A stateless handle whose every operation commits before it returns.

    Not a context manager. Hold one for as long as you like; it owns no
    connection between calls.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        max_retries: int = 3,
        base_delay: float = 0.5,
    ) -> None:
        """
        Args:
            engine: SQLAlchemy AsyncEngine for dependency injection.
            max_retries: Attempts for a unit of work that fails retryably.
            base_delay: Base delay in seconds for exponential backoff.
        """
        super().__init__()
        self._engine = engine
        self.max_retries = max_retries
        self.base_delay = base_delay

    @property
    def engine(self) -> AsyncEngine:
        """The engine this handle runs against."""
        return self._engine

    def transaction(self) -> DatabaseTransaction:
        """Open an explicit atomic block.

        Use for the sequences where rollback is load-bearing; everything else
        belongs on the handle. Nothing inside the block may reach this handle
        (see :class:`AmbientTransactionError`), and nothing inside it may run
        an LLM call or a tool dispatch.
        """
        return DatabaseTransaction(self)

    async def execute(
        self,
        query: Select | Insert | Update | Delete | TextClause,
        # ast-grep-ignore: no-dict-any - raw SQL bind parameters with dynamic column names
        params: dict[str, Any] | None = None,
    ) -> ExecuteResult:
        """Execute one statement in its own transaction, committed on return."""

        async def run(txn: DatabaseTransaction) -> ExecuteResult:
            return await txn.execute(query, params)

        return await self.atomic(run)

    async def atomic[T](
        self,
        body: Callable[[DatabaseTransaction], Awaitable[T]],
    ) -> T:
        """Run ``body`` in its own transaction, replaying it on retryable failure."""
        _reject_ambient_transaction("Database.atomic()")

        for attempt in range(self.max_retries):
            transaction = self.transaction()
            try:
                async with transaction as txn:
                    return await body(txn)
            except DBAPIError as e:
                if not _is_retryable(e):
                    logger.exception(f"Non-retryable database error: {e}")
                    raise
                if attempt == self.max_retries - 1:
                    logger.error(
                        "Max retries exceeded for retryable database error. Raising."
                    )
                    raise
                delay = self.base_delay * (2**attempt) + random.uniform(
                    0, self.base_delay
                )
                logger.warning(
                    f"Retryable DBAPIError (attempt {attempt + 1}/{self.max_retries}): "
                    f"{e}. Retrying in {delay:.2f}s."
                )
                await asyncio.sleep(delay)

        raise RuntimeError("Database operation failed after multiple retries")


class DatabaseTransaction(DatabaseExecutor):
    """One connection, one transaction, committed when the block exits.

    Obtained from :meth:`Database.transaction` or passed to an
    :meth:`DatabaseExecutor.atomic` closure. Statement failures raise
    immediately: an ``async with`` body cannot be re-entered, and retrying
    inside an aborted transaction is futile.
    """

    def __init__(self, database: Database) -> None:
        super().__init__()
        self._database = database
        self._connection: AsyncConnection | None = None
        self._lock: asyncio.Lock | None = None
        self._token: contextvars.Token[bool] | None = None
        self._transaction_cm: Any = None
        self._on_commit_callbacks: list[Callable[[], Any]] = []

    @property
    def engine(self) -> AsyncEngine:
        """The engine this transaction runs against."""
        return self._database.engine

    @property
    def connection(self) -> AsyncConnection:
        """The live connection, for the few callers that need one directly.

        Savepoints (``conn.begin_nested()``) and ORM sessions bound to a
        connection are the legitimate users; both require a guaranteed
        enclosing transaction, which is exactly what this type provides.
        """
        if self._connection is None:
            raise RuntimeError("DatabaseTransaction is not active")
        return self._connection

    async def __aenter__(self) -> DatabaseTransaction:
        """Acquire a connection and begin the transaction."""
        if self._connection is not None:
            raise RuntimeError("DatabaseTransaction is not reentrant")
        _reject_ambient_transaction("Database.transaction()")

        self._lock = _engine_transaction_lock(self.engine)
        if self._lock is not None:
            await self._lock.acquire()
        try:
            self._transaction_cm = self.engine.begin()
            self._connection = await self._transaction_cm.__aenter__()
        except BaseException:
            self._release_lock()
            raise
        self._token = _AMBIENT_TRANSACTION.set(True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Commit the transaction, or roll it back if the block raised."""
        if self._transaction_cm is None:
            return
        committed = False
        try:
            await self._transaction_cm.__aexit__(exc_type, exc_val, exc_tb)
            committed = exc_type is None
        finally:
            if self._token is not None:
                _AMBIENT_TRANSACTION.reset(self._token)
                self._token = None
            self._connection = None
            self._transaction_cm = None
            self._release_lock()
            callbacks, self._on_commit_callbacks = self._on_commit_callbacks, []

        if committed:
            for callback in callbacks:
                callback()

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    async def execute(
        self,
        query: Select | Insert | Update | Delete | TextClause,
        # ast-grep-ignore: no-dict-any - raw SQL bind parameters with dynamic column names
        params: dict[str, Any] | None = None,
    ) -> ExecuteResult:
        """Execute a statement on this transaction's connection."""
        if isinstance(query, Insert | Update | Delete):
            # Reported as the write is issued rather than on commit, so a
            # caller deciding whether to abandon this request sees it even if
            # the surrounding transaction has not finished. Raw TextClause
            # writes go unreported; this project queries symbolically.
            mark_state_changed()
        connection = self.connection
        result = (
            await connection.execute(query, params)
            if params
            else await connection.execute(query)
        )
        return _materialize(result)

    async def atomic[T](
        self,
        body: Callable[[DatabaseTransaction], Awaitable[T]],
    ) -> T:
        """Join this transaction: run ``body`` once, with retry owned by its opener."""
        return await body(self)

    def on_commit(self, callback: Callable[[], Any]) -> Callable[[], Any]:
        """Register a callback to run once this transaction has committed.

        Held on the transaction object rather than on SQLAlchemy's ``commit``
        connection event, which fires *before* the DBAPI commit: a worker woken
        from there can poll a queue whose row is not yet visible, and the
        callback would still run if the commit then failed. These run after the
        transaction context has exited successfully, and are discarded with the
        transaction on rollback -- so a replayed ``atomic()`` closure
        re-registers them and they fire exactly once, after the commit that
        actually succeeded.

        Args:
            callback: A callable to be executed after commit.

        Returns:
            The original callback for chaining.
        """
        # Accessing the connection asserts the transaction is active, so a
        # callback cannot be registered on a finished transaction.
        _ = self.connection
        self._on_commit_callbacks.append(callback)
        return callback


def _reject_ambient_transaction(operation: str) -> None:
    if _AMBIENT_TRANSACTION.get():
        raise AmbientTransactionError(
            f"{operation} was called inside an open database transaction. "
            "Use the transaction object instead of the handle, hoist the work "
            "to before the block, or spawn genuinely detached work with "
            "storage.database.spawn_detached()."
        )
