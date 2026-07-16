"""Automation execution logic and scheduling tests."""

import asyncio
import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.actions import ActionType, execute_action
from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.scripting.errors import ScriptError
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import (
    TaskWorker,
    build_script_confirmation_callback,
    handle_script_execution,
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    NOTE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.automations import (
    AUTOMATIONS_TOOLS_DEFINITION,
    create_automation_tool,
    get_automation_stats_tool,
)
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


def _make_processing_service(
    *,
    profile_id: str,
    tools_provider: CompositeToolsProvider,
    registry: dict[str, ProcessingService] | None = None,
) -> ProcessingService:
    """Build a minimal local ProcessingService for profile-resolution tests."""
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="N/A")
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id=profile_id,
            prompts={"system_prompt": profile_id},
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        processing_services_registry=registry,
    )


async def _provider_with_note_tool() -> CompositeToolsProvider:
    provider = CompositeToolsProvider(
        providers=[
            LocalToolsProvider(
                definitions=NOTE_TOOLS_DEFINITION,
                implementations={
                    "add_or_update_note": local_tool_implementations[
                        "add_or_update_note"
                    ],
                },
            )
        ]
    )
    await provider.get_tool_definitions()
    return provider


async def _provider_without_note_tool() -> CompositeToolsProvider:
    provider = CompositeToolsProvider(
        providers=[LocalToolsProvider(definitions=[], implementations={})]
    )
    await provider.get_tool_definitions()
    return provider


def _build_script_exec_context(
    *,
    db_ctx: DatabaseContext,
    conversation_id: str,
    processing_service: ProcessingService,
    processing_profile_id: str | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id=conversation_id,
        user_name="test_user",
        turn_id="test_turn",
        db_context=db_ctx,
        processing_service=processing_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
        processing_profile_id=processing_profile_id,
    )


@pytest.mark.asyncio
async def test_script_executes_under_creating_profile(db_engine: AsyncEngine) -> None:
    """A script runs with the tools of the profile that created the automation,
    not the task worker's default profile."""
    test_run_id = uuid.uuid4()

    # Creating profile has the note tool; the worker's default profile does not.
    creator_service = _make_processing_service(
        profile_id="creator_profile",
        tools_provider=await _provider_with_note_tool(),
    )
    default_service = _make_processing_service(
        profile_id="event_handler",
        tools_provider=await _provider_without_note_tool(),
        registry={"creator_profile": creator_service},
    )

    script_code = f"""
add_or_update_note(title="Provenance {test_run_id}", content="written by script")
"""

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = _build_script_exec_context(
            db_ctx=db_ctx,
            conversation_id=f"prov_{test_run_id}",
            processing_service=default_service,
        )
        await handle_script_execution(
            exec_context,
            {
                "script_code": script_code,
                "conversation_id": f"prov_{test_run_id}",
                "processing_profile_id": "creator_profile",
                "config": {},
            },
        )

    async with DatabaseContext(engine=db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all(visibility_grants=None)
        matching = [n for n in notes if f"Provenance {test_run_id}" in n.title]
        assert len(matching) == 1


@pytest.mark.asyncio
async def test_script_falls_back_to_default_profile_for_legacy_automation(
    db_engine: AsyncEngine,
) -> None:
    """A legacy automation with no recorded profile falls back to the default
    profile, whose tools are then enforced (here, the note tool is unavailable)."""
    test_run_id = uuid.uuid4()

    creator_service = _make_processing_service(
        profile_id="creator_profile",
        tools_provider=await _provider_with_note_tool(),
    )
    default_service = _make_processing_service(
        profile_id="event_handler",
        tools_provider=await _provider_without_note_tool(),
        registry={"creator_profile": creator_service},
    )

    script_code = f"""
add_or_update_note(title="Legacy {test_run_id}", content="should not be written")
"""

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = _build_script_exec_context(
            db_ctx=db_ctx,
            conversation_id=f"legacy_{test_run_id}",
            processing_service=default_service,
        )
        # No processing_profile_id -> falls back to the default profile, which
        # does not expose add_or_update_note, so the script raises.
        with pytest.raises(ScriptError):
            await handle_script_execution(
                exec_context,
                {
                    "script_code": script_code,
                    "conversation_id": f"legacy_{test_run_id}",
                    "config": {},
                },
            )

    async with DatabaseContext(engine=db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all(visibility_grants=None)
        matching = [n for n in notes if f"Legacy {test_run_id}" in n.title]
        assert len(matching) == 0


@pytest.mark.asyncio
async def test_script_with_unresolvable_stamped_profile_fails(
    db_engine: AsyncEngine,
) -> None:
    """An automation explicitly stamped with a non-default profile that is no
    longer registered fails rather than silently downgrading to the default
    profile (which would run with different tools/visibility)."""
    test_run_id = uuid.uuid4()
    default_service = _make_processing_service(
        profile_id="event_handler",
        tools_provider=await _provider_without_note_tool(),
        registry={},
    )

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = _build_script_exec_context(
            db_ctx=db_ctx,
            conversation_id=f"missing_{test_run_id}",
            processing_service=default_service,
        )
        with pytest.raises(RuntimeError, match="cannot be resolved"):
            await handle_script_execution(
                exec_context,
                {
                    "script_code": "x = 1\n",
                    "conversation_id": f"missing_{test_run_id}",
                    "processing_profile_id": "removed_profile",
                    "config": {},
                },
            )


@pytest.mark.asyncio
async def test_create_automation_validates_stored_script_against_profile(
    db_engine: AsyncEngine,
) -> None:
    """An automation referencing a stored script that uses tools unavailable to
    the creating profile is rejected at creation, since the automation will
    execute under that profile."""
    limited_service = _make_processing_service(
        profile_id="limited_profile",
        tools_provider=await _provider_without_note_tool(),
    )

    async with DatabaseContext(engine=db_engine) as db_ctx:
        await db_ctx.scripts.save(
            name="note-writer",
            description="Writes a note",
            script_code='add_or_update_note(title="t", content="c")\n',
        )
        exec_context = _build_script_exec_context(
            db_ctx=db_ctx,
            conversation_id="stored_profile_conv",
            processing_service=limited_service,
        )
        result = await create_automation_tool(
            exec_context=exec_context,
            name="Stored Script Profile Check",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="script",
            action_config={"script_name": "note-writer"},
        )

    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "note-writer" in data["error"]


@pytest.mark.asyncio
async def test_create_automation_accepts_stored_script_matching_profile(
    db_engine: AsyncEngine,
) -> None:
    """The same stored script is accepted when the creating profile has the
    tools it uses."""
    capable_service = _make_processing_service(
        profile_id="capable_profile",
        tools_provider=await _provider_with_note_tool(),
    )

    async with DatabaseContext(engine=db_engine) as db_ctx:
        await db_ctx.scripts.save(
            name="note-writer",
            description="Writes a note",
            script_code='add_or_update_note(title="t", content="c")\n',
        )
        exec_context = _build_script_exec_context(
            db_ctx=db_ctx,
            conversation_id="stored_profile_ok_conv",
            processing_service=capable_service,
        )
        result = await create_automation_tool(
            exec_context=exec_context,
            name="Stored Script Profile OK",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="script",
            action_config={"script_name": "note-writer"},
        )

    data = result.get_data()
    assert isinstance(data, dict)
    assert "id" in data


@pytest.mark.asyncio
async def test_execute_action_script_payload_includes_interface(
    db_engine: AsyncEngine,
) -> None:
    """Queued script actions carry the interface type, so contexts built from
    the payload (and any deferred confirmations they create) record the real
    origin interface instead of the worker's 'unknown_interface' default."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        await execute_action(
            db_ctx=db_ctx,
            action_type=ActionType.SCRIPT,
            action_config={"script_code": "x = 1\n"},
            conversation_id="iface_conv",
            interface_type="telegram",
        )

        rows = await db_ctx.fetch_all(
            select(tasks_table).where(tasks_table.c.task_type == "script_execution")
        )
        assert len(rows) == 1
        payload = rows[0]["payload"]
        assert payload["interface_type"] == "telegram"
        assert payload["conversation_id"] == "iface_conv"


@pytest.mark.asyncio
async def test_script_confirm_gated_tool_defers_to_durable_confirmation(
    db_engine: AsyncEngine,
) -> None:
    """A confirm-gated tool called from a script creates a durable confirmation
    addressed to the automation's owner rather than running immediately."""
    default_service = _make_processing_service(
        profile_id="event_handler",
        tools_provider=await _provider_without_note_tool(),
    )
    callback = build_script_confirmation_callback("owner-user")

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = _build_script_exec_context(
            db_ctx=db_ctx,
            conversation_id="confirm_conv",
            processing_service=default_service,
            processing_profile_id="creator_profile",
        )
        outcome = await callback(
            interface_type="web",
            conversation_id="confirm_conv",
            turn_id="test_turn",
            tool_name="delete_calendar_event",
            call_id="call-1",
            tool_args={"event_id": "evt-123"},
            timeout_seconds=3600.0,
            context=exec_context,
        )

        assert outcome.kind == "completed"
        assert isinstance(outcome.result, str)
        assert "hasn't run yet" in outcome.result

        pending = await db_ctx.confirmation_requests.list_pending_for_user("owner-user")
        assert len(pending) == 1
        assert pending[0]["tool_name"] == "delete_calendar_event"
        # The confirmation records the creating profile and origin conversation
        # so the deferred execution runs under the same profile and acts in the
        # requesting conversation rather than the worker's placeholder context.
        assert pending[0]["processing_profile_id"] == "creator_profile"
        assert pending[0]["origin_interface_type"] == "web"
        assert pending[0]["origin_conversation_id"] == "confirm_conv"


@pytest.mark.asyncio
async def test_script_confirm_gated_tool_without_owner_is_not_run(
    db_engine: AsyncEngine,
) -> None:
    """A confirm-gated tool in a legacy automation with no recorded owner cannot
    be approved, so it is reported as not run and no confirmation is created."""
    default_service = _make_processing_service(
        profile_id="event_handler",
        tools_provider=await _provider_without_note_tool(),
    )
    callback = build_script_confirmation_callback(None)

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = _build_script_exec_context(
            db_ctx=db_ctx,
            conversation_id="confirm_legacy_conv",
            processing_service=default_service,
        )
        outcome = await callback(
            interface_type="web",
            conversation_id="confirm_legacy_conv",
            turn_id="test_turn",
            tool_name="delete_calendar_event",
            call_id="call-1",
            tool_args={"event_id": "evt-123"},
            timeout_seconds=3600.0,
            context=exec_context,
        )

    assert outcome.kind == "failed"
    assert isinstance(outcome.result, str)
    assert "no recorded owner" in outcome.result


@pytest.mark.asyncio
async def test_get_automation_stats_event(db_engine: AsyncEngine) -> None:
    """Test getting execution stats for an event automation."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="stats_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Stats Test",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_stats_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Statistics for automation" in result.get_text()
    assert "Total executions: 0" in result.get_text()


@pytest.mark.asyncio
async def test_get_automation_stats_not_found(db_engine: AsyncEngine) -> None:
    """Test getting stats for non-existent automation returns error."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="stats_fail_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        result = await get_automation_stats_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="event",
        )

    assert "Error:" in result.get_text()
    assert "not found" in result.get_text().lower()


@pytest.mark.asyncio
async def test_event_automation_with_script_execution(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test event automation triggering script execution."""
    test_run_id = uuid.uuid4()

    # Set up tools with note tool for script to use

    local_provider = LocalToolsProvider(
        definitions=AUTOMATIONS_TOOLS_DEFINITION + NOTE_TOOLS_DEFINITION,
        implementations={
            "create_automation": local_tool_implementations["create_automation"],
            "add_or_update_note": local_tool_implementations["add_or_update_note"],
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

    # Create event automation with script
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id=f"event_script_{test_run_id}",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            tools_provider=tools_provider,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        script_code = f"""
def log_event():
    entity = event.get("entity_id", "unknown")
    add_or_update_note(
        title="Event Log {test_run_id}",
        content="Event triggered for " + entity
    )
    return "logged"

log_event()
"""

        result = await create_automation_tool(
            exec_context=exec_context,
            name=f"Event Script {test_run_id}",
            automation_type="event",
            trigger_config={
                "event_source": "home_assistant",
                "event_filter": {
                    "entity_id": f"sensor.test_{test_run_id}",
                    "new_state.state": "on",
                },
            },
            action_type="script",
            action_config={"script_code": script_code, "task_name": "Event Logger"},
        )

        assert "Created event automation" in result.get_text()

    # Set up event processor and task worker
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    processor._running = True
    await processor._refresh_listener_cache()

    processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="N/A")
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="event_handler",
            prompts={"system_prompt": "Event handler"},
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    task_worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)

    # Process event that triggers the automation
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": f"sensor.test_{test_run_id}",
            "old_state": {"state": "off"},
            "new_state": {"state": "on"},
        },
    )

    # Signal worker and wait for processing
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Verify the script created the note
    async with DatabaseContext(engine=db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all(visibility_grants=None)
        matching_notes = [n for n in notes if f"Event Log {test_run_id}" in n.title]
        assert len(matching_notes) == 1
        note = matching_notes[0]
        assert "Event triggered for" in note.content
        assert f"sensor.test_{test_run_id}" in note.content
