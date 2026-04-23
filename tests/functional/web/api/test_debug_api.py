"""Functional tests for debug API endpoints, including the profile config dump."""

import json
from typing import Any

import httpx
import pytest

from family_assistant.config_models import (
    AppConfig,
    DefaultProfileSettings,
    ProcessingConfig,
    ServiceProfile,
    ToolLoadingEntry,
    ToolsConfig,
)
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import (
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.web.app_creator import app as fastapi_app


def _install_test_config(config: AppConfig) -> AppConfig | None:
    """Swap in a test AppConfig, returning the prior config for restoration."""
    original = getattr(fastapi_app.state, "config", None)
    fastapi_app.state.config = config
    return original


def _restore_config(original: AppConfig | None) -> None:
    if original is not None:
        fastapi_app.state.config = original
    elif hasattr(fastapi_app.state, "config"):
        del fastapi_app.state.config


def _make_sample_config() -> AppConfig:
    """Build an AppConfig with two profiles exercising policies, tools, and prompts."""
    profile_a = ServiceProfile(
        id="trusted",
        description="Trusted profile with full tool access.",
        processing_config=ProcessingConfig(
            llm_model="gemini/gemini-2.5-pro",
            provider="google",
            max_iterations=7,
            home_assistant_token="super-secret-ha-token",  # should be redacted
            prompts={
                "system_prompt": "You are a helpful assistant for {user_name}.",
            },
        ),
        tools_config=ToolsConfig(
            enable_local_tools=[
                "add_or_update_note",
                ToolLoadingEntry(name="search_documents", loading="on_demand"),
            ],
            enable_mcp_server_ids=["time"],
            confirm_tools=["delete_note"],
        ),
        tools_policy=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["delete_*"]),
                    decision=ToolPolicyDecision.CONFIRM,
                    priority=50,
                    description="Require confirmation for destructive operations.",
                ),
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.READ_ONLY]),
                    decision=ToolPolicyDecision.ALLOW,
                    priority=10,
                    description="Read-only tools are always allowed.",
                ),
            ],
            default_decision=ToolPolicyDecision.ALLOW,
        ),
        slash_commands=["engineer"],
        visibility_grants=["family"],
    )

    profile_b = ServiceProfile(
        id="readonly",
        description="Read-only analysis profile.",
        processing_config=ProcessingConfig(
            llm_model="gemini/gemini-2.5-flash",
            provider="google",
            max_iterations=3,
        ),
        tools_config=ToolsConfig(
            enable_local_tools=["search_documents"],
            confirm_tools=[],
        ),
    )

    return AppConfig(
        default_service_profile_id="trusted",
        service_profiles=[profile_a, profile_b],
        default_profile_settings=DefaultProfileSettings(
            processing_config=ProcessingConfig(
                timezone="Australia/Sydney",
                max_iterations=5,
            ),
        ),
    )


# ast-grep-ignore: no-dict-any - Test helper wraps the runtime processing services registry
def _install_registry(registry: dict[str, Any]) -> dict[str, Any] | None:
    original = getattr(fastapi_app.state, "processing_services", None)
    fastapi_app.state.processing_services = registry
    return original


# ast-grep-ignore: no-dict-any - Test helper wraps the runtime processing services registry
def _restore_registry(original: dict[str, Any] | None) -> None:
    if original is not None:
        fastapi_app.state.processing_services = original
    elif hasattr(fastapi_app.state, "processing_services"):
        del fastapi_app.state.processing_services


@pytest.mark.asyncio
async def test_dump_profiles_returns_full_config(
    api_client: httpx.AsyncClient,
) -> None:
    """/api/debug/profiles returns the resolved configs for every profile."""
    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles")

        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]

        # Default format is pretty-printed JSON.
        raw_text = response.text
        assert "\n" in raw_text, "Expected pretty-printed JSON with newlines"
        assert "  " in raw_text, "Expected indented JSON output"

        data = response.json()
        assert data["default_service_profile_id"] == "trusted"
        assert data["profile_count"] == 2

        profile_ids = [p["id"] for p in data["profiles"]]
        assert profile_ids == ["trusted", "readonly"]

        trusted = data["profiles"][0]
        assert trusted["description"] == "Trusted profile with full tool access."
        processing = trusted["config"]["processing_config"]
        assert processing["llm_model"] == "gemini/gemini-2.5-pro"
        assert processing["max_iterations"] == 7
        assert (
            processing["prompts"]["system_prompt"]
            == "You are a helpful assistant for {user_name}."
        )

        tools_config = trusted["config"]["tools_config"]
        # Plain strings stay as strings; on-demand entries become dicts.
        assert "add_or_update_note" in tools_config["enable_local_tools"]
        on_demand = next(
            entry
            for entry in tools_config["enable_local_tools"]
            if isinstance(entry, dict)
        )
        assert on_demand == {"name": "search_documents", "loading": "on_demand"}
        assert tools_config["enable_mcp_server_ids"] == ["time"]
        assert tools_config["confirm_tools"] == ["delete_note"]

        # Policy rules are fully serialized.
        policy = trusted["config"]["tools_policy"]
        assert policy["default_decision"] == "allow"
        assert len(policy["rules"]) == 2
        first_rule = policy["rules"][0]
        assert first_rule["decision"] == "confirm"
        assert first_rule["priority"] == 50
        assert first_rule["description"] == (
            "Require confirmation for destructive operations."
        )
        assert first_rule["match"]["names"] == ["delete_*"]

        # Default profile settings are also exposed.
        assert (
            data["default_profile_settings"]["processing_config"]["timezone"]
            == "Australia/Sydney"
        )
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_redacts_sensitive_fields(
    api_client: httpx.AsyncClient,
) -> None:
    """Token/password-like fields are replaced with [REDACTED]."""
    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        data = response.json()

        trusted = data["profiles"][0]
        token = trusted["config"]["processing_config"]["home_assistant_token"]
        assert token == "[REDACTED]"
        assert "super-secret-ha-token" not in response.text
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_filter_by_id(
    api_client: httpx.AsyncClient,
) -> None:
    """The profile_id query param filters to a single profile."""
    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles?profile_id=readonly")

        assert response.status_code == 200
        data = response.json()
        assert data["profile_count"] == 1
        assert data["profiles"][0]["id"] == "readonly"
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_filter_unknown_id_returns_404(
    api_client: httpx.AsyncClient,
) -> None:
    """Requesting an unknown profile returns 404."""
    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles?profile_id=does_not_exist")
        assert response.status_code == 404
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_raw_format_is_compact(
    api_client: httpx.AsyncClient,
) -> None:
    """The raw format returns compact JSON (no indentation)."""
    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles?format=raw")

        assert response.status_code == 200
        # Compact JSON has no indentation.
        assert "\n  " not in response.text
        data = json.loads(response.text)
        assert data["profile_count"] == 2
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_includes_runtime_info(
    api_client: httpx.AsyncClient,
) -> None:
    """Runtime info from the services registry is included when available."""

    class _FakeLLM:
        model = "gemini/gemini-2.5-pro"
        provider = "google"

    class _FakeContextProvider:
        name = "notes"

    class _FakeLocalService:
        kind = "local"
        llm_client = _FakeLLM()
        context_providers = [_FakeContextProvider()]

    class _FakeRemoteService:
        kind = "remote"

    registry = {
        "trusted": _FakeLocalService(),
        "readonly": _FakeRemoteService(),
    }

    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry(registry)
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        data = response.json()

        trusted = next(p for p in data["profiles"] if p["id"] == "trusted")
        assert trusted["runtime"]["kind"] == "local"
        assert trusted["runtime"]["llm_model"] == "gemini/gemini-2.5-pro"
        assert trusted["runtime"]["llm_provider"] == "google"
        assert trusted["runtime"]["llm_client_class"] == "_FakeLLM"
        assert trusted["runtime"]["context_providers"] == ["notes"]

        readonly = next(p for p in data["profiles"] if p["id"] == "readonly")
        assert readonly["runtime"] == {"kind": "remote"}

        assert data["registered_service_ids"] == ["readonly", "trusted"]
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_returns_503_without_config(
    api_client: httpx.AsyncClient,
) -> None:
    """Without an initialized config, the endpoint reports 503."""
    original_config = getattr(fastapi_app.state, "config", None)
    if hasattr(fastapi_app.state, "config"):
        del fastapi_app.state.config
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 503
    finally:
        _restore_registry(original_registry)
        if original_config is not None:
            fastapi_app.state.config = original_config
