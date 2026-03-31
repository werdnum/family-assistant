"""Functional tests for script testing with simulated tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.messages import AssistantMessage, ToolMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.script_testing import test_script_with_simulated_tools_tool
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncEngine


class FakeStructuredClient:
    """Minimal fake LLM client for structured simulation responses."""

    def __init__(self, handler) -> None:  # noqa: ANN001
        self._handler = handler
        self.prompts: list[str] = []

    async def generate_structured(
        self,
        messages: list[Any],
        response_model: type[BaseModel],
    ) -> BaseModel:
        prompt = messages[-1].content
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        return self._handler(prompt, response_model)


def _build_tools_provider(*tool_names: str) -> CompositeToolsProvider:
    """Create a provider backed by real registered tool implementations."""
    registrations_by_name = {
        registration.name: registration for registration in LOCAL_TOOL_REGISTRATIONS
    }
    selected_registrations = [registrations_by_name[name] for name in tool_names]
    local_provider = LocalToolsProvider(registrations=selected_registrations)
    return CompositeToolsProvider([local_provider])


def _build_exec_context(
    *,
    db: DatabaseContext,
    tools_provider: CompositeToolsProvider,
    conversation_id: str,
    user_id: str,
    subconversation_id: str | None = None,
    chat_interface: AsyncMock | None = None,
) -> ToolExecutionContext:
    """Create a tool execution context with a real tools provider."""
    processing_service = Mock()
    processing_service.tools_provider = tools_provider

    typed_chat_interface = cast("Any", chat_interface)
    chat_interfaces = (
        cast("Any", {"test": chat_interface}) if chat_interface is not None else None
    )

    return ToolExecutionContext(
        interface_type="test",
        conversation_id=conversation_id,
        user_name="Alex",
        user_id=user_id,
        turn_id="turn-123",
        db_context=db,
        processing_service=processing_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        chat_interface=typed_chat_interface,
        chat_interfaces=chat_interfaces,
        subconversation_id=subconversation_id,
    )


async def _store_tool_history_example(
    *,
    db: DatabaseContext,
    conversation_id: str,
    user_id: str,
    tool_name: str,
    arguments: dict[str, object],
    result_content: str,
    subconversation_id: str | None = None,
) -> None:
    """Persist an assistant tool call and matching tool result for few-shot retrieval."""
    timestamp = datetime.now(UTC)
    tool_call_id = f"call-{conversation_id}-{subconversation_id or 'main'}-{tool_name}"

    await db.message_history.add_message(
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id=tool_call_id,
                    type="function",
                    function=ToolCallFunction(
                        name=tool_name,
                        arguments=json.dumps(arguments),
                    ),
                )
            ],
        ),
        interface_type="test",
        conversation_id=conversation_id,
        timestamp=timestamp,
        user_id=user_id,
        subconversation_id=subconversation_id,
    )
    await db.message_history.add_message(
        ToolMessage(
            tool_call_id=tool_call_id,
            name=tool_name,
            content=result_content,
        ),
        interface_type="test",
        conversation_id=conversation_id,
        timestamp=timestamp,
        user_id=user_id,
        subconversation_id=subconversation_id,
    )


@pytest.mark.asyncio
async def test_script_testing_simulates_actions_and_keeps_reads_real(
    db_engine: AsyncEngine,
) -> None:
    """Action tools should be simulated while read-only tools pass through."""
    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Trip Checklist",
            content="Passport and tickets",
            include_in_prompt=True,
        )

        tools_provider = _build_tools_provider(
            "get_note",
            "add_or_update_note",
            "send_message_to_user",
        )
        chat_interface = AsyncMock()
        exec_context = _build_exec_context(
            db=db,
            tools_provider=tools_provider,
            conversation_id="conv-script-test",
            user_id="user-1",
            chat_interface=chat_interface,
        )

        fake_client = FakeStructuredClient(
            lambda prompt, response_model: response_model(
                return_value_json=(
                    json.dumps("Note 'Packing List' has been created successfully.")
                    if "Tool name: add_or_update_note" in prompt
                    else json.dumps("Message queued for sibling-chat.")
                )
            )
        )

        with patch(
            "family_assistant.llm.one_shot.LLMClientFactory.create_client",
            return_value=fake_client,
        ):
            result = await test_script_with_simulated_tools_tool(
                exec_context,
                script="""
existing = get_note(title="Trip Checklist")
created = add_or_update_note(title="Packing List", content="Passport, chargers")
notified = send_message_to_user(
    target_chat_id="sibling-chat",
    message_content="Dinner is ready",
)
{
    "existing": existing,
    "created": created,
    "notified": notified,
}
""",
                scenario_description=(
                    "Simulate successful writes and outbound messages, but do not actually "
                    "change state or send anything."
                ),
            )

        assert isinstance(result.data, dict)
        script_result = result.data["script_result"]
        transcript = result.data["transcript"]

        assert script_result["existing"]["exists"] is True
        assert script_result["existing"]["title"] == "Trip Checklist"
        assert (
            script_result["created"]
            == "Note 'Packing List' has been created successfully."
        )
        assert script_result["notified"] == "Message queued for sibling-chat."

        created_note = await db.notes.get_by_title(
            "Packing List",
            visibility_grants=None,
        )
        assert created_note is None
        assert chat_interface.mock_calls == []

        assert [entry["mode"] for entry in transcript] == [
            "passthrough",
            "simulated",
            "simulated",
        ]
        assert [entry["tool_name"] for entry in transcript] == [
            "get_note",
            "add_or_update_note",
            "send_message_to_user",
        ]


@pytest.mark.asyncio
async def test_script_testing_uses_same_conversation_history_examples(
    db_engine: AsyncEngine,
) -> None:
    """Simulation should pull few-shot examples from the same conversation first."""
    async with DatabaseContext(engine=db_engine) as db:
        await _store_tool_history_example(
            db=db,
            conversation_id="conv-history",
            user_id="user-1",
            tool_name="send_message_to_user",
            arguments={
                "target_chat_id": "team-chat",
                "message_content": "Status update",
            },
            result_content="Message sent successfully to team-chat.",
        )

        tools_provider = _build_tools_provider("send_message_to_user")
        chat_interface = AsyncMock()
        exec_context = _build_exec_context(
            db=db,
            tools_provider=tools_provider,
            conversation_id="conv-history",
            user_id="user-1",
            chat_interface=chat_interface,
        )

        def handler(
            prompt: str,
            response_model: type[BaseModel],
        ) -> BaseModel:
            assert "Historical examples from persisted tool history:" in prompt
            assert "Message sent successfully to team-chat." in prompt
            assert '"target_chat_id": "team-chat"' in prompt
            return response_model(
                return_value_json=json.dumps("Message sent successfully to team-chat.")
            )

        fake_client = FakeStructuredClient(handler)

        with patch(
            "family_assistant.llm.one_shot.LLMClientFactory.create_client",
            return_value=fake_client,
        ):
            result = await test_script_with_simulated_tools_tool(
                exec_context,
                script="""
send_message_to_user(
    target_chat_id="team-chat",
    message_content="Status update",
)
""",
                scenario_description="Pretend the message send succeeded.",
            )

        assert isinstance(result.data, dict)
        transcript = result.data["transcript"]
        assert transcript[0]["mode"] == "simulated"
        assert transcript[0]["historical_examples_used"] == 1
        assert chat_interface.mock_calls == []


@pytest.mark.asyncio
async def test_script_testing_falls_back_to_same_user_history(
    db_engine: AsyncEngine,
) -> None:
    """When the conversation has no examples, simulation should fall back to the same user."""
    async with DatabaseContext(engine=db_engine) as db:
        await _store_tool_history_example(
            db=db,
            conversation_id="conv-old",
            user_id="user-1",
            tool_name="add_or_update_note",
            arguments={
                "title": "Weekly Plan",
                "content": "Groceries and school pickup",
            },
            result_content="Note 'Weekly Plan' has been created successfully.",
        )

        tools_provider = _build_tools_provider("add_or_update_note")
        exec_context = _build_exec_context(
            db=db,
            tools_provider=tools_provider,
            conversation_id="conv-new",
            user_id="user-1",
        )

        def handler(
            prompt: str,
            response_model: type[BaseModel],
        ) -> BaseModel:
            assert "Note 'Weekly Plan' has been created successfully." in prompt
            assert '"title": "Weekly Plan"' in prompt
            return response_model(
                return_value_json=json.dumps(
                    "Note 'Weekly Plan' has been created successfully."
                )
            )

        fake_client = FakeStructuredClient(handler)

        with patch(
            "family_assistant.llm.one_shot.LLMClientFactory.create_client",
            return_value=fake_client,
        ):
            result = await test_script_with_simulated_tools_tool(
                exec_context,
                script="""
add_or_update_note(
    title="Weekly Plan",
    content="Groceries and school pickup",
)
""",
                scenario_description="Simulate note writes using realistic prior outputs.",
            )

        assert isinstance(result.data, dict)
        transcript = result.data["transcript"]
        assert transcript[0]["historical_examples_used"] == 1
        stored_note = await db.notes.get_by_title(
            "Weekly Plan",
            visibility_grants=None,
        )
        assert stored_note is None


@pytest.mark.asyncio
async def test_script_testing_keeps_subconversation_history_isolated(
    db_engine: AsyncEngine,
) -> None:
    """Examples from other subconversations must not leak into the current run."""
    async with DatabaseContext(engine=db_engine) as db:
        await _store_tool_history_example(
            db=db,
            conversation_id="conv-shared",
            user_id="user-1",
            subconversation_id="worker-1",
            tool_name="send_message_to_user",
            arguments={
                "target_chat_id": "delegate-chat",
                "message_content": "Worker update",
            },
            result_content="Message sent successfully to delegate-chat.",
        )
        await _store_tool_history_example(
            db=db,
            conversation_id="conv-fallback",
            user_id="user-1",
            tool_name="send_message_to_user",
            arguments={
                "target_chat_id": "family-chat",
                "message_content": "Dinner at 6",
            },
            result_content="Message sent successfully to family-chat.",
        )

        tools_provider = _build_tools_provider("send_message_to_user")
        exec_context = _build_exec_context(
            db=db,
            tools_provider=tools_provider,
            conversation_id="conv-shared",
            user_id="user-1",
        )

        def handler(
            prompt: str,
            response_model: type[BaseModel],
        ) -> BaseModel:
            assert "Message sent successfully to family-chat." in prompt
            assert "Message sent successfully to delegate-chat." not in prompt
            return response_model(
                return_value_json=json.dumps(
                    "Message sent successfully to family-chat."
                )
            )

        fake_client = FakeStructuredClient(handler)

        with patch(
            "family_assistant.llm.one_shot.LLMClientFactory.create_client",
            return_value=fake_client,
        ):
            result = await test_script_with_simulated_tools_tool(
                exec_context,
                script="""
send_message_to_user(
    target_chat_id="family-chat",
    message_content="Dinner at 6",
)
""",
                scenario_description="Pretend the message send succeeded.",
            )

        assert isinstance(result.data, dict)
        transcript = result.data["transcript"]
        assert transcript[0]["historical_examples_used"] == 1


@pytest.mark.asyncio
async def test_script_testing_rejects_action_tool_passthrough_override(
    db_engine: AsyncEngine,
) -> None:
    """Unsafe passthrough overrides should be rejected up front."""
    async with DatabaseContext(engine=db_engine) as db:
        tools_provider = _build_tools_provider("send_message_to_user")
        exec_context = _build_exec_context(
            db=db,
            tools_provider=tools_provider,
            conversation_id="conv-override",
            user_id="user-1",
        )

        result = await test_script_with_simulated_tools_tool(
            exec_context,
            script="send_message_to_user(target_chat_id='chat-1', message_content='hi')",
            scenario_description="This should not run.",
            passthrough_tools=["send_message_to_user"],
        )

        assert result.text is not None
        assert "cannot be forced into passthrough mode" in result.text


@pytest.mark.asyncio
async def test_script_testing_rejects_null_simulated_results(
    db_engine: AsyncEngine,
) -> None:
    """Null simulator outputs should fail fast with a clear error."""
    async with DatabaseContext(engine=db_engine) as db:
        tools_provider = _build_tools_provider("send_message_to_user")
        exec_context = _build_exec_context(
            db=db,
            tools_provider=tools_provider,
            conversation_id="conv-null",
            user_id="user-1",
        )

        fake_client = FakeStructuredClient(
            lambda prompt, response_model: response_model(return_value_json="null")
        )

        with patch(
            "family_assistant.llm.one_shot.LLMClientFactory.create_client",
            return_value=fake_client,
        ):
            result = await test_script_with_simulated_tools_tool(
                exec_context,
                script="""
send_message_to_user(
    target_chat_id="family-chat",
    message_content="Dinner at 6",
)
""",
                scenario_description="Pretend the message send returned null.",
            )

        assert result.text is not None
        assert "returned null" in result.text
