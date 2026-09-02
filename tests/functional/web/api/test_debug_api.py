"""Functional tests for debug API endpoints, including the profile config dump."""

import json
from typing import Any

import httpx
import pytest

from family_assistant.config_loader import resolve_all_service_profiles
from family_assistant.config_models import (
    AppConfig,
    DefaultProfileSettings,
    ProcessingConfig,
    ServiceProfile,
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
            llm_model="gemini/gemini-3.1-pro-preview",
            provider="google",
            max_iterations=7,
            home_assistant_token="super-secret-ha-token",  # should be redacted
            prompts={
                "system_prompt": "You are a helpful assistant for {user_name}.",
            },
        ),
        tools_config=ToolsConfig(
            on_demand_local_tools=["search_documents"],
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
            llm_model="gemini/gemini-3.8-flash",
            provider="google",
            max_iterations=3,
        ),
        tools_config=ToolsConfig(on_demand_local_tools=["search_documents"]),
    )

    return AppConfig(
        # The endpoint fails closed when auth is disabled unless dev_mode is
        # explicitly on; these tests run with auth disabled so they opt in here.
        dev_mode=True,
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
        assert processing["llm_model"] == "gemini/gemini-3.1-pro-preview"
        assert processing["max_iterations"] == 7
        assert (
            processing["prompts"]["system_prompt"]
            == "You are a helpful assistant for {user_name}."
        )

        tools_config = trusted["config"]["tools_config"]
        assert tools_config["on_demand_local_tools"] == ["search_documents"]

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
    """Runtime info is extracted using the same attribute shapes the real LLM clients use.

    ``OpenAIClient`` / ``AnthropicClient`` expose ``self.model``, while
    ``GoogleGenAIClient`` uses ``self.model_name`` with a leading ``models/``
    prefix. The endpoint must surface the live model for all shapes and must
    NOT re-emit a configured-only ``provider`` field (no concrete client
    exposes one). This test uses small classes that mirror those real attribute
    shapes to catch regressions where the endpoint reads from a
    non-existent attribute.
    """

    class _OpenAILikeClient:
        """Mirrors OpenAIClient/AnthropicClient — sets ``self.model``."""

        def __init__(self) -> None:
            self.model = "gpt-5-turbo"

    class _GoogleLikeClient:
        """Mirrors GoogleGenAIClient — sets ``self.model_name`` with ``models/`` prefix."""

        def __init__(self) -> None:
            self.model_name = "models/gemini-3.1-pro-preview"

    class _FakeContextProvider:
        name = "notes"

    class _OpenAILocalService:
        kind = "local"

        def __init__(self) -> None:
            self.llm_client = _OpenAILikeClient()
            self.context_providers = [_FakeContextProvider()]

    class _GoogleLocalService:
        kind = "local"

        def __init__(self) -> None:
            self.llm_client = _GoogleLikeClient()
            self.context_providers = [_FakeContextProvider()]

    registry = {
        "trusted": _OpenAILocalService(),  # exercises the .model path
        "readonly": _GoogleLocalService(),  # exercises the .model_name path
    }

    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry(registry)
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        data = response.json()

        trusted = next(p for p in data["profiles"] if p["id"] == "trusted")
        assert trusted["runtime"]["kind"] == "local"
        assert trusted["runtime"]["llm_model"] == "gpt-5-turbo"
        assert trusted["runtime"]["llm_fallback_model"] is None
        assert trusted["runtime"]["llm_client_class"] == "_OpenAILikeClient"
        assert trusted["runtime"]["context_providers"] == ["notes"]
        # provider should NOT be in runtime: no concrete client exposes it.
        assert "llm_provider" not in trusted["runtime"]

        readonly = next(p for p in data["profiles"] if p["id"] == "readonly")
        # Google-like client: model_name with "models/" prefix is normalized.
        assert readonly["runtime"]["kind"] == "local"
        assert readonly["runtime"]["llm_model"] == "gemini-3.1-pro-preview"
        assert readonly["runtime"]["llm_fallback_model"] is None
        assert readonly["runtime"]["llm_client_class"] == "_GoogleLikeClient"
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_runtime_info_for_retrying_llm_client(
    api_client: httpx.AsyncClient,
) -> None:
    """RetryingLLMClient exposes model names on primary_model / fallback_model.

    Profiles configured with ``processing_config.retry_config`` get their LLM
    client wrapped by ``RetryingLLMClient`` in production (see ``assistant.py``).
    That wrapper does not have ``model`` or ``model_name`` attributes; it stores
    the active identifier on ``self.primary_model`` and the fallback on
    ``self.fallback_model``. The endpoint must handle this wrapper shape or
    ``/api/debug/profiles`` would emit ``"llm_model": null`` for any profile
    using retry/fallback — a supported production configuration.
    """

    class _InnerClient:
        """Stand-in for the primary/fallback provider client that RetryingLLMClient wraps."""

    class _RetryingLikeClient:
        """Mirrors RetryingLLMClient — wraps primary/fallback clients.

        Sets ``fallback_client`` to a truthy object to signal that a fallback
        is actually configured. ``RetryingLLMClient.__init__`` always sets
        ``self.fallback_model`` to a default string, so the endpoint uses
        ``fallback_client`` as the "fallback is wired" signal.
        """

        def __init__(self) -> None:
            self.primary_client = _InnerClient()
            self.primary_model = "anthropic/claude-sonnet-4-6"
            self.fallback_client = _InnerClient()
            self.fallback_model = "models/gemini-3.8-flash"

    class _RetryingLocalService:
        kind = "local"

        def __init__(self) -> None:
            self.llm_client = _RetryingLikeClient()
            self.context_providers: list[object] = []

    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({"trusted": _RetryingLocalService()})
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        trusted = next(p for p in response.json()["profiles"] if p["id"] == "trusted")
        runtime = trusted["runtime"]
        assert runtime["kind"] == "local"
        assert runtime["llm_model"] == "anthropic/claude-sonnet-4-6"
        # Fallback's "models/" prefix is normalized too.
        assert runtime["llm_fallback_model"] == "gemini-3.8-flash"
        assert runtime["llm_client_class"] == "_RetryingLikeClient"
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_retrying_llm_client_without_fallback_reports_no_fallback(
    api_client: httpx.AsyncClient,
) -> None:
    """Primary-only retry_config profiles must not falsely advertise a fallback.

    ``RetryingLLMClient.__init__`` always stores a default string on
    ``self.fallback_model`` (currently ``"openai/gpt-5.6-terra"``) even when
    ``fallback_client=None``, so a naive read of ``fallback_model`` would
    misrepresent every primary-only retry profile as having a fallback.
    """

    class _PrimaryOnlyRetrying:
        def __init__(self) -> None:
            self.primary_client = object()
            self.primary_model = "anthropic/claude-sonnet-4-6"
            # Mirrors RetryingLLMClient: fallback_client=None but
            # fallback_model retains its default string because of the
            # ``fallback_model or "openai/gpt-5.6-terra"`` constructor logic.
            self.fallback_client = None
            self.fallback_model = "openai/gpt-5.6-terra"

    class _PrimaryOnlyService:
        kind = "local"

        def __init__(self) -> None:
            self.llm_client = _PrimaryOnlyRetrying()
            self.context_providers: list[object] = []

    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({"trusted": _PrimaryOnlyService()})
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        runtime = next(p for p in response.json()["profiles"] if p["id"] == "trusted")[
            "runtime"
        ]
        assert runtime["llm_model"] == "anthropic/claude-sonnet-4-6"
        assert runtime["llm_fallback_model"] is None
        # Sanity: the default fallback string must not leak into the response
        # anywhere, since no fallback_client is configured.
        assert "openai/gpt-5.6-terra" not in response.text
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_remote_services_have_no_live_llm_fields(
    api_client: httpx.AsyncClient,
) -> None:
    """Remote A2A profiles surface only ``kind`` in runtime info."""

    class _RemoteService:
        kind = "remote"

    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({"trusted": _RemoteService()})
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        trusted = next(p for p in response.json()["profiles"] if p["id"] == "trusted")
        assert trusted["runtime"] == {"kind": "remote"}
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_includes_operator_layer(
    api_client: httpx.AsyncClient,
) -> None:
    """Operator-layer policy overrides are exposed even though model_dump excludes them.

    ``ServiceProfile.operator_tools_policy`` is declared with ``exclude=True`` so
    Pydantic's default ``model_dump()`` leaves it out. At runtime it gets merged
    into the effective policy alongside the profile-layer rules, so the debug
    endpoint must surface it or it will misrepresent how tools are gated.
    """
    profile = ServiceProfile(
        id="with_operator_overrides",
        description="Profile with operator-layer policy overrides.",
        processing_config=ProcessingConfig(llm_model="gemini/gemini-3.8-flash"),
        tools_policy=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["profile_*"]),
                    decision=ToolPolicyDecision.ALLOW,
                    description="Profile-layer rule",
                ),
            ],
            default_decision=ToolPolicyDecision.CONFIRM,
        ),
    )
    config = AppConfig(
        dev_mode=True,  # opt in to debug dump when auth is disabled
        default_service_profile_id="with_operator_overrides",
        service_profiles=[profile],
        default_profile_settings=DefaultProfileSettings(),
    )
    # ServiceProfile declares operator_tools_policy with exclude=True, so passing
    # it to AppConfig(...) would strip it during nested re-validation. In
    # production it is set programmatically after config load, so we mirror that.
    config.service_profiles[0].operator_tools_policy = ToolPolicyConfig(
        rules=[
            PolicyRule(
                match=ToolMatcher(names=["operator_*"]),
                decision=ToolPolicyDecision.DENY,
                description="Operator-layer override",
            ),
        ],
        default_decision=ToolPolicyDecision.DENY,
    )
    original_config = _install_test_config(config)
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        dumped = response.json()["profiles"][0]["config"]

        operator_policy = dumped["operator_tools_policy"]
        assert operator_policy is not None
        assert operator_policy["default_decision"] == "deny"
        assert len(operator_policy["rules"]) == 1
        assert operator_policy["rules"][0]["match"]["names"] == ["operator_*"]
        assert operator_policy["rules"][0]["decision"] == "deny"
        assert operator_policy["rules"][0]["description"] == "Operator-layer override"

        # The profile-layer policy is still present alongside the operator layer.
        assert dumped["tools_policy"]["rules"][0]["match"]["names"] == ["profile_*"]
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_reflects_resolved_defaults(
    api_client: httpx.AsyncClient,
) -> None:
    """Fields inherited from default_profile_settings must appear in each profile's dump.

    Uses ``resolve_all_service_profiles`` — the same resolver
    ``config_loader.load_config`` runs in production — so the profiles that end
    up in ``config.service_profiles`` have defaults merged the same way the app
    sees them at runtime. If the endpoint ever switched to
    ``model_dump(exclude_defaults=True)`` or otherwise dropped inherited values,
    this test would catch it.
    """
    config_data = {
        "dev_mode": True,  # endpoint fails closed without real auth otherwise
        "default_service_profile_id": "inheriting_profile",
        "default_profile_settings": {
            "processing_config": {
                "timezone": "Australia/Sydney",
                "max_history_messages": 11,
                "history_max_age_hours": 48.0,
                "llm_model": "gemini/default-model",
            },
            "tools_config": {
                "confirmation_timeout_seconds": 120.0,
            },
        },
        "service_profiles": [
            {
                "id": "inheriting_profile",
                "description": "Inherits defaults without overriding them.",
            },
        ],
    }
    resolved = resolve_all_service_profiles(config_data, {})
    config_data["service_profiles"] = resolved
    app_config = AppConfig.model_validate(config_data)

    original_config = _install_test_config(app_config)
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        data = response.json()

        profile_config = data["profiles"][0]["config"]
        processing = profile_config["processing_config"]
        # These values come from default_profile_settings, not from the profile
        # definition, so the endpoint only surfaces them if it emits the
        # already-merged ServiceProfile (as production does).
        assert processing["timezone"] == "Australia/Sydney"
        assert processing["max_history_messages"] == 11
        assert processing["history_max_age_hours"] == 48.0
        assert processing["llm_model"] == "gemini/default-model"
        assert profile_config["tools_config"]["confirmation_timeout_seconds"] == 120.0
    finally:
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_default_settings_include_operator_layer(
    api_client: httpx.AsyncClient,
) -> None:
    """default_profile_settings also exposes excluded operator-layer fields.

    ``DefaultProfileSettings.operator_tools_policy`` has ``exclude=True`` for
    the same reason the profile-level field does, and it applies to every
    profile at runtime. The top-level ``default_profile_settings`` dump must
    include it too or a consumer reading this endpoint will misjudge the
    effective policy.
    """
    config = _make_sample_config()
    config.default_profile_settings.operator_tools_policy = ToolPolicyConfig(
        rules=[
            PolicyRule(
                match=ToolMatcher(names=["global_*"]),
                decision=ToolPolicyDecision.DENY,
                description="Global operator override",
            ),
        ],
        default_decision=ToolPolicyDecision.DENY,
    )
    original_config = _install_test_config(config)
    original_registry = _install_registry({})
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        defaults = response.json()["default_profile_settings"]

        op_policy = defaults["operator_tools_policy"]
        assert op_policy is not None
        assert op_policy["default_decision"] == "deny"
        assert op_policy["rules"][0]["match"]["names"] == ["global_*"]
        assert op_policy["rules"][0]["description"] == "Global operator override"

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


@pytest.mark.asyncio
async def test_dump_profiles_fails_closed_when_auth_disabled_and_dev_mode_off(
    api_client: httpx.AsyncClient,
) -> None:
    """With auth disabled and dev_mode off, the endpoint returns 403.

    ``get_current_user`` returns a synthetic test user when
    ``auth_service.auth_enabled`` is false, which would otherwise let an
    unauthenticated caller read prompts / policy / runtime state. The endpoint
    must refuse to respond in that configuration unless the operator has
    explicitly set ``dev_mode=true``.
    """
    config = _make_sample_config()
    config.dev_mode = False  # override the default enabled by _make_sample_config
    original_config = _install_test_config(config)
    original_registry = _install_registry({})
    # Ensure no real auth_service is attached — mirrors auth-disabled deployments.
    original_auth = getattr(fastapi_app.state, "auth_service", None)
    if hasattr(fastapi_app.state, "auth_service"):
        del fastapi_app.state.auth_service
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 403
        # No profile data should leak in the error body.
        assert "default_service_profile_id" not in response.text
    finally:
        if original_auth is not None:
            fastapi_app.state.auth_service = original_auth
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_dev_mode_bypasses_fail_closed_check(
    api_client: httpx.AsyncClient,
) -> None:
    """With dev_mode=True and no auth service, the endpoint still responds 200.

    This is the explicit opt-in path for local development: the operator sets
    ``app_config.dev_mode=true`` to accept responsibility for the exposure.
    """
    config = _make_sample_config()  # dev_mode=True from the helper
    original_config = _install_test_config(config)
    original_registry = _install_registry({})
    original_auth = getattr(fastapi_app.state, "auth_service", None)
    if hasattr(fastapi_app.state, "auth_service"):
        del fastapi_app.state.auth_service
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 200
        assert response.json()["default_service_profile_id"] == "trusted"
    finally:
        if original_auth is not None:
            fastapi_app.state.auth_service = original_auth
        _restore_registry(original_registry)
        _restore_config(original_config)


class _FakeAuthService:
    """Minimal stand-in for AuthService with auth enabled and no valid sessions/tokens."""

    auth_enabled = True
    oauth = None

    async def get_user_from_api_token(
        self,
        auth_header: str,
        request: object,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_dump_profiles_requires_authentication(
    api_client: httpx.AsyncClient,
) -> None:
    """With auth enabled, unauthenticated requests to /api/debug/profiles return 401."""
    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({})
    original_auth = getattr(fastapi_app.state, "auth_service", None)
    fastapi_app.state.auth_service = _FakeAuthService()
    try:
        response = await api_client.get("/api/debug/profiles")
        assert response.status_code == 401
        # The response must not leak profile data when unauthenticated.
        assert "default_service_profile_id" not in response.text
    finally:
        if original_auth is not None:
            fastapi_app.state.auth_service = original_auth
        else:
            del fastapi_app.state.auth_service
        _restore_registry(original_registry)
        _restore_config(original_config)


@pytest.mark.asyncio
async def test_dump_profiles_accepts_valid_bearer_token(
    api_client: httpx.AsyncClient,
) -> None:
    """With auth enabled, a valid bearer token lets the caller read the dump."""

    class _AcceptingAuthService(_FakeAuthService):
        async def get_user_from_api_token(
            self,
            auth_header: str,
            request: object,
        ) -> dict | None:
            if auth_header == "Bearer good-token":
                return {"sub": "tester", "source": "api_token"}
            return None

    original_config = _install_test_config(_make_sample_config())
    original_registry = _install_registry({})
    original_auth = getattr(fastapi_app.state, "auth_service", None)
    fastapi_app.state.auth_service = _AcceptingAuthService()
    try:
        bad = await api_client.get(
            "/api/debug/profiles",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert bad.status_code == 401

        good = await api_client.get(
            "/api/debug/profiles",
            headers={"Authorization": "Bearer good-token"},
        )
        assert good.status_code == 200
        assert good.json()["default_service_profile_id"] == "trusted"
    finally:
        if original_auth is not None:
            fastapi_app.state.auth_service = original_auth
        else:
            del fastapi_app.state.auth_service
        _restore_registry(original_registry)
        _restore_config(original_config)
