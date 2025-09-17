"""Base repository class for storage repositories."""

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.sql import Delete, Insert, Select, Update

from family_assistant.storage.context import DatabaseContext


class BaseRepository:
    """Base class for all storage repositories."""

    def __init__(self, db_context: DatabaseContext) -> None:
        """Initialize repository with database context.

        Args:
            db_context: The database context for operations
        """
        self._db = db_context
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    async def _execute_with_logging(
        self,
        operation_name: str,
        query: "Select | Insert | Update | Delete",
        params: dict[str, object] | None = None,
    ) -> "CursorResult[object]":
        """Execute query with consistent error logging.

        Args:
            operation_name: Name of the operation for logging
            query: SQLAlchemy query to execute
            params: Optional query parameters

        Raises:
            SQLAlchemyError: Re-raises database errors after logging
        """
        try:
            return await self._db.execute_with_retry(query, params)
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in {operation_name}: {e}", exc_info=True
            )
            raise
