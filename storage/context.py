"""
Database context manager for storage operations.

This module provides a context manager and utilities for database operations,
enabling dependency injection for testing and centralizing retry logic.
"""

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional, TypeVar, Generic, Callable, Union, cast

from sqlalchemy import Result, TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import Select, Insert, Update, Delete
from sqlalchemy.exc import DBAPIError

from .base import get_engine

logger = logging.getLogger(__name__)

# Type variable for query result type
T = TypeVar('T')

class DatabaseContext:
    """
    Context manager for database operations with retry logic.
    
    This class provides a centralized way to handle database connections,
    transactions, and retry logic for database operations.
    """
    
    def __init__(
        self, 
        engine: Optional[AsyncEngine] = None,
        max_retries: int = 3,
        base_delay: float = 0.5
    ):
        """
        Initialize the database context.
        
        Args:
            engine: Optional SQLAlchemy AsyncEngine. If not provided, the default engine from
                   storage.base will be used. This enables dependency injection for testing.
            max_retries: Maximum number of retries for database operations.
            base_delay: Base delay in seconds for exponential backoff.
        """
        self.engine = engine or get_engine()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.conn: Optional[AsyncConnection] = None
        self._in_transaction = False
        
    async def __aenter__(self) -> "DatabaseContext":
        """Enter the async context manager, establishing a database connection."""
        if self.conn is not None:
            raise RuntimeError("DatabaseContext is not reentrant")
        
        self.conn = await self.engine.connect()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the async context manager, closing the database connection."""
        if self.conn is not None:
            if self._in_transaction:
                # If we're still in a transaction, roll it back
                await self.conn.rollback()
                self._in_transaction = False
                
            await self.conn.close()
            self.conn = None
    
    async def begin(self):
        """Begin a transaction."""
        if self.conn is None:
            raise RuntimeError("No active database connection")
        
        if self._in_transaction:
            raise RuntimeError("Already in a transaction")
            
        await self.conn.begin()
        self._in_transaction = True
        
    async def commit(self):
        """Commit the current transaction."""
        if self.conn is None:
            raise RuntimeError("No active database connection")
            
        if not self._in_transaction:
            raise RuntimeError("Not in a transaction")
            
        await self.conn.commit()
        self._in_transaction = False
        
    async def rollback(self):
        """Roll back the current transaction."""
        if self.conn is None:
            raise RuntimeError("No active database connection")
            
        if not self._in_transaction:
            raise RuntimeError("Not in a transaction")
            
        await self.conn.rollback()
        self._in_transaction = False
    
    async def execute_with_retry(
        self, 
        query: Union[Select, Insert, Update, Delete, TextClause],
        params: Optional[Dict[str, Any]] = None
    ) -> Result:
        """
        Execute a query with retry logic for transient database errors.
        
        Args:
            query: The SQLAlchemy query to execute.
            params: Optional parameters for the query.
            
        Returns:
            The SQLAlchemy Result object.
            
        Raises:
            RuntimeError: If there is no active database connection or if
                         all retry attempts fail.
        """
        if self.conn is None:
            raise RuntimeError("No active database connection")
            
        for attempt in range(self.max_retries):
            try:
                if params:
                    return await self.conn.execute(query, params)
                else:
                    return await self.conn.execute(query)
            except DBAPIError as e:
                logger.warning(
                    f"DBAPIError (attempt {attempt + 1}/{self.max_retries}): {e}."
                )
                if attempt == self.max_retries - 1:
                    logger.error(f"Max retries exceeded. Raising error.")
                    raise
                    
                # Calculate backoff with jitter for retry
                delay = self.base_delay * (2**attempt) + random.uniform(0, self.base_delay)
                logger.info(f"Retrying in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"Non-retryable error: {e}", exc_info=True)
                raise
                
        # This should never happen as we raise inside the loop on max retries
        raise RuntimeError("Database operation failed after multiple retries")
    
    async def fetch_all(
        self, 
        query: Union[Select, TextClause],
        params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Execute a query and fetch all results as dictionaries.
        
        Args:
            query: The SQLAlchemy SELECT query to execute.
            params: Optional parameters for the query.
            
        Returns:
            A list of dictionaries representing the rows.
        """
        result = await self.execute_with_retry(query, params)
        rows = result.fetchall()
        return [row._mapping for row in rows]
    
    async def fetch_one(
        self, 
        query: Union[Select, TextClause],
        params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a query and fetch one result as a dictionary.
        
        Args:
            query: The SQLAlchemy SELECT query to execute.
            params: Optional parameters for the query.
            
        Returns:
            A dictionary representing the row, or None if no results.
        """
        result = await self.execute_with_retry(query, params)
        row = result.fetchone()
        return row._mapping if row else None
    
    async def execute_and_commit(
        self, 
        query: Union[Insert, Update, Delete, TextClause],
        params: Optional[Dict[str, Any]] = None
    ) -> Result:
        """
        Execute a query and commit the transaction, with retry logic.
        This opens a new transaction if one is not already in progress.
        
        Args:
            query: The SQLAlchemy query to execute.
            params: Optional parameters for the query.
            
        Returns:
            The SQLAlchemy Result object.
        """
        if self.conn is None:
            raise RuntimeError("No active database connection")
            
        # Check if we're in a transaction already
        was_in_transaction = self._in_transaction
        
        if not was_in_transaction:
            await self.begin()
            
        try:
            result = await self.execute_with_retry(query, params)
            
            if not was_in_transaction:
                await self.commit()
                
            return result
        except Exception:
            if not was_in_transaction:
                await self.rollback()
            raise

# Convenience function to create a database context
async def get_db_context(
    engine: Optional[AsyncEngine] = None,
    max_retries: int = 3,
    base_delay: float = 0.5
) -> DatabaseContext:
    """
    Create and enter a database context.
    
    This function creates a DatabaseContext and enters its async context manager,
    returning the active context. This is intended to be used with an
    async with statement.
    
    Args:
        engine: Optional SQLAlchemy AsyncEngine for dependency injection.
        max_retries: Maximum number of retries for database operations.
        base_delay: Base delay in seconds for exponential backoff.
        
    Returns:
        An active DatabaseContext.
    
    Example:
        ```python
        async with get_db_context() as db:
            result = await db.fetch_all(...)
        ```
    """
    return DatabaseContext(engine, max_retries, base_delay)
