"""Functional tests for the resolved per-profile tool inventory endpoint.

``GET /api/debug/profiles/tools`` reports the eager-vs-on-demand tool split per
live profile for diagnosing tool bloat, and is unlocked by the read-only
diagnostics token (it exposes only tool names and sizes, no prompts or policy).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    LocalToolsProvider,
    OnDemandToolsView,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import DIAGNOSTICS_READONLY_TOKEN_ENV_VAR

if TYPE_CHECKING:
    import httpx
    import pytest as _pytest

READONLY_TOKEN = "ro-secret-token-value"


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


async def _build_registry() -> tuple[dict[str, object], str]:
    """Return a {profile_id: service} registry plus the hidden on-demand name."""
    provider = _notes_policy_provider()
    all_names = [
        defn["function"]["name"]
        for defn in await provider.get_tool_definitions(can_confirm=True)
    ]
    hidden = all_names[0]
    on_demand_view = OnDemandToolsView(
        wrapped_provider=provider,
        on_demand_tool_names={hidden},
    )
    service = SimpleNamespace(
        tools_provider=provider,
        on_demand_view=on_demand_view,
    )
    return {"notes_profile": service}, hidden


def _install_registry(registry: dict[str, object]) -> object | None:
    original = getattr(fastapi_app.state, "processing_services", None)
    fastapi_app.state.processing_services = registry
    return original


def _restore_registry(original: object | None) -> None:
    if original is not None:
        fastapi_app.state.processing_services = original
    elif hasattr(fastapi_app.state, "processing_services"):
        del fastapi_app.state.processing_services


class _FakeAuthService:
    """AuthService stand-in with auth enabled and no valid sessions/tokens."""

    auth_enabled = True
    oauth = None

    async def get_user_from_api_token(
        self,
        auth_header: str,
        request: object,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_tool_inventory_partitions_eager_and_on_demand(
    api_client: httpx.AsyncClient,
) -> None:
    registry, hidden = await _build_registry()
    original = _install_registry(registry)
    try:
        response = await api_client.get("/api/debug/profiles/tools")
        assert response.status_code == 200
        payload = response.json()
    finally:
        _restore_registry(original)

    assert payload["profile_count"] == 1
    profile = payload["profiles"][0]
    assert profile["profile_id"] == "notes_profile"
    assert profile["activate_tools_present"] is True

    eager_names = {entry["name"] for entry in profile["eager"]["tools"]}
    on_demand_names = {entry["name"] for entry in profile["on_demand"]["tools"]}
    assert on_demand_names == {hidden}
    assert "activate_tools" in eager_names
    assert hidden not in eager_names

    # Per-turn cost includes the on-demand catalog prompt (rendered every turn),
    # not just the eager tool definitions.
    assert profile["advertised_per_turn_tokens"] == (
        profile["eager"]["estimated_tokens"]
        + profile["on_demand_catalog_prompt"]["estimated_tokens"]
    )
    assert profile["on_demand_catalog_prompt"]["estimated_tokens"] > 0
    sources = {b["source"] for b in profile["by_source"]}
    assert {"local", "meta"} <= sources


@pytest.mark.asyncio
async def test_tool_inventory_summary_omits_tool_lists(
    api_client: httpx.AsyncClient,
) -> None:
    registry, _hidden = await _build_registry()
    original = _install_registry(registry)
    try:
        response = await api_client.get("/api/debug/profiles/tools?include_tools=false")
        assert response.status_code == 200
        payload = response.json()
    finally:
        _restore_registry(original)

    profile = payload["profiles"][0]
    assert "tools" not in profile["eager"]
    assert "tools" not in profile["on_demand"]
    assert profile["eager"]["count"] >= 1


@pytest.mark.asyncio
async def test_tool_inventory_unknown_profile_returns_404(
    api_client: httpx.AsyncClient,
) -> None:
    registry, _hidden = await _build_registry()
    original = _install_registry(registry)
    try:
        response = await api_client.get(
            "/api/debug/profiles/tools?profile_id=does_not_exist"
        )
    finally:
        _restore_registry(original)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_tool_inventory_unlocked_by_readonly_token(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
) -> None:
    """With auth enabled and no session, the read-only token grants access."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    registry, _hidden = await _build_registry()
    registry_original = _install_registry(registry)
    auth_original = getattr(fastapi_app.state, "auth_service", None)
    fastapi_app.state.auth_service = _FakeAuthService()
    try:
        # Without the token, auth-enabled access is refused.
        denied = await api_client.get("/api/debug/profiles/tools")
        assert denied.status_code == 401

        allowed = await api_client.get(
            "/api/debug/profiles/tools",
            headers={"Authorization": f"Bearer {READONLY_TOKEN}"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["profile_count"] == 1
    finally:
        if auth_original is not None:
            fastapi_app.state.auth_service = auth_original
        elif hasattr(fastapi_app.state, "auth_service"):
            del fastapi_app.state.auth_service
        _restore_registry(registry_original)
