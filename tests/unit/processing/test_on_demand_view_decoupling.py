"""Regression tests for on-demand decoupling from the shared tools provider.

On-demand gating is an LLM-context-window concern only. Before the
``OnDemandToolsView`` refactor, the on-demand wrapper sat in the shared
``ProcessingService.tools_provider`` chain, so non-LLM consumers — most
importantly the script engine driving event-triggered automations — also saw
on-demand tools filtered out of ``get_tool_definitions()`` and lost the
ability to call them. These tests pin the new shape: ``tools_provider``
returns the full policy-filtered set; the on-demand view is a sibling
referenced only by the LLM loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import OnDemandToolsView
from tests.mocks.mock_llm import (
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition


async def _noop_tool(**_kwargs: object) -> str:
    return "ok"


def _registration(name: str) -> ToolRegistration:
    return ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Description of {name}.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        implementation=_noop_tool,
        metadata=make_local_tool_metadata([
            ToolTag.READ_ONLY,
            ToolTag.OUTPUT_TRUSTED,
        ]),
    )


def _build_service(
    *, with_on_demand: bool
) -> tuple[ProcessingService, LocalToolsProvider, OnDemandToolsView | None]:
    local_provider = LocalToolsProvider(
        registrations=[_registration("eager_a"), _registration("lazy_b")]
    )
    on_demand_view: OnDemandToolsView | None = None
    if with_on_demand:
        on_demand_view = OnDemandToolsView(
            wrapped_provider=local_provider,
            on_demand_tool_names={"lazy_b"},
        )
    service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="ok", tool_calls=None),
        ),
        tools_provider=local_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "test"},
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="on-demand-decoupling",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        on_demand_view=on_demand_view,
    )
    return service, local_provider, on_demand_view


def _assistant_config(*, on_demand_local_tools: list[str]) -> dict[str, object]:
    return {
        "telegram_token": "test_token",
        "allowed_user_ids": [12345],
        "developer_chat_id": 12345,
        "model": "test-model",
        "embedding_model": "mock-deterministic-embedder",
        "embedding_dimensions": 384,
        "server_url": "http://test.local",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "service_profiles": [
            {
                "id": "on-demand-profile",
                "processing_config": {
                    "prompts": {},
                    "timezone": "UTC",
                    "max_history_messages": 10,
                    "history_max_age_hours": 24,
                },
                "tools_config": {"on_demand_local_tools": on_demand_local_tools},
                "tools_policy": {"default_decision": "allow", "rules": []},
            },
            {
                "id": "no-on-demand-profile",
                "processing_config": {
                    "prompts": {},
                    "timezone": "UTC",
                    "max_history_messages": 10,
                    "history_max_age_hours": 24,
                },
                "tools_config": {},
                "tools_policy": {"default_decision": "allow", "rules": []},
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_profile_tools_provider_includes_on_demand_tools_for_non_llm_consumers() -> (
    None
):
    """Scripts read the profile's ``tools_provider``; it must return ALL tools.

    Drives the real wiring in ``Assistant._build_profile_tools_provider``, not
    a hand-built stand-in. Regression: when on-demand was wrapped around the
    policy provider in the shared chain, scripts (which call
    ``get_tool_definitions()`` without an activation set) only saw eager
    tools, so tools moved behind a skill became invisible to automations.
    """
    mock_llm = RuleBasedMockLLMClient(
        rules=[], default_response=LLMOutput(content="ok", tool_calls=None)
    )
    config = _assistant_config(on_demand_local_tools=["add_or_update_note"])
    assistant = Assistant(
        AppConfig.model_validate(config),
        llm_client_overrides={
            "on-demand-profile": mock_llm,
            "no-on-demand-profile": mock_llm,
        },
    )
    await assistant.setup_dependencies()
    try:
        service = cast(
            "ProcessingService",
            assistant.processing_services_registry["on-demand-profile"],
        )

        defs = await service.tools_provider.get_tool_definitions()
        names = {d["function"]["name"] for d in defs}

        assert "add_or_update_note" in names
        assert service.on_demand_view is not None

        view_defs = await service.on_demand_view.get_tool_definitions()
        view_names = {d["function"]["name"] for d in view_defs}
        assert "add_or_update_note" not in view_names
        assert "activate_tools" in view_names
    finally:
        await assistant.stop_services()


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_profile_without_on_demand_entries_gets_no_view() -> None:
    """A profile configured with no on-demand entries gets a ``None`` view."""
    mock_llm = RuleBasedMockLLMClient(
        rules=[], default_response=LLMOutput(content="ok", tool_calls=None)
    )
    config = _assistant_config(on_demand_local_tools=[])
    assistant = Assistant(
        AppConfig.model_validate(config),
        llm_client_overrides={
            "on-demand-profile": mock_llm,
            "no-on-demand-profile": mock_llm,
        },
    )
    await assistant.setup_dependencies()
    try:
        service = cast(
            "ProcessingService",
            assistant.processing_services_registry["no-on-demand-profile"],
        )
        assert service.on_demand_view is None

        defs = await service.tools_provider.get_tool_definitions()
        names = {d["function"]["name"] for d in defs}
        assert "add_or_update_note" in names
    finally:
        await assistant.stop_services()


@pytest.mark.asyncio
async def test_on_demand_view_still_hides_unactivated_tools_from_llm() -> None:
    """The LLM-facing view must still gate on-demand tools until activated."""
    service, _, on_demand_view = _build_service(with_on_demand=True)
    assert service.on_demand_view is on_demand_view
    assert on_demand_view is not None

    defs = await on_demand_view.get_tool_definitions()
    names = {d["function"]["name"] for d in defs}

    # Eager tool plus the synthetic activate_tools meta-tool; lazy_b is hidden.
    assert names == {"eager_a", "activate_tools"}
