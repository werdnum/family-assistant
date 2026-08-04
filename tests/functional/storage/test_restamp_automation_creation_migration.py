"""Data migration: automations owned by the removed ``automation_creation`` profile.

The profile was replaced by the "Automation Creation" built-in skill. Execution-time
profile resolution is fail-loud, so automations still stamped with the removed
profile would raise on every fire rather than falling back. The migration re-stamps
them to ``default_assistant``.
"""

from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import Connection, Table, create_engine, select

from alembic import command
from family_assistant.storage.confirmation_requests import confirmation_requests_table
from family_assistant.storage.delegation_runs import delegation_runs_table
from family_assistant.storage.events import event_listeners_table
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.tasks import tasks_table

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
_PRIOR_HEAD = "a2a_tasks_json_to_jsonb"
_RESTAMP_HEAD = "restamp_automation_creation"
# The migration-built SQLite schema carries a ``now()`` server default for
# ``created_at`` that SQLite cannot evaluate, so rows set it explicitly.
_CREATED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _delegation_row(
    delegation_id: str, target_service_id: str, status: str
) -> dict[str, object]:
    """A delegation_runs row with the columns the table requires as NOT NULL."""
    return {
        "delegation_id": delegation_id,
        "task_id": f"task-{delegation_id}",
        "status": status,
        "source_profile_id": "default_assistant",
        "target_service_id": target_service_id,
        "interface_type": "telegram",
        "conversation_id": f"conv-{delegation_id}",
        "subconversation_id": f"sub-{delegation_id}",
        "request_text": "make me an automation",
        "content_parts_json": [],
        "created_at": _CREATED_AT,
    }


def _confirmation_row(
    request_id: str, processing_profile_id: str, status: str
) -> dict[str, object]:
    """A confirmation_requests row with the columns the table requires."""
    return {
        "id": request_id,
        "target_user_id": "user-123",
        "status": status,
        "tool_name": "add_calendar_event",
        "tool_args_json": {"title": "dentist"},
        "confirmation_prompt": "Add this event?",
        "processing_profile_id": processing_profile_id,
        "expires_at": _CREATED_AT,
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
    }


def _profiles_by_name(conn: Connection, table: Table) -> dict[str, str | None]:
    """Map each row's name to its recorded creator profile."""
    rows = conn.execute(select(table.c.name, table.c.processing_profile_id)).all()
    return {
        str(name): None if profile is None else str(profile) for name, profile in rows
    }


def test_restamps_only_automation_creation_rows(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'restamp.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                event_listeners_table.insert(),
                [
                    {
                        "name": "owned-by-removed-profile",
                        "source_id": "home_assistant",
                        "match_conditions": {"entity_id": "person.alex"},
                        "action_type": "wake_llm",
                        "conversation_id": "conv-1",
                        "processing_profile_id": "automation_creation",
                        "created_at": _CREATED_AT,
                    },
                    {
                        "name": "owned-by-event-handler",
                        "source_id": "home_assistant",
                        "match_conditions": {"entity_id": "person.sam"},
                        "action_type": "script",
                        "conversation_id": "conv-2",
                        "processing_profile_id": "event_handler",
                        "created_at": _CREATED_AT,
                    },
                    {
                        "name": "legacy-unstamped",
                        "source_id": "home_assistant",
                        "match_conditions": {},
                        "action_type": "wake_llm",
                        "conversation_id": "conv-3",
                        "processing_profile_id": None,
                        "created_at": _CREATED_AT,
                    },
                ],
            )
            conn.execute(
                schedule_automations_table.insert(),
                [
                    {
                        "name": "weekly-check",
                        "conversation_id": "conv-4",
                        "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU;BYHOUR=12",
                        "action_config": {"context": "check the thing"},
                        "action_type": "wake_llm",
                        "processing_profile_id": "automation_creation",
                        "created_at": _CREATED_AT,
                    },
                    {
                        "name": "ops-diagnostics",
                        "conversation_id": "conv-5",
                        "recurrence_rule": "FREQ=DAILY;BYHOUR=3",
                        "action_config": {"script_code": "pass"},
                        "action_type": "script",
                        "processing_profile_id": "ops_automation",
                        "created_at": _CREATED_AT,
                    },
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _RESTAMP_HEAD)

        with engine.connect() as conn:
            listeners = _profiles_by_name(conn, event_listeners_table)
            schedules = _profiles_by_name(conn, schedule_automations_table)

        assert listeners["owned-by-removed-profile"] == "default_assistant"
        assert listeners["owned-by-event-handler"] == "event_handler"
        assert listeners["legacy-unstamped"] is None
        assert schedules["weekly-check"] == "default_assistant"
        assert schedules["ops-diagnostics"] == "ops_automation"
    finally:
        engine.dispose()


def test_restamps_in_flight_delegation_targets(tmp_path: Path) -> None:
    """A delegation still in flight across the upgrade keeps its target resolvable.

    The target profile lives in its own column, not the task payload, and the
    delegation handler fails the run outright when it no longer resolves.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'restamp_delegations.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                delegation_runs_table.insert(),
                [
                    _delegation_row(
                        "queued-to-removed", "automation_creation", "queued"
                    ),
                    _delegation_row(
                        "running-to-removed", "automation_creation", "running"
                    ),
                    _delegation_row(
                        "completed-to-removed", "automation_creation", "completed"
                    ),
                    _delegation_row("queued-to-other", "research", "queued"),
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _RESTAMP_HEAD)

        with engine.connect() as conn:
            targets = {
                str(delegation_id): str(target)
                for delegation_id, target in conn.execute(
                    select(
                        delegation_runs_table.c.delegation_id,
                        delegation_runs_table.c.target_service_id,
                    )
                ).all()
            }

        assert targets["queued-to-removed"] == "default_assistant"
        assert targets["running-to-removed"] == "default_assistant"
        # Terminal runs are history; rewriting their target would misreport where
        # the work actually ran.
        assert targets["completed-to-removed"] == "automation_creation"
        assert targets["queued-to-other"] == "research"
    finally:
        engine.dispose()


def test_restamps_outstanding_confirmation_profiles(tmp_path: Path) -> None:
    """An approval the user has yet to act on still executes after the upgrade.

    The deferred execution task carries only the request id, so the profile is
    read back off the request and resolved fail-loud.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'restamp_confirmations.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                confirmation_requests_table.insert(),
                [
                    _confirmation_row("pending-req", "automation_creation", "pending"),
                    _confirmation_row(
                        "approved-req", "automation_creation", "approved"
                    ),
                    _confirmation_row(
                        "rejected-req", "automation_creation", "rejected"
                    ),
                    _confirmation_row("other-profile-req", "ops_automation", "pending"),
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _RESTAMP_HEAD)

        with engine.connect() as conn:
            profiles = {
                str(request_id): str(profile)
                for request_id, profile in conn.execute(
                    select(
                        confirmation_requests_table.c.id,
                        confirmation_requests_table.c.processing_profile_id,
                    )
                ).all()
            }

        assert profiles["pending-req"] == "default_assistant"
        assert profiles["approved-req"] == "default_assistant"
        # Rejected is terminal — it will never execute, and its profile is the
        # record of who asked.
        assert profiles["rejected-req"] == "automation_creation"
        assert profiles["other-profile-req"] == "ops_automation"
    finally:
        engine.dispose()


def test_restamps_queued_task_payloads(tmp_path: Path) -> None:
    """A recurring automation's next occurrence is already queued when we upgrade.

    The task payload carries its own copy of the provenance, so re-stamping only
    the automation tables would leave the next fire resolving a removed profile.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'restamp_tasks.db'}")
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
                        "task_id": "sched_auto_43_pending",
                        "task_type": "llm_callback",
                        "status": "pending",
                        "payload": {
                            "conversation_id": "conv-4",
                            "callback_context": "check the thing",
                            "processing_profile_id": "automation_creation",
                        },
                        "created_at": _CREATED_AT,
                    },
                    {
                        "task_id": "script_failed_retryable",
                        "task_type": "script_execution",
                        "status": "failed",
                        "payload": {
                            "script_code": "pass",
                            "processing_profile_id": "automation_creation",
                        },
                        "created_at": _CREATED_AT,
                    },
                    {
                        "task_id": "other_profile_pending",
                        "task_type": "llm_callback",
                        "status": "pending",
                        "payload": {
                            "conversation_id": "conv-9",
                            "processing_profile_id": "ops_automation",
                        },
                        "created_at": _CREATED_AT,
                    },
                    {
                        "task_id": "unstamped_pending",
                        "task_type": "llm_callback",
                        "status": "pending",
                        "payload": {"conversation_id": "conv-10"},
                        "created_at": _CREATED_AT,
                    },
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _RESTAMP_HEAD)

        with engine.connect() as conn:
            payloads = {
                str(task_id): payload
                for task_id, payload in conn.execute(
                    select(tasks_table.c.task_id, tasks_table.c.payload)
                ).all()
            }

        assert payloads["sched_auto_43_pending"]["processing_profile_id"] == (
            "default_assistant"
        )
        assert payloads["script_failed_retryable"]["processing_profile_id"] == (
            "default_assistant"
        )
        assert payloads["other_profile_pending"]["processing_profile_id"] == (
            "ops_automation"
        )
        assert "processing_profile_id" not in payloads["unstamped_pending"]
        # The rest of the payload survives the rewrite.
        assert payloads["sched_auto_43_pending"]["callback_context"] == (
            "check the thing"
        )
    finally:
        engine.dispose()
