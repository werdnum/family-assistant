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
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module - note: pylint cannot resolve the implicit `tests` namespace package
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


@pytest.mark.asyncio
async def test_tools_provider_includes_on_demand_tools_for_non_llm_consumers() -> None:
    """Scripts read ``service.tools_provider``; it must return ALL tools.

    Regression: when on-demand was wrapped around the policy provider in the
    shared chain, scripts (which call ``get_tool_definitions()`` without an
    activation set) only saw eager tools, so HA tools that had been moved
    behind a skill became invisible to automations.
    """
    service, _, on_demand_view = _build_service(with_on_demand=True)
    assert on_demand_view is not None

    defs = await service.tools_provider.get_tool_definitions()
    names = {d["function"]["name"] for d in defs}

    assert names == {"eager_a", "lazy_b"}


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


@pytest.mark.asyncio
async def test_processing_service_without_on_demand_has_no_view() -> None:
    """Profiles with no on-demand entries get a ``None`` view, not a wrapper."""
    service, _, on_demand_view = _build_service(with_on_demand=False)

    assert on_demand_view is None
    assert service.on_demand_view is None
    defs = await service.tools_provider.get_tool_definitions()
    names = {d["function"]["name"] for d in defs}
    assert names == {"eager_a", "lazy_b"}
