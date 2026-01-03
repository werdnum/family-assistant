"""Database query tool for the engineer profile with read-only enforcement."""

from __future__ import annotations

import json
import os
from typing import Any

import sqlparse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from family_assistant.storage.context import DatabaseContext


def is_select_only(query: str) -> bool:
    """
    Validate that a SQL query contains only SELECT statements using sqlparse.

    Args:
        query: The SQL query string to validate.

    Returns:
        True if the query contains only SELECT statements, False otherwise.
    """
    try:
        parsed = sqlparse.parse(query)
        if not parsed:
            return False

        for statement in parsed:
            # Skip empty statements (e.g., from trailing semicolons)
            if not statement.tokens or not str(statement).strip():
                continue

            # Get the statement type
            stmt_type = statement.get_type()
            if stmt_type != "SELECT":
                return False

        return True
    except Exception:
        return False


def _get_readonly_engine() -> AsyncEngine | None:
    """
    Get a read-only database engine if DB_READONLY_URL is configured.

    Returns:
        An AsyncEngine configured for read-only access, or None if not configured.
    """
    readonly_url = os.environ.get("DB_READONLY_URL")
    if readonly_url:
        return create_async_engine(readonly_url)
    return None


async def database_readonly_query(query: str) -> str:
    """
    Executes a read-only SQL query against the application database.

    This tool is for diagnostic purposes and can only perform 'SELECT' operations.
    Any attempts to modify data will be rejected.

    Security measures:
    - Uses sqlparse to validate the query is a SELECT statement
    - For PostgreSQL: Sets transaction to read-only mode
    - Optionally uses a separate read-only database URL if DB_READONLY_URL is set

    Args:
        query: The SQL query to execute.

    Returns:
        A JSON string representing the query results, or an error message.
    """
    # Validate the query using sqlparse
    if not is_select_only(query):
        return json.dumps(
            {"error": "Only SELECT queries are allowed. Query was rejected."},
            indent=2,
        )

    try:
        # Use read-only engine if available
        readonly_engine = _get_readonly_engine()

        async with DatabaseContext(engine=readonly_engine) as db:
            # For PostgreSQL, set the transaction to read-only mode
            # This provides an additional layer of protection
            if db.conn is not None:
                dialect_name = db.conn.dialect.name
                if dialect_name == "postgresql":
                    await db.conn.execute(text("SET TRANSACTION READ ONLY"))

            # Execute the query
            results = await db.fetch_all(text(query))

            # Convert results to a list of dicts for JSON serialization
            return json.dumps(
                [dict(row) for row in results],
                indent=2,
                default=str,
            )
    except Exception as e:
        return json.dumps(
            {"error": f"An error occurred while executing the query: {e}"},
            indent=2,
        )


# ast-grep-ignore: no-dict-any - Legacy tool definition format
DATABASE_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "database_readonly_query",
            "description": (
                "Executes a read-only SQL query against the application database. "
                "Only SELECT queries are allowed. Use this to inspect data for debugging."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL SELECT query to execute.",
                    },
                },
                "required": ["query"],
            },
        },
    }
]
