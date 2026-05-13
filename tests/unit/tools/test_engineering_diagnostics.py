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

from family_assistant.tools.engineering import (
    get_mcp_server_status,
    get_profile_config,
    get_resolved_config,
    get_system_info,
    reconnect_mcp_server,
)
from family_assistant.tools.mcp import MCPToolsProvider
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
