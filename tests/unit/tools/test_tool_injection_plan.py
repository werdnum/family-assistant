"""Tests for context injection into local tool functions.

This module mirrors how tool modules are written: ``from __future__ import
annotations`` with the injected types imported only under ``TYPE_CHECKING``, so
none of them is a runtime global here.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.tools import types as tool_types
from family_assistant.tools.catalog import AVAILABLE_FUNCTIONS
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    build_injection_plan,
)
from tests.mocks.unconnected_database import unconnected_database

if TYPE_CHECKING:
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.storage.database import Database, DatabaseExecutor
    from family_assistant.tools.types import CalendarConfig, ToolExecutionContext


def test_type_checking_only_annotations_are_injected() -> None:
    async def tool(
        exec_context: ToolExecutionContext,
        db_context: Database,
        embedding_generator: EmbeddingGenerator | None,
        calendar_config: CalendarConfig,
        query: str,
    ) -> str:
        return query

    plan = build_injection_plan(tool)

    assert (
        plan.exec_context,
        plan.db_context,
        plan.embedding_generator,
        plan.requires_embedding_generator,
        plan.calendar_config,
    ) == (True, True, True, True, True)


def test_injection_requires_matching_annotation_type() -> None:
    async def tool(exec_context: str, db_context: DatabaseExecutor) -> str:
        return exec_context

    plan = build_injection_plan(tool)

    assert (plan.exec_context, plan.db_context) == (False, False)


@pytest.mark.parametrize(
    ("tool_name", "implementation"),
    [
        pytest.param(name, implementation, id=name)
        for name, implementation in AVAILABLE_FUNCTIONS.items()
        if "exec_context" in inspect.signature(implementation).parameters
    ],
)
def test_catalog_tools_with_exec_context_get_it_injected(
    tool_name: str,
    implementation: Any,  # noqa: ANN401 - catalog implementations have varied signatures
) -> None:
    assert build_injection_plan(implementation).exec_context, tool_name


@pytest.mark.asyncio
async def test_execute_tool_injects_without_logging_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    received: list[ToolExecutionContext] = []

    async def tool(exec_context: ToolExecutionContext, query: str) -> str:
        received.append(exec_context)
        return f"Got {query}"

    provider = LocalToolsProvider(
        definitions=[
            {
                "type": "function",
                "function": {
                    "name": "tool",
                    "description": "Test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ],
        implementations={"tool": tool},
    )
    context = tool_types.ToolExecutionContext(
        conversation_id="test-conv",
        user_name="test-user",
        interface_type="test",
        timezone=ZoneInfo("UTC"),
        turn_id=None,
        db_context=unconnected_database(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        result = await provider.execute_tool("tool", {"query": "x"}, context)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert (result, received, warnings) == ("Got x", [context], [])
