from __future__ import annotations

# pylint: disable=no-name-in-module
from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_models import AppConfig
from family_assistant.tools import (
    PolicyEnforcingToolsProvider,
    get_tool_definitions_for_advertisement,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _build_test_config(
    db_engine: AsyncEngine,
    *,
    include_tools_policy: bool,
) -> AppConfig:
    profile_config: dict[str, object] = {
        "id": "default_assistant",
        "description": "Runtime cutover test profile",
        "processing_config": {
            "prompts": {"system_prompt": "You are a test assistant."},
            "timezone": "UTC",
            "max_history_messages": 5,
            "history_max_age_hours": 1,
            "llm_model": "mock-model",
            "delegation_security_level": "none",
        },
        "tools_config": {
            "enable_local_tools": ["get_note", "delete_note"],
            "enable_mcp_server_ids": [],
            "confirm_tools": ["delete_note"],
            "confirmation_timeout_seconds": 10.0,
            "mcp_initialization_timeout_seconds": 5,
        },
    }
    if include_tools_policy:
        profile_config["tools_policy"] = {
            "default_decision": "deny",
            "rules": [
                {
                    "match": {"names": ["get_note", "delete_note"]},
                    "decision": "allow",
                    "priority": 10,
                },
                {
                    "match": {"names": ["delete_note"]},
                    "decision": "confirm",
                    "priority": 20,
                },
            ],
        }

    return AppConfig.model_validate({
        "telegram_enabled": False,
        "model": "mock-model",
        "embedding_model": "mock-deterministic-embedder",
        "embedding_dimensions": 10,
        "database_url": str(db_engine.url),
        "server_url": "http://testserver",
        "document_storage_path": "/tmp/runtime-cutover-docs",
        "chat_attachment_storage_path": "/tmp/runtime-cutover-attachments",
        "default_service_profile_id": "default_assistant",
        "default_profile_settings": {
            "processing_config": {
                "prompts": {"system_prompt": "You are a default assistant."},
                "timezone": "UTC",
                "max_history_messages": 5,
                "history_max_age_hours": 1,
                "delegation_security_level": "none",
            },
            "tools_config": {
                "enable_local_tools": [],
                "enable_mcp_server_ids": [],
                "confirm_tools": [],
            },
        },
        "service_profiles": [profile_config],
        "mcp_config": {"mcpServers": {}},
        "indexing_pipeline_config": {"processors": []},
        "event_system": {"enabled": False},
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("include_tools_policy", [True, False])
async def test_assistant_profile_tools_are_policy_enforced(
    db_engine: AsyncEngine,
    include_tools_policy: bool,
) -> None:
    assistant = Assistant(
        config=_build_test_config(
            db_engine,
            include_tools_policy=include_tools_policy,
        ),
        llm_client_overrides={
            "default_assistant": RuleBasedMockLLMClient(rules=[]),
        },
        database_engine=db_engine,
    )

    try:
        await assistant.setup_dependencies()

        service = assistant.default_processing_service
        assert service is not None
        assert isinstance(service.tools_provider, PolicyEnforcingToolsProvider)

        without_confirm = await get_tool_definitions_for_advertisement(
            service.tools_provider,
            can_confirm=False,
        )
        with_confirm = await get_tool_definitions_for_advertisement(
            service.tools_provider,
            can_confirm=True,
        )

        names_without_confirm = {
            definition["function"]["name"] for definition in without_confirm
        }
        names_with_confirm = {
            definition["function"]["name"] for definition in with_confirm
        }

        assert names_without_confirm == {"get_note"}
        assert names_with_confirm == {"get_note", "delete_note"}
    finally:
        await assistant.stop_services()
