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


def _build_policy_config(
    *,
    allowed_names: list[str],
    confirm_names: list[str] | None = None,
) -> dict[str, object]:
    rules: list[dict[str, object]] = [
        {
            "match": {"names": allowed_names},
            "decision": "allow",
            "priority": 10,
        }
    ]
    if confirm_names:
        rules.append({
            "match": {"names": confirm_names},
            "decision": "confirm",
            "priority": 20,
        })
    return {
        "default_decision": "deny",
        "rules": rules,
    }


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
            "confirmation_timeout_seconds": 10.0,
            "mcp_initialization_timeout_seconds": 5,
        },
        "tools_policy": _build_policy_config(
            allowed_names=["get_note", "delete_note"],
            confirm_names=["delete_note"],
        ),
    }
    if not include_tools_policy:
        del profile_config["tools_policy"]

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
                "confirmation_timeout_seconds": 10.0,
            },
            "tools_policy": _build_policy_config(allowed_names=[]),
        },
        "service_profiles": [profile_config],
        "mcp_config": {"mcpServers": {}},
        "indexing_pipeline_config": {"processors": []},
        "event_system": {"enabled": False},
    })


@pytest.mark.asyncio
async def test_assistant_profile_tools_are_policy_enforced(
    db_engine: AsyncEngine,
) -> None:
    assistant = Assistant(
        config=_build_test_config(
            db_engine,
            include_tools_policy=True,
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


@pytest.mark.asyncio
async def test_assistant_requires_explicit_tools_policy(
    db_engine: AsyncEngine,
) -> None:
    assistant = Assistant(
        config=_build_test_config(
            db_engine,
            include_tools_policy=False,
        ),
        llm_client_overrides={
            "default_assistant": RuleBasedMockLLMClient(rules=[]),
        },
        database_engine=db_engine,
    )

    with pytest.raises(
        ValueError,
        match="Profile 'default_assistant' is missing tools_policy",
    ):
        await assistant.setup_dependencies()
