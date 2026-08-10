"""Functional tests for script testing with simulated tools."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, KeychuteConfig
from family_assistant.llm.messages import AssistantMessage, ToolMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.metadata import (
    ToolRegistration,
    make_local_tool_metadata,
)
from family_assistant.tools.script_testing import (
    ScriptTestingToolsProvider,
    test_script_with_simulated_tools_tool,
)
from family_assistant.tools.types import ToolExecutionContext, ToolResult

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


def _build_tools_provider_from_registrations(
    *registrations: ToolRegistration,
) -> CompositeToolsProvider:
    """Create a provider from explicit test registrations."""
    local_provider = LocalToolsProvider(registrations=list(registrations))
    return CompositeToolsProvider([local_provider])


def _build_exec_context(
    *,
    db: Database,
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
        credential_resolvers=None,
        api_backend=None,
    )


async def _store_tool_history_example(
    *,
    db: Database,
    conversation_id: str,
    user_id: str,
    tool_name: str,
    arguments: dict[str, object],
    result_content: str,
    subconversation_id: str | None = None,
    timestamp: datetime | None = None,
    interface_type: str = "test",
) -> None:
    """Persist an assistant tool call and matching tool result for few-shot retrieval."""
    timestamp = timestamp or datetime.now(UTC)
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
        interface_type=interface_type,
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
        interface_type=interface_type,
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
    db = Database(engine=db_engine)
    await db.notes.add_or_update(
        title="Trip Checklist",
        content="Passport and tickets",
        include_in_prompt=True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
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
        script_result["created"] == "Note 'Packing List' has been created successfully."
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
    db = Database(engine=db_engine)
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
    db = Database(engine=db_engine)
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
async def test_script_testing_does_not_fall_back_across_users(
    db_engine: AsyncEngine,
) -> None:
    """Fallback retrieval must not use examples from a different user."""
    db = Database(engine=db_engine)
    await _store_tool_history_example(
        db=db,
        conversation_id="conv-other-user",
        user_id="user-2",
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
        assert "Historical examples from persisted tool history:" in prompt
        assert "Note 'Weekly Plan' has been created successfully." not in prompt
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
            scenario_description="Simulate note writes without any history examples.",
        )

    assert isinstance(result.data, dict)
    transcript = result.data["transcript"]
    assert transcript[0]["historical_examples_used"] == 0


@pytest.mark.asyncio
async def test_script_testing_same_user_fallback_stays_on_same_interface(
    db_engine: AsyncEngine,
) -> None:
    """Same-user history fallback must not cross interface types."""
    db = Database(engine=db_engine)
    await _store_tool_history_example(
        db=db,
        conversation_id="conv-telegram",
        user_id="user-1",
        tool_name="add_or_update_note",
        arguments={
            "title": "Weekly Plan",
            "content": "Groceries and school pickup",
        },
        result_content="Note 'Weekly Plan' has been created successfully.",
        interface_type="telegram",
    )

    tools_provider = _build_tools_provider("add_or_update_note")
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-web",
        user_id="user-1",
    )

    def handler(
        prompt: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        assert "Historical examples from persisted tool history:" in prompt
        assert "Note 'Weekly Plan' has been created successfully." not in prompt
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
            scenario_description="Simulate note writes without cross-interface history.",
        )

    assert isinstance(result.data, dict)
    transcript = result.data["transcript"]
    assert transcript[0]["historical_examples_used"] == 0


@pytest.mark.asyncio
async def test_script_testing_keeps_subconversation_history_isolated(
    db_engine: AsyncEngine,
) -> None:
    """Examples from other subconversations must not leak into the current run."""
    db = Database(engine=db_engine)
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
            return_value_json=json.dumps("Message sent successfully to family-chat.")
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
    db = Database(engine=db_engine)
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
    db = Database(engine=db_engine)
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


@pytest.mark.asyncio
async def test_script_testing_formats_non_json_passthrough_results_in_transcript(
    db_engine: AsyncEngine,
) -> None:
    """Transcript rendering should not fail on passthrough results with non-JSON-native data."""

    async def passthrough_snapshot_tool(
        exec_context: ToolExecutionContext,
    ) -> ToolResult:
        del exec_context
        return ToolResult(
            data={
                "captured_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            }
        )

    tools_provider = _build_tools_provider_from_registrations(
        ToolRegistration(
            definition={
                "type": "function",
                "function": {
                    "name": "passthrough_snapshot",
                    "description": "Return a snapshot timestamp.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
            implementation=passthrough_snapshot_tool,
            metadata=make_local_tool_metadata(["read_only"]),
        )
    )

    db = Database(engine=db_engine)
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-transcript",
        user_id="user-1",
    )

    result = await test_script_with_simulated_tools_tool(
        exec_context,
        script="""
passthrough_snapshot()
"ok"
""",
        scenario_description="Run the read-only tool for real.",
    )

    assert result.text is not None
    assert "[passthrough] passthrough_snapshot" in result.text
    assert "2026-01-02" in result.text
    assert isinstance(result.data, dict)
    transcript = result.data["transcript"]
    assert transcript[0]["mode"] == "passthrough"


@pytest.mark.asyncio
async def test_script_testing_serializes_non_json_passthrough_results_in_simulation_prompt(
    db_engine: AsyncEngine,
) -> None:
    """Simulation prompts should tolerate prior passthrough results with non-JSON data."""

    async def passthrough_snapshot_tool(
        exec_context: ToolExecutionContext,
    ) -> ToolResult:
        del exec_context
        return ToolResult(data={"status": "ok"})

    registrations_by_name = {
        registration.name: registration for registration in LOCAL_TOOL_REGISTRATIONS
    }
    tools_provider = _build_tools_provider_from_registrations(
        ToolRegistration(
            definition={
                "type": "function",
                "function": {
                    "name": "passthrough_snapshot",
                    "description": "Return a snapshot timestamp.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
            implementation=passthrough_snapshot_tool,
            metadata=make_local_tool_metadata(["read_only"]),
        ),
        registrations_by_name["send_message_to_user"],
    )

    db = Database(engine=db_engine)
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-transcript-prompt",
        user_id="user-1",
    )

    def handler(
        prompt: str,
        response_model: type[BaseModel],
    ) -> BaseModel:
        assert '"captured_at": "2026-01-02 03:04:05+00:00"' in prompt
        return response_model(
            return_value_json=json.dumps("Message queued for sibling-chat.")
        )

    fake_client = FakeStructuredClient(handler)
    original_append_transcript = ScriptTestingToolsProvider._append_transcript

    def patched_append_transcript(
        self: ScriptTestingToolsProvider,
        *,
        tool_name: str,
        mode: Literal["passthrough", "simulated"],
        arguments: object,
        result: str | ToolResult,
        classification_reason: str,
        historical_examples_used: int,
    ) -> None:
        original_append_transcript(
            self,
            tool_name=tool_name,
            mode=mode,
            arguments=cast("dict[str, Any]", arguments),
            result=result,
            classification_reason=classification_reason,
            historical_examples_used=historical_examples_used,
        )
        if (
            mode == "passthrough"
            and self._transcript[-1].tool_name == "passthrough_snapshot"
        ):
            self._transcript[-1].result = {
                "captured_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            }

    with (
        patch(
            "family_assistant.llm.one_shot.LLMClientFactory.create_client",
            return_value=fake_client,
        ),
        patch.object(
            ScriptTestingToolsProvider,
            "_append_transcript",
            patched_append_transcript,
        ),
    ):
        result = await test_script_with_simulated_tools_tool(
            exec_context,
            script="""
passthrough_snapshot()
send_message_to_user(
    target_chat_id="sibling-chat",
    message_content="Dinner is ready",
)
""",
            scenario_description=(
                "Run the snapshot tool for real, then simulate the outbound message."
            ),
        )

    assert isinstance(result.data, dict)
    transcript = result.data["transcript"]
    assert [entry["mode"] for entry in transcript] == ["passthrough", "simulated"]


@pytest.mark.asyncio
async def test_script_testing_exposes_script_errors_in_structured_result(
    db_engine: AsyncEngine,
) -> None:
    """Structured output should preserve script failures for machine consumers."""
    db = Database(engine=db_engine)
    tools_provider = _build_tools_provider("send_message_to_user")
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-script-error",
        user_id="user-1",
    )

    result = await test_script_with_simulated_tools_tool(
        exec_context,
        script="""
if True
    pass
""",
        scenario_description="This script should fail before any tool calls.",
    )

    assert result.text is not None
    assert "Error: Syntax error in script" in result.text
    assert isinstance(result.data, dict)
    assert result.data["status"] == "error"
    assert result.data["script_result_text"] is not None
    assert "Syntax error in script" in result.data["script_result_text"]
    assert result.data["error"] == result.data["script_result"]["error"]
    assert result.data["script_result"]["status"] == "error"
    assert result.data["script_result"]["error_type"] == "syntax_error"
    assert "Syntax error in script" in result.data["script_result"]["error"]
    assert result.data["transcript"] == []


@pytest.mark.asyncio
async def test_script_testing_returns_structured_error_when_tools_provider_missing(
    db_engine: AsyncEngine,
) -> None:
    """Tool-level setup failures should still return the normal structured envelope."""
    db = Database(engine=db_engine)
    tools_provider = _build_tools_provider("send_message_to_user")
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-missing-provider",
        user_id="user-1",
    )
    exec_context = replace(
        exec_context,
        tools_provider=None,
        processing_service=None,
    )

    result = await test_script_with_simulated_tools_tool(
        exec_context,
        script='{"ok": true}',
        scenario_description="No tools provider is available in this context.",
    )

    assert result.text == (
        "Error: test_script_with_simulated_tools requires an available tools provider."
    )
    assert isinstance(result.data, dict)
    assert result.data == {
        "status": "error",
        "script_result": None,
        "script_result_text": result.text,
        "error": (
            "test_script_with_simulated_tools requires an available tools provider."
        ),
        "transcript": [],
    }


@pytest.mark.asyncio
async def test_script_testing_does_not_expose_keychute(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The simulation harness cannot make a real brokered HTTP request."""
    db = Database(engine=db_engine)
    tools_provider = _build_tools_provider("get_note")
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-keychute-simulation",
        user_id="user-1",
    )
    assert exec_context.processing_service is not None
    exec_context.processing_service.app_config = AppConfig(
        keychute_config=KeychuteConfig(
            enabled=True,
            url="https://keychute.test",
            token="client-token",
        )
    )
    request = AsyncMock(side_effect=AssertionError("real Keychute call attempted"))
    monkeypatch.setattr(
        "family_assistant.scripting.apis.keychute.KeychuteScriptHttpClient.request",
        request,
    )

    result = await test_script_with_simulated_tools_tool(
        exec_context,
        script='keychute_http_request("weather", "https://example.test")',
        scenario_description="No external request should be sent.",
    )

    assert isinstance(result.data, dict)
    assert result.data["status"] == "error"
    assert result.data["transcript"] == []
    assert "keychute_http_request" in result.data["error"]
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_script_testing_returns_structured_error_for_invalid_overrides(
    db_engine: AsyncEngine,
) -> None:
    """Invalid passthrough/simulation override requests should be machine-readable."""
    db = Database(engine=db_engine)
    tools_provider = _build_tools_provider("send_message_to_user")
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-invalid-override",
        user_id="user-1",
    )

    result = await test_script_with_simulated_tools_tool(
        exec_context,
        script='{"ok": true}',
        scenario_description="This run should fail before execution.",
        simulated_tools=["send_message_to_user"],
        passthrough_tools=["send_message_to_user"],
    )

    assert result.text == (
        "Error: Tools cannot be both simulated and passthrough: send_message_to_user"
    )
    assert isinstance(result.data, dict)
    assert result.data == {
        "status": "error",
        "script_result": None,
        "script_result_text": result.text,
        "error": (
            "Tools cannot be both simulated and passthrough: send_message_to_user"
        ),
        "transcript": [],
    }


@pytest.mark.asyncio
async def test_script_testing_does_not_treat_error_like_return_strings_as_failures(
    db_engine: AsyncEngine,
) -> None:
    """A successful script returning an 'Error: ...' string should still be success."""
    db = Database(engine=db_engine)
    tools_provider = _build_tools_provider("send_message_to_user")
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-script-success-string",
        user_id="user-1",
    )

    result = await test_script_with_simulated_tools_tool(
        exec_context,
        script='"Error: harmless value"',
        scenario_description="Return a plain string that happens to start with Error.",
    )

    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert result.data["error"] is None
    assert result.data["script_result"] == "Error: harmless value"
    assert result.data["script_result_text"] == "Script result: Error: harmless value"


@pytest.mark.asyncio
async def test_script_testing_does_not_treat_user_dict_error_shapes_as_failures(
    db_engine: AsyncEngine,
) -> None:
    """A script can legitimately return an error-shaped dict as data."""
    db = Database(engine=db_engine)
    tools_provider = _build_tools_provider("send_message_to_user")
    exec_context = _build_exec_context(
        db=db,
        tools_provider=tools_provider,
        conversation_id="conv-script-success-dict",
        user_id="user-1",
    )

    result = await test_script_with_simulated_tools_tool(
        exec_context,
        script='{"status": "error", "error": "harmless value"}',
        scenario_description=(
            "Return a plain dictionary that happens to look like an error payload."
        ),
    )

    assert isinstance(result.data, dict)
    assert result.data["status"] == "success"
    assert result.data["error"] is None
    assert result.data["script_result"] == {
        "status": "error",
        "error": "harmless value",
    }


@pytest.mark.asyncio
async def test_script_testing_history_examples_respect_internal_id_on_timestamp_ties(
    db_engine: AsyncEngine,
) -> None:
    """History lookup should not use future assistant rows with the same timestamp."""
    db = Database(engine=db_engine)
    shared_timestamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    await _store_tool_history_example(
        db=db,
        conversation_id="conv-timestamp-tie",
        user_id="user-1",
        tool_name="send_message_to_user",
        arguments={
            "target_chat_id": "first-chat",
            "message_content": "First message",
        },
        result_content="Message sent successfully to first-chat.",
        timestamp=shared_timestamp,
    )
    await _store_tool_history_example(
        db=db,
        conversation_id="conv-timestamp-tie",
        user_id="user-1",
        tool_name="send_message_to_user",
        arguments={
            "target_chat_id": "second-chat",
            "message_content": "Second message",
        },
        result_content="Message sent successfully to second-chat.",
        timestamp=shared_timestamp,
    )

    examples = await db.message_history.get_recent_tool_examples(
        interface_type="test",
        conversation_id="conv-timestamp-tie",
        subconversation_id=None,
        tool_name="send_message_to_user",
        limit=2,
        user_id="user-1",
    )

    assert [example.arguments["target_chat_id"] for example in examples] == [
        "second-chat",
        "first-chat",
    ]
