"""Data migration: delete the second copies of web callback replies."""

from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, select

from alembic import command
from family_assistant.storage.message_history import message_history_table

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
_PRIOR_HEAD = "delete_orphaned_note_documents"
_CLEANUP_HEAD = "delete_duplicate_web_callback_replies"
_NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _row(
    internal_id: int,
    *,
    content: str,
    interface_type: str = "web",
    turn_id: str | None = None,
    interface_message_id: str | None = None,
) -> dict[str, object]:
    return {
        "internal_id": internal_id,
        "interface_type": interface_type,
        "conversation_id": "conv-1",
        "interface_message_id": interface_message_id,
        "turn_id": turn_id,
        "timestamp": _NOW,
        "role": "assistant",
        "content": content,
    }


def test_deletes_only_copies_their_canonical_row_points_at(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'duplicates.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                message_history_table.insert(),
                [
                    # A callback reply and the copy delivery saved.
                    _row(1, content="tea", turn_id="t1", interface_message_id="2"),
                    _row(2, content="tea"),
                    # An ordinary out-of-band web message nothing points at.
                    _row(3, content="hello"),
                    # Pointed at, but not the same reply.
                    _row(4, content="one", turn_id="t2", interface_message_id="5"),
                    _row(5, content="two"),
                    # Telegram ids are Telegram's, not history row ids.
                    _row(
                        6,
                        content="tg",
                        interface_type="telegram",
                        turn_id="t3",
                        interface_message_id="7",
                    ),
                    _row(7, content="tg", interface_type="telegram"),
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _CLEANUP_HEAD)

        with engine.connect() as conn:
            remaining = set(
                conn.execute(select(message_history_table.c.internal_id)).scalars()
            )
        assert remaining == {1, 3, 4, 5, 6, 7}
    finally:
        engine.dispose()
