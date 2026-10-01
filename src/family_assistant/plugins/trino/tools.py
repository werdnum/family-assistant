"""The Trino query tool, graded by the tables each query reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.plugins.trino.client import TrinoQueryError
from family_assistant.plugins.trino.instance import TrinoInstance
from family_assistant.security.taint import SourceTrustTier
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

QUERY_TRINO_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "query_trino",
        "description": (
            "Run a read-only SQL query against the household data lake in Trino "
            "(health metrics, bank transactions, Home Assistant history, operations "
            "events and message archives). Use SHOW SCHEMAS, SHOW TABLES and "
            "DESCRIBE to explore. Returns column names and rows, truncated to a "
            "row limit, so aggregate in SQL rather than fetching raw rows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "One Trino SQL statement, without a trailing semicolon.",
                }
            },
            "required": ["sql"],
        },
    },
}


async def query_trino_tool(exec_context: ToolExecutionContext, sql: str) -> ToolResult:
    """Run ``sql``, grading the result by the tables Trino says it reads."""
    instance = (
        exec_context.plugins.get(TrinoInstance)
        if exec_context.plugins is not None
        else None
    )
    if instance is None:
        return ToolResult(data={"error": "Trino is not configured for this profile."})
    try:
        plan = await instance.client.io_plan(sql)
    except TrinoQueryError as exc:
        # A statement EXPLAIN cannot plan is usually one Trino cannot run
        # either, and running it anyway would leave nothing to grade it by.
        return ToolResult(data={"error": f"Trino could not plan the query: {exc}"})
    if plan.output_table is not None:
        return ToolResult(
            data={
                "error": (
                    f"query_trino is read-only; this statement writes to "
                    f"{plan.output_table}."
                )
            }
        )
    try:
        result = await instance.client.execute(sql)
    except TrinoQueryError as exc:
        return ToolResult(data={"error": f"Trino query failed: {exc}"})

    tables = sorted(plan.input_tables)
    tier = instance.config.tier_for_tables(tables)
    data = {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": len(result.rows),
        "truncated": result.truncated,
    }
    return ToolResult(
        data=data,
        # A statement that reads no table (SELECT 1) returns only what the
        # model wrote; the tool's floor still applies.
        provenance=tier if tier is not None else SourceTrustTier.RECOGNIZED_MACHINE,
        provenance_reason=(
            f"Trino query read {', '.join(tables)}."
            if tables
            else "Trino query read no tables."
        ),
    )


TRINO_TOOLS: tuple[ToolRegistration, ...] = (
    ToolRegistration(
        definition=QUERY_TRINO_DEFINITION,
        implementation=query_trino_tool,
        metadata=make_local_tool_metadata(
            [
                ToolTag.READ_ONLY,
                ToolTag.SENSITIVE_DATA,
                ToolTag.DATA,
                ToolTag.OUTPUT_UNTRUSTED,
            ],
            cleanest_result_tier=SourceTrustTier.RECOGNIZED_MACHINE,
        ),
    ),
)
