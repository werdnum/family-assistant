from __future__ import annotations

# pylint: disable=no-name-in-module
import sys
import types
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

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
from family_assistant.web.routers.gemini_live_api import (
    _convert_json_schema_type_to_gemini,  # noqa: PLC2701 - unit tests need direct access to internal helper
    _convert_properties_to_gemini,  # noqa: PLC2701 - unit tests need direct access to internal helper
    gemini_live_router,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.tools.types import (
        ToolDefinition,
        ToolExecutionContext,
        ToolResult,
    )


def test_convert_scalar_type_to_gemini() -> None:
    assert _convert_json_schema_type_to_gemini("string") == ("STRING", False)


def test_convert_nullable_list_type_to_gemini() -> None:
    assert _convert_json_schema_type_to_gemini(["string", "null"]) == ("STRING", True)


def test_convert_properties_marks_nullable_field() -> None:
    properties = {"note": {"type": ["string", "null"], "description": "Optional"}}

    converted = _convert_properties_to_gemini(properties)

    assert converted["note"]["type"] == "STRING"
    assert converted["note"]["nullable"] is True


def test_convert_properties_nullable_array_items() -> None:
    properties = {"tags": {"type": "array", "items": {"type": ["string", "null"]}}}

    converted = _convert_properties_to_gemini(properties)

    assert converted["tags"]["type"] == "ARRAY"
    assert converted["tags"]["items"]["type"] == "STRING"
    assert converted["tags"]["items"]["nullable"] is True


def test_convert_properties_non_nullable_field_omits_flag() -> None:
    properties = {"query": {"type": "string"}}

    converted = _convert_properties_to_gemini(properties)

    assert "nullable" not in converted["query"]


class VoiceModeStubToolsProvider:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def get_tool_definitions(
        self,
        *,
        can_confirm: bool = True,
    ) -> list[ToolDefinition]:
        self.calls.append(can_confirm)
        tools: list[ToolDefinition] = [
            cast(
                "ToolDefinition",
                {
                    "type": "function",
                    "function": {
                        "name": "safe_tool",
                        "description": "Always allowed",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            )
        ]
        if can_confirm:
            tools.append(
                cast(
                    "ToolDefinition",
                    {
                        "type": "function",
                        "function": {
                            "name": "confirm_tool",
                            "description": "Requires confirmation",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                )
            )
        return tools

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, object],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        return f"Executed {name}"

    async def close(self) -> None:
        return None


class _FakeAuthTokens:
    @staticmethod
    def create(*, config: dict[str, object]) -> types.SimpleNamespace:
        assert config["uses"] == 1
        return types.SimpleNamespace(name="ephemeral-token")


class _FakeGenAIClient:
    def __init__(self, *, api_key: str, http_options: dict[str, str]) -> None:
        assert api_key == "test-api-key"
        assert http_options == {"api_version": "v1alpha"}
        self.auth_tokens = _FakeAuthTokens()


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_ephemeral_token_uses_confirmation_aware_tool_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")

    fake_google = cast("Any", types.ModuleType("google"))
    fake_google.genai = types.SimpleNamespace(Client=_FakeGenAIClient)
    monkeypatch.setitem(sys.modules, "google", fake_google)

    tools_provider = VoiceModeStubToolsProvider()
    processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="ok", tool_calls=None),
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a voice assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="voice-profile",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )

    app = FastAPI()
    app.include_router(gemini_live_router, prefix="/api")
    app.state.processing_service = processing_service
    app.state.config = AppConfig()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/gemini/ephemeral-token", json={})

    assert response.status_code == 200
    body = response.json()
    declarations = body["tools"][0]["functionDeclarations"]
    assert [declaration["name"] for declaration in declarations] == ["safe_tool"]
    assert tools_provider.calls == [False]


async def _live_noop_tool(**_kwargs: object) -> str:
    return "ok"


def _live_registration(name: str) -> ToolRegistration:
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
        implementation=_live_noop_tool,
        metadata=make_local_tool_metadata([
            ToolTag.READ_ONLY,
            ToolTag.OUTPUT_TRUSTED,
        ]),
    )


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_ephemeral_token_declares_meta_tools_for_on_demand_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live session cannot gain declarations later, so it gets the meta-tools.

    The on-demand tool is left out of the declaration list and named in the
    system instruction instead, which is the only place the model can learn it
    is worth a ``search_tools`` call.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")

    fake_google = cast("Any", types.ModuleType("google"))
    fake_google.genai = types.SimpleNamespace(Client=_FakeGenAIClient)
    monkeypatch.setitem(sys.modules, "google", fake_google)

    tools_provider = LocalToolsProvider(
        registrations=[
            _live_registration("list_notes"),
            _live_registration("generate_image"),
        ]
    )
    processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="ok", tool_calls=None),
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a voice assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="voice-profile",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        on_demand_view=OnDemandToolsView(
            wrapped_provider=tools_provider,
            on_demand_tool_names={"generate_image"},
        ),
    )

    app = FastAPI()
    app.include_router(gemini_live_router, prefix="/api")
    app.state.processing_service = processing_service
    app.state.config = AppConfig()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/gemini/ephemeral-token", json={})

    assert response.status_code == 200
    body = response.json()
    declared = {
        declaration["name"] for declaration in body["tools"][0]["functionDeclarations"]
    }
    assert declared == {"list_notes", "search_tools", "call_tool"}
    assert "generate_image" in body["system_instruction"]


@pytest.mark.asyncio
@pytest.mark.no_db
async def test_ephemeral_token_declares_everything_when_on_demand_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")

    fake_google = cast("Any", types.ModuleType("google"))
    fake_google.genai = types.SimpleNamespace(Client=_FakeGenAIClient)
    monkeypatch.setitem(sys.modules, "google", fake_google)

    tools_provider = LocalToolsProvider(
        registrations=[
            _live_registration("list_notes"),
            _live_registration("generate_image"),
        ]
    )
    processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="ok", tool_calls=None),
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a voice assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="voice-profile",
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        on_demand_view=OnDemandToolsView(
            wrapped_provider=tools_provider,
            on_demand_tool_names={"generate_image"},
        ),
    )

    app = FastAPI()
    app.include_router(gemini_live_router, prefix="/api")
    app.state.processing_service = processing_service
    app.state.config = AppConfig.model_validate({
        "gemini_live_config": {"tools": {"on_demand": False}}
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/gemini/ephemeral-token", json={})

    assert response.status_code == 200
    declared = {
        declaration["name"]
        for declaration in response.json()["tools"][0]["functionDeclarations"]
    }
    assert declared == {"list_notes", "generate_image"}
