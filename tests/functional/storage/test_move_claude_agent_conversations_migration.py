"""Data migration: what the removed ``claude_agent`` profile owned moves to default."""

from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, select

from alembic import command
from family_assistant.storage.conversation_threads import conversation_threads_table
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.tasks import tasks_table

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
_PRIOR_HEAD = "add_review_rejection_notice"
_MOVE_HEAD = "move_claude_agent_conversations"
# The migration-built SQLite schema carries ``now()`` server defaults that
# SQLite cannot evaluate, so rows set their timestamps explicitly.
_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _thread(conversation_id: str, profile_id: str) -> dict[str, object]:
    return {
        "conversation_id": conversation_id,
        "home_conversation_id": "home-1",
        "interface_type": "web",
        "owner_user_id": "user-1",
        "profile_id": profile_id,
        "title": conversation_id,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _message(conversation_id: str, profile_id: str) -> dict[str, object]:
    return {
        "interface_type": "web",
        "conversation_id": conversation_id,
        "timestamp": _NOW,
        "role": "user",
        "content": "hello",
        "processing_profile_id": profile_id,
    }


def test_moves_claude_agent_conversations_and_work(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'move.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                conversation_threads_table.insert(),
                [
                    _thread("t-claude", "claude_agent"),
                    _thread("t-engineer", "engineer"),
                ],
            )
            conn.execute(
                message_history_table.insert(),
                [
                    _message("t-claude", "claude_agent"),
                    _message("t-engineer", "engineer"),
                ],
            )
            conn.execute(
                schedule_automations_table.insert(),
                [
                    {
                        "name": "made-under-claude",
                        "conversation_id": "t-claude",
                        "recurrence_rule": "FREQ=DAILY;BYHOUR=3",
                        "action_config": {"context": "check"},
                        "action_type": "wake_llm",
                        "processing_profile_id": "claude_agent",
                        "recurrence_anchor": _NOW,
                        "created_at": _NOW,
                    }
                ],
            )
            conn.execute(
                tasks_table.insert(),
                [
                    {
                        "task_id": "queued",
                        "task_type": "llm_callback",
                        "payload": {"processing_profile_id": "claude_agent"},
                        "status": "pending",
                        "created_at": _NOW,
                        "scheduled_at": _NOW,
                    },
                    {
                        "task_id": "finished",
                        "task_type": "llm_callback",
                        "payload": {"processing_profile_id": "claude_agent"},
                        "status": "done",
                        "created_at": _NOW,
                        "scheduled_at": _NOW,
                    },
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _MOVE_HEAD)

        with engine.connect() as conn:
            threads = {
                str(conversation_id): str(profile)
                for conversation_id, profile in conn.execute(
                    select(
                        conversation_threads_table.c.conversation_id,
                        conversation_threads_table.c.profile_id,
                    )
                ).all()
            }
            messages = {
                str(conversation_id): str(profile)
                for conversation_id, profile in conn.execute(
                    select(
                        message_history_table.c.conversation_id,
                        message_history_table.c.processing_profile_id,
                    )
                ).all()
            }
            automation_profile = conn.execute(
                select(schedule_automations_table.c.processing_profile_id)
            ).scalar_one()
            task_profiles = {
                task_id: payload["processing_profile_id"]
                for task_id, payload in conn.execute(
                    select(tasks_table.c.task_id, tasks_table.c.payload)
                ).all()
            }

        assert threads == {"t-claude": "default_assistant", "t-engineer": "engineer"}
        assert messages == {"t-claude": "default_assistant", "t-engineer": "engineer"}
        assert automation_profile == "default_assistant"
        assert task_profiles == {
            "queued": "default_assistant",
            "finished": "claude_agent",
        }
    finally:
        engine.dispose()
