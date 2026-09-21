"""Data migration: the lane the tasks already in the queue are classified into.

The column's server default is background, which would demote every reminder,
delegation poll and confirmation execution that is pending at upgrade time
behind whatever bulk work is queued -- the starvation the lanes exist to end.
The migration classifies the pending population by task type instead.
"""

from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import Connection, create_engine, select

from alembic import command
from family_assistant.storage.tasks import TaskPriority, tasks_table

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
_PRIOR_HEAD = "d7065490c04e"
_PRIORITY_HEAD = "add_task_priority"
# The migration-built SQLite schema carries a ``now()`` server default for
# ``created_at`` that SQLite cannot evaluate, so rows set it explicitly.
_CREATED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

_INTERACTIVE_TYPES = (
    "llm_callback",
    "confirmation_tool_execution",
    "delegated_profile_run",
    "delegation_poll",
    "script_execution",
    "email_intake_action",
    "process_uploaded_document",
    "reindex_document",
    "index_email",
    "embed_and_store_batch",
    "schedule_automation_advance",
)

_BACKGROUND_TYPES = (
    "index_message_history_batch",
    "index_note",
    "log_message",
    "system_event_cleanup",
    "system_error_log_cleanup",
    "worker_task_cleanup",
    "delegation_run_cleanup",
    "completed_automation_cleanup",
    "attachment_cleanup",
)


def _priorities_by_type(conn: Connection) -> dict[str, int]:
    """Map each pending row's task type to the lane it was classified into."""
    rows = conn.execute(select(tasks_table.c.task_type, tasks_table.c.priority)).all()
    return {str(task_type): int(priority) for task_type, priority in rows}


def test_classifies_pending_rows_by_task_type(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'priority.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                tasks_table.insert(),
                [
                    {
                        "task_id": f"pending_{task_type}",
                        "task_type": task_type,
                        "status": "pending",
                        "retry_count": 0,
                        "max_retries": 3,
                        "created_at": _CREATED_AT,
                    }
                    for task_type in (*_INTERACTIVE_TYPES, *_BACKGROUND_TYPES)
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIORITY_HEAD)

        with engine.connect() as conn:
            priorities = _priorities_by_type(conn)

        assert {
            task_type: priorities[task_type] for task_type in _INTERACTIVE_TYPES
        } == dict.fromkeys(_INTERACTIVE_TYPES, TaskPriority.INTERACTIVE)
        assert {
            task_type: priorities[task_type] for task_type in _BACKGROUND_TYPES
        } == dict.fromkeys(_BACKGROUND_TYPES, TaskPriority.BACKGROUND)
    finally:
        engine.dispose()


def test_unlisted_task_type_keeps_the_background_default(tmp_path: Path) -> None:
    """A type the classification does not name lands in the safe lane."""
    engine = create_engine(f"sqlite:///{tmp_path / 'priority_default.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                tasks_table.insert(),
                {
                    "task_id": "pending_unknown",
                    "task_type": "some_future_task_type",
                    "status": "pending",
                    "retry_count": 0,
                    "max_retries": 3,
                    "created_at": _CREATED_AT,
                },
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIORITY_HEAD)

        with engine.connect() as conn:
            priorities = _priorities_by_type(conn)

        assert priorities["some_future_task_type"] == TaskPriority.BACKGROUND
    finally:
        engine.dispose()
