"""Functional tests for incoming-email storage semantics.

PostgreSQL only, like every other test that stores a received email (see
``tests/functional/web/api/test_mail_webhook_security.py``). The table's ``id``
is a ``BigInteger``, which SQLite does not treat as a rowid alias, so an insert
that omits it fails there — email intake has never run on SQLite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from family_assistant.storage.database import Database
from family_assistant.storage.email import ParsedEmailData, received_emails_table
from family_assistant.storage.tasks import tasks_table

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _parsed_email(message_id: str) -> ParsedEmailData:
    return ParsedEmailData.model_validate({
        "Message-Id": message_id,
        "sender": "sender@example.com",
        "From": "Sender <sender@example.com>",
        "recipient": "intake@example.net",
        "To": "Intake <intake@example.net>",
        "subject": "Delivery",
        "body-plain": "Body",
        "stripped-text": "Body",
    })


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_store_incoming_returns_the_new_row_id(db_engine: AsyncEngine) -> None:
    """A first delivery is stored and gets its indexing task."""
    db = Database(engine=db_engine)

    email_db_id = await db.email.store_incoming(_parsed_email("<first@example.com>"))

    assert email_db_id is not None
    tasks = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "index_email")
    )
    assert [task["payload"]["email_db_id"] for task in tasks] == [email_db_id]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_duplicate_delivery_inside_a_transaction_does_not_abort_it(
    db_engine: AsyncEngine,
) -> None:
    """A duplicate Message-ID is a no-op the caller's transaction survives.

    The email webhook calls store_incoming through its own transaction, so a
    duplicate that raised IntegrityError would abort that transaction on
    PostgreSQL: the caller's commit would fail and the idempotent delivery
    retry would surface as a 500 rather than a no-op.
    """
    db = Database(engine=db_engine)
    parsed = _parsed_email("<repeat@example.com>")
    first_id = await db.email.store_incoming(parsed)
    assert first_id is not None

    async with db.transaction() as txn:
        duplicate_id = await txn.email.store_incoming(parsed)
        assert duplicate_id is None
        # A write after the duplicate, which only commits if the transaction is
        # still usable.
        await txn.tasks.enqueue(
            task_id="after-duplicate",
            task_type="index_email",
            payload={"email_db_id": first_id},
        )

    rows = await db.fetch_all(
        select(received_emails_table).where(
            received_emails_table.c.message_id_header == "<repeat@example.com>"
        )
    )
    assert len(rows) == 1
    assert rows[0]["id"] == first_id

    survived = await db.fetch_one(
        select(tasks_table).where(tasks_table.c.task_id == "after-duplicate")
    )
    assert survived is not None
