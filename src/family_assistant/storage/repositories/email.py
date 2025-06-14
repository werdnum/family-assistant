"""Repository for email storage operations."""

import uuid
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from family_assistant import storage
from family_assistant.storage.email import ParsedEmailData, received_emails_table
from family_assistant.storage.repositories.base import BaseRepository


class EmailRepository(BaseRepository):
    """Repository for managing received emails in the database."""

    async def store_incoming(self, parsed_email: ParsedEmailData) -> None:
        """
        Stores parsed email data in the `received_emails` table and enqueues an indexing task.

        Args:
            parsed_email: A Pydantic model instance containing the parsed email data.
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

        try:
            # Step 1: Insert the email and get its ID
            if self._db.engine.dialect.name == "postgresql":
                # PostgreSQL: Use RETURNING to get the ID
                stmt = (
                    insert(received_emails_table)
                    .values(**email_data_for_db)
                    .returning(received_emails_table.c.id)
                )
                result = await self._db.execute_with_retry(stmt)
                row = result.one()  # type: ignore[attr-defined]
                email_db_id = row[0]
            else:
                # SQLite: Insert and get lastrowid
                stmt = insert(received_emails_table).values(**email_data_for_db)
                result = await self._db.execute_with_retry(stmt)
                email_db_id = result.lastrowid  # type: ignore[attr-defined]

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
            await storage.enqueue_task(
                db_context=self._db,
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
            await self._db.execute_with_retry(update_stmt)
            self._logger.info(
                f"Updated email {email_db_id} with indexing task ID {task_id}"
            )

        except IntegrityError as e:
            # Handle duplicate emails
            self._logger.warning(
                f"Email with Message-ID {parsed_email.message_id_header} already exists: {e}"
            )
            # Don't re-raise - email already exists
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error storing email {parsed_email.message_id_header}: {e}",
                exc_info=True,
            )
            raise
        except Exception as e:
            self._logger.error(
                f"Unexpected error storing email {parsed_email.message_id_header}: {e}",
                exc_info=True,
            )
            raise

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
            stmt.order_by(received_emails_table.c.received_at.desc())
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
