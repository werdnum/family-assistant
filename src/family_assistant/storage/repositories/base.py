"""Base repository class for storage repositories."""

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.sql import Delete, Insert, Select, Update

from family_assistant.storage.database import DatabaseExecutor, ExecuteResult


class BaseRepository:
    """Base class for all storage repositories."""

    def __init__(self, db: DatabaseExecutor) -> None:
        """Initialize repository with a database executor.

        Args:
            db: The handle or transaction this repository runs against. Which
                one it is determines when the work commits, not how it is
                written.
        """
        self._db = db
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    async def _execute_with_logging(
        self,
        operation_name: str,
        query: "Select | Insert | Update | Delete",
        params: dict[str, object] | None = None,
    ) -> ExecuteResult:
        """Execute query with consistent error logging.

        Args:
            operation_name: Name of the operation for logging
            query: SQLAlchemy query to execute
            params: Optional query parameters

        Raises:
            SQLAlchemyError: Re-raises database errors after logging
        """
        try:
            return await self._db.execute(query, params)
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in {operation_name}: {e}")
            raise
