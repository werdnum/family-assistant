"""Functional tests for surfacing tool errors from scheduled script runs."""

import asyncio
from collections.abc import Callable
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority
from family_assistant.task_worker import TaskWorker, handle_script_execution
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient
from tests.mocks.recording_chat_interface import RecordingChatInterface

FAILING_TOOL_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "failing_tool",
            "description": "Always reports an error.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def failing_tool(exec_context: ToolExecutionContext) -> ToolResult:
    del exec_context
    return ToolResult(data={"error": "upstream unavailable"})


def _processing_service() -> ProcessingService:
    tools_provider = CompositeToolsProvider(
        providers=[
            LocalToolsProvider(
                definitions=FAILING_TOOL_DEFINITION,
                implementations={"failing_tool": failing_tool},
            )
        ]
    )
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="N/A")
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="event_handler",
            prompts={"system_prompt": "Event handler"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=0,
            history_min_turns=0,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )


@pytest.mark.asyncio
async def test_script_run_records_tool_errors_on_finished_task(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """A script that ignores a tool error still finishes, with the error noted."""
    worker, new_task_event, _ = task_worker_manager(
        _processing_service(), RecordingChatInterface()
    )
    worker.register_task_handler("script_execution", handle_script_execution)

    db = Database(engine=db_engine)
    await db.tasks.enqueue(
        task_id="script-with-tool-error",
        task_type="script_execution",
        payload={
            "script_code": "failing_tool()\n",
            "conversation_id": "test_conv",
            "interface_type": "telegram",
        },
        priority=TaskPriority.BACKGROUND,
    )
    new_task_event.set()
    # The note lives in the task's error column, which this helper otherwise
    # reads as a failure; the status assertion below is what proves it is not.
    await wait_for_tasks_to_complete(
        db_engine, task_ids={"script-with-tool-error"}, allow_failures=True
    )

    [task] = [
        task
        for task in await db.tasks.get_all(task_type="script_execution")
        if task["task_id"] == "script-with-tool-error"
    ]
    assert task["status"] == "done"
    assert task["error"] == (
        "Completed with 1 tool error(s): failing_tool: upstream unavailable"
    )
