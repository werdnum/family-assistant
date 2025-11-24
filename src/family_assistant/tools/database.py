import json

from sqlalchemy import text

from family_assistant.storage.context import DatabaseContext


async def database_readonly_query(query: str) -> str:
    """
    Executes a read-only SQL query against the application database.

    This tool is for diagnostic purposes and can only perform 'SELECT' operations.
    Any attempts to modify data (e.g., using INSERT, UPDATE, DELETE) will be rejected.

    Args:
        query: The SQL query to execute.

    Returns:
        A JSON string representing the query results, or an error message.
    """
    normalized_query = query.strip().upper()
    if not normalized_query.startswith("SELECT"):
        return "Error: Only SELECT queries are allowed."

    # Basic security check to prevent modification queries
    disallowed_keywords = [
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
    ]
    if any(keyword in normalized_query for keyword in disallowed_keywords):
        return "Error: Query contains disallowed keywords."

    try:
        async with DatabaseContext() as db:
            results = await db.fetch_all(text(query))
            # Convert results to a list of dicts for JSON serialization
            return json.dumps([dict(row) for row in results], indent=2, default=str)
    except Exception as e:
        return json.dumps(
            {"error": f"An error occurred while executing the query: {e}"}, indent=2
        )


DATABASE_TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "database_readonly_query",
            "description": "Executes a read-only SQL query against the application database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute.",
                    },
                },
                "required": ["query"],
            },
        },
    }
]
