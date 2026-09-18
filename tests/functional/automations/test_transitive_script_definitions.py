"""Definition provenance for statically named stored-script descendants."""

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.definition_resolution import (
    LoadedScriptRef,
    ScheduleAutomationRef,
    resolve_definition_closure,
)
from family_assistant.security.script_closure import (
    ScriptClosureError,
    resolve_script_closure,
)
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.database import Database
from family_assistant.storage.scripts import scripts_table
from family_assistant.task_worker import handle_script_execution
from family_assistant.tools import CompositeToolsProvider, LocalToolsProvider
from family_assistant.tools.execute_script import (
    SCRIPT_TOOLS_DEFINITION,
    execute_script_tool,
)
from family_assistant.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolParametersSchema,
    ToolPropertySchema,
)
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.security.definition_records import DefinitionResolution
    from family_assistant.services.tool_call_review import TriggerReviewInput
    from family_assistant.storage.repositories.scripts import ScriptRow

_PARENT = "update_eink_display"
_CHILD = "inkplate-deploy-image"
_GRANDCHILD = "inkplate-upload-image"


def _tool_definition(
    name: str,
    *,
    properties: dict[str, ToolPropertySchema] | None = None,
    required: list[str] | None = None,
) -> ToolDefinition:
    parameters = ToolParametersSchema(type="object", properties=properties or {})
    if required is not None:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Test tool {name}.",
            "parameters": parameters,
        },
    }


def _processing_service(
    provider: CompositeToolsProvider,
) -> ProcessingService:
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="unused")
        ),
        tools_provider=provider,
        service_config=ProcessingServiceConfig(
            id="script_profile",
            prompts={"system_prompt": "Script test"},
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


def _handler_context(
    db: Database,
    service: ProcessingService,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id="test_turn",
        db_context=db,
        processing_service=service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


def _tainted_state() -> TurnTaintState:
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="message-1",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Inbound email.",
        )
    )


async def _save_script(
    db: Database,
    *,
    name: str,
    source: str,
    taint_state: TurnTaintState | None = None,
) -> "ScriptRow":
    return await db.scripts.save(
        name=name,
        description=f"Stored script {name}",
        script_code=source,
        definition_taint_state=taint_state or TurnTaintState.empty(),
    )


async def _create_script_automation(db: Database, script_name: str) -> int:
    return await db.schedule_automations.create(
        name=f"Run {script_name}",
        recurrence_rule="FREQ=DAILY",
        action_type="script",
        action_config={"script_name": script_name},
        conversation_id="test_conv",
        timezone=ZoneInfo("UTC"),
        definition_taint_state=TurnTaintState.empty(),
    )


async def _save_production_shaped_chain(
    db: Database,
    *,
    grandchild_taint: TurnTaintState | None = None,
    grandchild_name: str = _GRANDCHILD,
) -> tuple[int, "ScriptRow"]:
    if grandchild_name == _GRANDCHILD:
        await _save_script(
            db,
            name=_GRANDCHILD,
            source='print("uploaded")',
            taint_state=grandchild_taint,
        )
    await _save_script(
        db,
        name=_CHILD,
        source=f"execute_script(name='{grandchild_name}')",
    )
    parent = await _save_script(
        db,
        name=_PARENT,
        source="execute_script(name='inkplate-deploy-image')",
    )
    automation_id = await _create_script_automation(db, _PARENT)
    return automation_id, parent


async def _resolve_chain(
    db: Database,
    automation_id: int,
    parent: "ScriptRow",
) -> "DefinitionResolution":
    return await resolve_definition_closure(
        db,
        (
            ScheduleAutomationRef(automation_id=automation_id),
            LoadedScriptRef(script=parent),
        ),
    )


@pytest.mark.asyncio
async def test_production_eink_script_chain_resolves_transitively(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    automation_id, parent = await _save_production_shaped_chain(db)

    resolution = await _resolve_chain(db, automation_id, parent)

    assert resolution.resolved


@pytest.mark.asyncio
async def test_missing_grandchild_is_not_blessed_by_parent_stamps(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    automation_id, parent = await _save_production_shaped_chain(
        db, grandchild_name="missing-inkplate-uploader"
    )

    with pytest.raises(
        ScriptClosureError,
        match="Static script dependency 'missing-inkplate-uploader' not found",
    ):
        await _resolve_chain(db, automation_id, parent)


@pytest.mark.asyncio
async def test_mutated_grandchild_is_not_blessed_by_parent_stamps(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    automation_id, parent = await _save_production_shaped_chain(db)
    await db.execute(
        update(scripts_table)
        .where(scripts_table.c.name == _GRANDCHILD)
        .values(script_code='print("mutated after stamping")')
    )

    resolution = await _resolve_chain(db, automation_id, parent)

    assert not resolution.resolved


@pytest.mark.asyncio
async def test_tainted_grandchild_is_not_blessed_by_parent_stamps(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    automation_id, parent = await _save_production_shaped_chain(
        db, grandchild_taint=_tainted_state()
    )

    resolution = await _resolve_chain(db, automation_id, parent)

    assert not resolution.resolved


@pytest.mark.asyncio
async def test_dynamic_child_name_remains_outside_the_static_closure(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    parent = await _save_script(
        db,
        name=_PARENT,
        source=(
            "child_name = 'runtime-selected-script'\nexecute_script(name=child_name)"
        ),
    )

    resolution = await resolve_definition_closure(db, [LoadedScriptRef(parent)])

    assert resolution.resolved


@pytest.mark.asyncio
async def test_trusted_descendants_cannot_supply_missing_invocation_provenance(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _save_script(
        db,
        name=_CHILD,
        source='print("trusted child")',
    )
    closure = await resolve_script_closure(
        db, "execute_script(name='inkplate-deploy-image')"
    )

    resolution = await resolve_definition_closure(
        db,
        (),
        script_closure=closure,
    )

    assert closure.resolution is not None and closure.resolution.resolved
    assert not resolution.resolved


@pytest.mark.asyncio
async def test_static_script_cycle_terminates_with_each_definition_resolved(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    parent = await _save_script(
        db,
        name=_PARENT,
        source="execute_script(name='inkplate-deploy-image')",
    )
    await _save_script(
        db,
        name=_CHILD,
        source="execute_script(name='update_eink_display')",
    )

    closure = await resolve_script_closure(db, parent.script_code, loaded_root=parent)
    resolution = await resolve_definition_closure(
        db,
        [LoadedScriptRef(parent)],
        script_closure=closure,
    )

    assert tuple(binding.name for binding in closure.bindings) == (_CHILD, _PARENT)
    assert resolution.resolved


@pytest.mark.asyncio
async def test_shared_static_descendant_is_included_once(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _save_script(
        db,
        name="left-branch",
        source="execute_script(name='shared-descendant')",
    )
    await _save_script(
        db,
        name="right-branch",
        source="execute_script(name='shared-descendant')",
    )
    await _save_script(
        db,
        name="shared-descendant",
        source='print("shared")',
    )
    parent = await _save_script(
        db,
        name=_PARENT,
        source=(
            "execute_script(name='left-branch')\nexecute_script(name='right-branch')"
        ),
    )

    closure = await resolve_script_closure(db, parent.script_code, loaded_root=parent)
    resolution = await resolve_definition_closure(
        db,
        [LoadedScriptRef(parent)],
        script_closure=closure,
    )

    assert tuple(binding.name for binding in closure.bindings) == (
        "left-branch",
        "right-branch",
        "shared-descendant",
    )
    assert resolution.resolved


@pytest.mark.asyncio
async def test_scheduled_handler_trigger_is_unresolved_for_tainted_grandchild(
    db_engine: AsyncEngine,
) -> None:
    captured: list[TriggerReviewInput | None] = []

    async def capture_trigger(exec_context: ToolExecutionContext) -> str:
        captured.append(exec_context.tool_call_review_trigger)
        return "captured"

    provider = CompositeToolsProvider(
        providers=[
            LocalToolsProvider(
                definitions=[
                    *SCRIPT_TOOLS_DEFINITION,
                    _tool_definition("capture_trigger"),
                ],
                implementations={
                    "execute_script": execute_script_tool,
                    "capture_trigger": capture_trigger,
                },
            )
        ]
    )
    service = _processing_service(provider)
    db = Database(engine=db_engine)
    await _save_script(
        db,
        name=_GRANDCHILD,
        source="capture_trigger()",
        taint_state=_tainted_state(),
    )
    await _save_script(
        db,
        name=_CHILD,
        source=f"execute_script(name='{_GRANDCHILD}')",
    )
    await _save_script(
        db,
        name=_PARENT,
        source=f"execute_script(name='{_CHILD}')",
    )
    automation_id = await _create_script_automation(db, _PARENT)

    await handle_script_execution(
        _handler_context(db, service),
        {
            "script_name": _PARENT,
            "automation_id": str(automation_id),
            "automation_type": "schedule",
            "conversation_id": "test_conv",
            "config": {"script_name": _PARENT},
        },
    )

    assert len(captured) == 1
    trigger = captured[0]
    assert trigger is not None
    assert trigger.definition_taint_metadata is None


@pytest.mark.asyncio
async def test_scheduled_handler_rejects_child_changed_after_closure_capture(
    db_engine: AsyncEngine,
) -> None:
    effects: list[str] = []
    child_results: list[object] = []

    async def mutate_child(exec_context: ToolExecutionContext) -> str:
        await _save_script(
            exec_context.db_context,
            name="bound-child",
            source='record_effect()\nprint("changed")',
        )
        return "mutated"

    async def record_effect() -> str:
        effects.append("ran")
        return "ran"

    async def capture_result(value: object) -> str:
        child_results.append(value)
        return "captured"

    provider = CompositeToolsProvider(
        providers=[
            LocalToolsProvider(
                definitions=[
                    *SCRIPT_TOOLS_DEFINITION,
                    _tool_definition("mutate_child"),
                    _tool_definition("record_effect"),
                    _tool_definition(
                        "capture_result",
                        properties={"value": {"type": "object"}},
                        required=["value"],
                    ),
                ],
                implementations={
                    "execute_script": execute_script_tool,
                    "mutate_child": mutate_child,
                    "record_effect": record_effect,
                    "capture_result": capture_result,
                },
            )
        ]
    )
    service = _processing_service(provider)
    db = Database(engine=db_engine)
    await _save_script(db, name="bound-child", source="record_effect()")
    await _save_script(
        db,
        name=_PARENT,
        source=(
            "mutate_child()\n"
            "child_result = execute_script(name='bound-child')\n"
            "capture_result(value=child_result)"
        ),
    )
    automation_id = await _create_script_automation(db, _PARENT)

    await handle_script_execution(
        _handler_context(db, service),
        {
            "script_name": _PARENT,
            "automation_id": str(automation_id),
            "automation_type": "schedule",
            "conversation_id": "test_conv",
            "config": {"script_name": _PARENT},
        },
    )

    assert effects == []
    assert len(child_results) == 1
    result = child_results[0]
    assert isinstance(result, dict)
    assert result.get("error_type") == "stale_script_binding"
