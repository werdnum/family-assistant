"""Repository for email storage operations."""

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from family_assistant.storage.database import DatabaseTransaction
from family_assistant.storage.email import ParsedEmailData, received_emails_table
from family_assistant.storage.repositories.base import BaseRepository


class EmailRepository(BaseRepository):
    """Repository for managing received emails in the database."""

    async def store_incoming(self, parsed_email: ParsedEmailData) -> int | None:
        """
        Stores parsed email data in the `received_emails` table and enqueues an indexing task.

        Args:
            parsed_email: A Pydantic model instance containing the parsed email data.

        Returns:
            The inserted email row id, or None when the email already exists.
        """
        self._logger.info(
            f"Storing parsed email data for Message-ID: {parsed_email.message_id_header}"
        )

        # Convert Pydantic model to dict for database insertion.
        email_data_for_db = parsed_email.model_dump(
            by_alias=False, exclude_none=True
        )  # Use model_dump for Pydantic v2

        # Ensure message_id_header is present (it's non-nullable in Pydantic model and DB)
        if not email_data_for_db.get("message_id_header"):
            self._logger.error(
                "Cannot store email: 'message_id_header' (aliased as 'Message-Id') is missing after Pydantic parsing."
            )
            raise ValueError(
                "Cannot store email: 'message_id_header' is missing after Pydantic parsing."
            )

        async def _store(txn: DatabaseTransaction) -> int | None:
            """Insert the email, enqueue its indexing task, and record the task id.

            One unit: an email row committed without its indexing task would
            never become searchable, and the caller's duplicate-Message-ID
            retry would get None back rather than a second chance.

            A duplicate is absorbed by ON CONFLICT rather than allowed to raise.
            This method is reached through a caller's own transaction (the email
            webhook does exactly that), where an IntegrityError would abort that
            transaction on PostgreSQL: the caller's commit would then fail and
            the idempotent delivery retry would become a 500 instead of a no-op.
            """
            # Step 1: Insert the email and get its ID, unless it is already here
            if txn.dialect_name == "postgresql":
                # PostgreSQL: Use RETURNING to get the ID
                stmt = (
                    pg_insert(received_emails_table)
                    .values(**email_data_for_db)
                    .on_conflict_do_nothing(
                        index_elements=[received_emails_table.c.message_id_header]
                    )
                    .returning(received_emails_table.c.id)
                )
                result = await txn.execute(stmt)
                # No row back means the conflict fired: this Message-ID is here.
                email_db_id = result.scalar_one_or_none()
                if email_db_id is None:
                    return None
            else:
                # SQLite: Insert and get lastrowid. lastrowid is meaningless
                # when nothing was inserted, so rowcount is what distinguishes
                # a stored email from an absorbed duplicate.
                stmt = (
                    sqlite_insert(received_emails_table)
                    .values(**email_data_for_db)
                    .on_conflict_do_nothing(
                        index_elements=[received_emails_table.c.message_id_header]
                    )
                )
                result = await txn.execute(stmt)
                if result.rowcount == 0:
                    return None
                email_db_id = result.lastrowid

            if not email_db_id:
                raise RuntimeError(
                    f"Failed to retrieve DB ID after inserting email with Message-ID: {parsed_email.message_id_header}"
                )

            self._logger.info(
                f"Stored email with Message-ID: {parsed_email.message_id_header}, DB ID: {email_db_id}"
            )

            # Step 2: Generate a unique task ID using the DB ID
            task_id = f"index_email_{email_db_id}_{uuid.uuid4()}"

            # Step 3: Enqueue the indexing task
            await txn.tasks.enqueue(
                task_id=task_id,
                task_type="index_email",
                payload={"email_db_id": email_db_id},
            )
            self._logger.info(
                f"Enqueued indexing task {task_id} for email DB ID {email_db_id}"
            )

            # Step 4: Update the email record with the task ID
            update_stmt = (
                update(received_emails_table)
                .where(received_emails_table.c.id == email_db_id)
                .values(indexing_task_id=task_id)
            )
            await txn.execute(update_stmt)
            self._logger.info(
                f"Updated email {email_db_id} with indexing task ID {task_id}"
            )
            return int(email_db_id)

        try:
            stored_id = await self._db.atomic(_store)
        except SQLAlchemyError as e:
            self._logger.exception(
                f"Database error storing email {parsed_email.message_id_header}: {e}"
            )
            raise

        if stored_id is None:
            # A duplicate Message-ID is the delivery retry, not a failure.
            self._logger.warning(
                f"Email with Message-ID {parsed_email.message_id_header} already exists"
            )
        return stored_id

    async def get_by_message_id(self, message_id_header: str) -> dict | None:
        """
        Retrieves an email by its Message-ID header.

        Args:
            message_id_header: The Message-ID header value

        Returns:
            Email data or None if not found
        """
        stmt = select(received_emails_table).where(
            received_emails_table.c.message_id_header == message_id_header
        )
        row = await self._db.fetch_one(stmt)
        return dict(row) if row else None

    async def get_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        since: datetime | None = None,
    ) -> list[dict]:
        """
        Retrieves recent emails with pagination.

        Args:
            limit: Maximum number of emails to return
            offset: Number of emails to skip
            since: Only return emails received after this timestamp

        Returns:
            List of email dictionaries
        """
        stmt = select(received_emails_table)

        if since:
            stmt = stmt.where(received_emails_table.c.received_at >= since)

        stmt = (
            stmt
            .order_by(received_emails_table.c.received_at.desc())
            .limit(limit)
            .offset(offset)
        )

        rows = await self._db.fetch_all(stmt)
        return [dict(row) for row in rows]

    async def search_by_sender(
        self, sender_address: str, limit: int = 50
    ) -> list[dict]:
        """
        Search emails by sender address.

        Args:
            sender_address: Email address to search for
            limit: Maximum number of results

        Returns:
            List of matching emails
        """
        stmt = (
            select(received_emails_table)
            .where(received_emails_table.c.sender_address == sender_address)
            .order_by(received_emails_table.c.received_at.desc())
            .limit(limit)
        )

        rows = await self._db.fetch_all(stmt)
        return [dict(row) for row in rows]

    async def search_by_subject(
        self, subject_pattern: str, limit: int = 50
    ) -> list[dict]:
        """
        Search emails by subject pattern.

        Args:
            subject_pattern: Pattern to search for in subjects
            limit: Maximum number of results

        Returns:
            List of matching emails
        """
        stmt = (
            select(received_emails_table)
            .where(received_emails_table.c.subject.contains(subject_pattern))
            .order_by(received_emails_table.c.received_at.desc())
            .limit(limit)
        )

        rows = await self._db.fetch_all(stmt)
        return [dict(row) for row in rows]
