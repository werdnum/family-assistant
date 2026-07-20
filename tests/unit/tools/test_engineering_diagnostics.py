"""Tests for engineer-profile diagnostic tools.

Covers the MCP status / reconnect tools, resolved config / profile config
inspection, and system info. Heavy mocking is used because these tools wrap
runtime infrastructure (MCPToolsProvider, ProcessingService, AppConfig) which
isn't trivially constructable in a unit test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    LocalToolsProvider,
    OnDemandToolsView,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.engineering import (
    get_mcp_server_status,
    get_profile_config,
    get_profile_tool_inventory,
    get_resolved_config,
    get_system_info,
    reconnect_mcp_server,
    resolve_tool_policy,
)
from family_assistant.tools.mcp import MCPToolsProvider
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.fixture
def exec_context_with_db() -> ToolExecutionContext:
    """Minimal execution context with a fake db dialect."""
    db = Mock()
    db.engine = Mock()
    db.engine.dialect = Mock()
    db.engine.dialect.name = "sqlite"
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="conv-1",
        user_name="tester",
        turn_id=None,
        db_context=db,
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


# --- get_system_info ---


@pytest.mark.anyio
async def test_get_system_info_returns_runtime_metadata(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    result = await get_system_info(exec_context_with_db)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "python_version" in data
    assert "platform" in data
    assert data["database_dialect"] == "sqlite"


@pytest.mark.anyio
async def test_get_system_info_handles_missing_engine() -> None:
    """If the db_context lacks an engine, dialect falls back to 'unknown'."""
    db = Mock(spec=[])  # no `engine` attribute
    ctx = ToolExecutionContext(
        interface_type="test",
        conversation_id="c",
        user_name="u",
        turn_id=None,
        db_context=db,
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
    result = await get_system_info(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["database_dialect"] == "unknown"


# --- MCP status / reconnect ---


def _make_ctx_with_tools_provider(
    tools_provider: Mock,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="c",
        user_name="u",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        tools_provider=tools_provider,
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.anyio
async def test_get_mcp_server_status_with_no_provider(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    """Without a tools_provider wired in, the tool reports a clear error
    rather than silently pretending all servers are healthy."""
    result = await get_mcp_server_status(exec_context_with_db)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["servers"] == {}


@pytest.mark.anyio
async def test_get_mcp_server_status_aggregates_statuses() -> None:
    """When an MCPToolsProvider is reachable, the tool dumps its statuses
    and computes a summary count."""
    fake_provider = Mock(spec=MCPToolsProvider)
    fake_provider.get_server_statuses.return_value = {
        "code-execution": {
            "status": "connected",
            "transport": "stdio",
            "tool_count": 3,
            "tools": ["create_workspace", "execute_shell", "write_file"],
            "session_active": True,
            "command": "code-execution-mcp",
            "args": [],
            "url": None,
        },
        "broken": {
            "status": "failed",
            "transport": "sse",
            "tool_count": 0,
            "tools": [],
            "session_active": False,
            "command": None,
            "args": [],
            "url": "http://broken.invalid",
        },
    }

    # Patch find_provider_by_type to return our fake provider regardless of
    # the wrapped tools_provider passed in.
    ctx = _make_ctx_with_tools_provider(fake_provider)

    result = await get_mcp_server_status(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["summary"] == {
        "total": 2,
        "connected": 1,
        "failed": 1,
        "cancelled": 0,
        "pending": 0,
    }
    assert "code-execution" in data["servers"]
    assert "broken" in data["servers"]


@pytest.mark.anyio
async def test_reconnect_mcp_server_unknown_id_returns_error() -> None:
    fake_provider = Mock(spec=MCPToolsProvider)
    fake_provider.reconnect_server = AsyncMock(side_effect=KeyError("nope"))
    fake_provider.server_configs = {"a": {}, "b": {}}
    fake_provider.get_server_statuses.return_value = {}

    ctx = _make_ctx_with_tools_provider(fake_provider)
    result = await reconnect_mcp_server(ctx, server_id="nope")
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["configured_servers"] == ["a", "b"]


@pytest.mark.anyio
async def test_reconnect_mcp_server_success() -> None:
    fake_provider = Mock(spec=MCPToolsProvider)
    fake_provider.reconnect_server = AsyncMock(return_value=True)
    fake_provider.server_configs = {"code-execution": {}}
    fake_provider.get_server_statuses.return_value = {
        "code-execution": {"status": "connected", "tool_count": 3},
    }

    ctx = _make_ctx_with_tools_provider(fake_provider)
    result = await reconnect_mcp_server(ctx, server_id="code-execution")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["success"] is True
    assert data["server_id"] == "code-execution"
    assert data["status"]["status"] == "connected"


# --- get_resolved_config / get_profile_config ---


def _ctx_with_app_config(app_config: Mock) -> ToolExecutionContext:
    """Build a context whose processing_service.app_config is the given object."""
    processing_service = Mock()
    processing_service.app_config = app_config
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="c",
        user_name="u",
        turn_id=None,
        db_context=Mock(),
        processing_service=processing_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.anyio
async def test_get_resolved_config_redacts_secrets_and_omits_profile_bodies() -> None:
    fake_app_config = Mock()
    fake_app_config.model_dump.return_value = {
        "default_service_profile_id": "default_assistant",
        "openai_api_key": "sk-secret-value",
        "telegram_token": "telegram-secret",
        "service_profiles": [
            {"id": "default_assistant", "description": "Default"},
            {"id": "engineer", "description": "Engineer"},
        ],
        "default_profile_settings": {"some": "data"},
    }

    ctx = _ctx_with_app_config(fake_app_config)
    result = await get_resolved_config(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    cfg = data["config"]
    assert isinstance(cfg, dict)
    assert cfg["openai_api_key"] == "[REDACTED]"
    assert cfg["telegram_token"] == "[REDACTED]"
    assert "service_profiles" not in cfg  # bodies omitted
    assert "default_profile_settings" not in cfg
    assert data["profile_ids"] == ["default_assistant", "engineer"]
    assert data["profile_count"] == 2


@pytest.mark.anyio
async def test_get_resolved_config_without_processing_service(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    result = await get_resolved_config(exec_context_with_db)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data


@pytest.mark.anyio
async def test_get_profile_config_lists_when_id_omitted() -> None:
    fake_default_settings = Mock()
    fake_default_settings.model_dump.return_value = {"description": "default"}
    fake_default_settings.operator_tools_policy = None

    p1 = Mock()
    p1.id = "default_assistant"
    p1.description = "Default"
    p2 = Mock()
    p2.id = "engineer"
    p2.description = "Engineer"

    fake_app_config = Mock()
    fake_app_config.service_profiles = [p1, p2]
    fake_app_config.default_profile_settings = fake_default_settings
    fake_app_config.default_service_profile_id = "default_assistant"

    ctx = _ctx_with_app_config(fake_app_config)
    result = await get_profile_config(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["profile_ids"] == ["default_assistant", "engineer"]
    assert data["default_service_profile_id"] == "default_assistant"
    assert "default_profile_settings" in data


@pytest.mark.anyio
async def test_get_profile_config_returns_specific_profile() -> None:
    matched = Mock()
    matched.id = "engineer"
    matched.description = "Engineer profile"
    matched.model_dump.return_value = {
        "id": "engineer",
        "description": "Engineer profile",
        "processing_config": {"openai_api_key": "leak-me"},
    }
    matched.operator_tools_policy = None

    fake_app_config = Mock()
    fake_app_config.service_profiles = [matched]

    ctx = _ctx_with_app_config(fake_app_config)
    result = await get_profile_config(ctx, profile_id="engineer")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["id"] == "engineer"
    assert data["description"] == "Engineer profile"
    cfg = data["config"]
    assert isinstance(cfg, dict)
    assert cfg["processing_config"]["openai_api_key"] == "[REDACTED]"


@pytest.mark.anyio
async def test_get_profile_config_unknown_id_returns_error() -> None:
    p1 = Mock()
    p1.id = "default_assistant"
    fake_app_config = Mock()
    fake_app_config.service_profiles = [p1]

    ctx = _ctx_with_app_config(fake_app_config)
    result = await get_profile_config(ctx, profile_id="does_not_exist")
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["profile_ids"] == ["default_assistant"]


# --- get_profile_tool_inventory ---


def _notes_policy_provider() -> PolicyEnforcingToolsProvider:
    return PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.NOTES]),
                        decision=ToolPolicyDecision.ALLOW,
                        priority=10,
                    ),
                ],
            )
        ),
    )


def _ctx_with_registry(registry: object) -> ToolExecutionContext:
    processing_service = Mock()
    processing_service.processing_services_registry = registry
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="c",
        user_name="u",
        turn_id=None,
        db_context=Mock(),
        processing_service=processing_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.anyio
async def test_get_profile_tool_inventory_summary_across_profiles() -> None:
    provider = _notes_policy_provider()
    all_names = [
        defn["function"]["name"]
        for defn in await provider.get_tool_definitions(can_confirm=True)
    ]
    hidden = all_names[0]
    on_demand_view = OnDemandToolsView(
        wrapped_provider=provider, on_demand_tool_names={hidden}
    )
    service = Mock()
    service.tools_provider = provider
    service.on_demand_view = on_demand_view
    ctx = _ctx_with_registry({"notes_profile": service})

    result = await get_profile_tool_inventory(ctx)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["profile_count"] == 1
    profile = data["profiles"][0]
    assert profile["profile_id"] == "notes_profile"
    # Summary mode omits the per-tool lists.
    assert "tools" not in profile["eager"]
    assert profile["activate_tools_present"] is True
    assert profile["on_demand"]["count"] == 1


@pytest.mark.anyio
async def test_get_profile_tool_inventory_specific_profile_has_tool_lists() -> None:
    provider = _notes_policy_provider()
    service = Mock()
    service.tools_provider = provider
    service.on_demand_view = None
    ctx = _ctx_with_registry({"notes_profile": service})

    result = await get_profile_tool_inventory(ctx, profile_id="notes_profile")
    data = result.get_data()
    assert isinstance(data, dict)
    profile = data["profiles"][0]
    assert isinstance(profile["eager"]["tools"], list)
    assert profile["eager"]["tools"]
    assert profile["on_demand"]["count"] == 0


@pytest.mark.anyio
async def test_get_profile_tool_inventory_unknown_profile_returns_error() -> None:
    service = Mock()
    service.tools_provider = _notes_policy_provider()
    service.on_demand_view = None
    ctx = _ctx_with_registry({"notes_profile": service})

    result = await get_profile_tool_inventory(ctx, profile_id="missing")
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["profile_ids"] == ["notes_profile"]


@pytest.mark.anyio
async def test_get_profile_tool_inventory_without_registry_returns_error(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    result = await get_profile_tool_inventory(exec_context_with_db)
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data


# --- resolve_tool_policy ---


def _diagnostic_policy_provider() -> PolicyEnforcingToolsProvider:
    """Policy provider with allow, confirm, and argument-conditional rules."""
    return PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(names=["add_or_update_note"]),
                        decision=ToolPolicyDecision.ALLOW,
                        priority=10,
                        description="notes tool allowed",
                    ),
                    PolicyRule(
                        match=ToolMatcher(
                            names=["add_or_update_note"],
                            argument_equals={"title": "secret"},
                        ),
                        decision=ToolPolicyDecision.DENY,
                        priority=20,
                        description="secret titles denied",
                    ),
                    PolicyRule(
                        match=ToolMatcher(names=["read_error_logs"]),
                        decision=ToolPolicyDecision.CONFIRM,
                        priority=10,
                        description="error logs need confirmation",
                    ),
                ],
            )
        ),
    )


def _diagnostic_service(
    on_demand_view: OnDemandToolsView | None = None,
) -> Mock:
    service = Mock()
    service.tools_provider = _diagnostic_policy_provider()
    service.on_demand_view = on_demand_view
    return service


@pytest.mark.anyio
async def test_resolve_tool_policy_allowed_tool_reports_matched_rule() -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()})

    result = await resolve_tool_policy(ctx, tool_name="add_or_update_note")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["tool_exists"] is True
    assert data["can_confirm"] is True
    assert data["profile_count"] == 1
    entry = data["profiles"][0]
    assert entry["profile_id"] == "diag"
    assert entry["default_decision"] == "deny"
    assert entry["origin"] == "local"
    assert entry["mcp_server_id"] is None
    assert entry["on_demand"] is False
    raw = entry["raw"]
    assert raw["decision"] == "allow"
    rule = raw["matched_rule"]
    assert rule["layer"] == "defaults"
    assert rule["priority"] == 10
    assert rule["effective_priority"] == 10
    assert rule["decision"] == "allow"
    assert rule["description"] == "notes tool allowed"
    assert rule["match"] == {"names": ["add_or_update_note"]}
    assert entry["advertisement"]["decision"] == "allow"
    assert entry["execution"]["decision"] == "allow"
    # Without arguments the caller is warned that argument-conditional rules
    # cannot match.
    assert "argument_equals" in data["note"]


@pytest.mark.anyio
async def test_resolve_tool_policy_default_deny_has_no_matched_rule() -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()})

    result = await resolve_tool_policy(ctx, tool_name="search_documents")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["tool_exists"] is True
    entry = data["profiles"][0]
    raw = entry["raw"]
    assert raw["decision"] == "deny"
    assert raw["matched_rule"] is None
    assert "default" in raw["reason"]
    assert entry["execution"]["decision"] == "deny"


@pytest.mark.anyio
async def test_resolve_tool_policy_confirm_gated_tool_depends_on_can_confirm() -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()})

    confirmable = await resolve_tool_policy(
        ctx, tool_name="read_error_logs", can_confirm=True
    )
    data = confirmable.get_data()
    assert isinstance(data, dict)
    entry = data["profiles"][0]
    assert entry["raw"]["decision"] == "confirm"
    assert entry["execution"]["decision"] == "confirm"
    assert entry["advertisement"]["decision"] == "confirm"

    unconfirmable = await resolve_tool_policy(
        ctx, tool_name="read_error_logs", can_confirm=False
    )
    data = unconfirmable.get_data()
    assert isinstance(data, dict)
    entry = data["profiles"][0]
    assert entry["raw"]["decision"] == "confirm"
    assert entry["execution"]["decision"] == "deny"
    assert "confirmation required but unavailable" in entry["execution"]["reason"]
    assert entry["advertisement"]["decision"] == "deny"


@pytest.mark.anyio
async def test_resolve_tool_policy_argument_conditional_rule_needs_arguments() -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()})

    without_args = await resolve_tool_policy(ctx, tool_name="add_or_update_note")
    data = without_args.get_data()
    assert isinstance(data, dict)
    assert data["profiles"][0]["raw"]["decision"] == "allow"
    assert "note" in data

    with_args = await resolve_tool_policy(
        ctx,
        tool_name="add_or_update_note",
        arguments={"title": "secret"},
    )
    data = with_args.get_data()
    assert isinstance(data, dict)
    assert data["arguments"] == {"title": "secret"}
    assert "note" not in data
    entry = data["profiles"][0]
    assert entry["raw"]["decision"] == "deny"
    assert entry["raw"]["matched_rule"]["description"] == "secret titles denied"
    assert entry["execution"]["decision"] == "deny"


@pytest.mark.anyio
async def test_resolve_tool_policy_unknown_tool_suggests_similar_names() -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()})

    result = await resolve_tool_policy(ctx, tool_name="add_or_update_notes")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["tool_exists"] is False
    assert "add_or_update_note" in data["similar_tool_names"]
    assert len(data["similar_tool_names"]) <= 10
    assert data["profiles"][0]["profile_id"] == "diag"
    assert "error" in data["profiles"][0]


@pytest.mark.anyio
async def test_resolve_tool_policy_unknown_profile_returns_error() -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()})

    result = await resolve_tool_policy(
        ctx, tool_name="add_or_update_note", profile_id="missing"
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert data["profile_ids"] == ["diag"]


@pytest.mark.anyio
async def test_resolve_tool_policy_without_registry_returns_error(
    exec_context_with_db: ToolExecutionContext,
) -> None:
    result = await resolve_tool_policy(
        exec_context_with_db, tool_name="add_or_update_note"
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data


@pytest.mark.anyio
async def test_resolve_tool_policy_reports_on_demand_tools() -> None:
    provider = _diagnostic_policy_provider()
    service = Mock()
    service.tools_provider = provider
    service.on_demand_view = OnDemandToolsView(
        wrapped_provider=provider,
        on_demand_tool_names={"add_or_update_note"},
    )
    ctx = _ctx_with_registry({"diag": service})

    result = await resolve_tool_policy(ctx, tool_name="add_or_update_note")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["profiles"][0]["on_demand"] is True


@pytest.mark.anyio
async def test_resolve_tool_policy_rejects_non_bool_can_confirm() -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()})

    result = await resolve_tool_policy(
        ctx,
        tool_name="add_or_update_note",
        can_confirm="true",  # type: ignore[arg-type]  # script kwargs bypass schema coercion
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "can_confirm must be a boolean" in data["error"]


@pytest.mark.anyio
async def test_resolve_tool_policy_rejects_non_dict_arguments() -> None:
    ctx = _ctx_with_registry({"diag": _diagnostic_service()})

    result = await resolve_tool_policy(
        ctx,
        tool_name="add_or_update_note",
        arguments=["title"],  # type: ignore[arg-type]  # script kwargs bypass schema coercion
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "arguments must be an object" in data["error"]
