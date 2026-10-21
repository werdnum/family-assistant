"""Tests for the scripting LLM API."""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import LLMInterface, LLMMessage, LLMOutput
from family_assistant.llm.base import StructuredOutputError
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.scripting.apis.llm import (
    DEFAULT_MODEL,
    llm_call_async,
    llm_call_json_async,
)
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService

# ast-grep-ignore: no-dict-any - mirrors LLMClientFactory.create_client's config
ClientConfig = dict[str, Any]


class RecordingClientFactory:
    """Stands in for ``LLMClientFactory.create_client``, recording each config."""

    def __init__(self, client: LLMInterface) -> None:
        self.client = client
        self.configs: list[ClientConfig] = []

    def __call__(self, config: ClientConfig) -> LLMInterface:
        self.configs.append(config)
        return self.client


def _replies(content: str | None) -> RuleBasedMockLLMClient:
    """An LLM whose every response has ``content``."""
    return RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=content))


def _messages_sent_to(
    client: RuleBasedMockLLMClient, method_name: str
) -> list[LLMMessage]:
    calls = [c for c in client.get_calls() if c["method_name"] == method_name]
    assert len(calls) == 1, calls
    return calls[0]["kwargs"]["messages"]


@contextmanager
def _factory_serving(client: LLMInterface) -> Iterator[RecordingClientFactory]:
    factory = RecordingClientFactory(client)
    with patch.object(LLMClientFactory, "create_client", new=factory):
        yield factory


@pytest.fixture
def llm_client() -> RuleBasedMockLLMClient:
    return _replies("Test LLM response")


@pytest.mark.no_db
async def test_llm_call_async(llm_client: RuleBasedMockLLMClient) -> None:
    """Test basic llm_call_async."""
    with _factory_serving(llm_client) as factory:
        result = await llm_call_async("Hello")

    assert factory.configs == [{"model": DEFAULT_MODEL}]
    assert result == "Test LLM response"


@pytest.mark.no_db
async def test_llm_call_async_with_system(llm_client: RuleBasedMockLLMClient) -> None:
    """Test llm_call_async with system prompt."""
    with _factory_serving(llm_client):
        await llm_call_async("Hello", system="You are helpful.")

    messages = _messages_sent_to(llm_client, "generate_response")
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[0].content == "You are helpful."
    assert messages[1].role == "user"


@pytest.mark.no_db
async def test_llm_call_async_with_custom_model(
    llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test llm_call_async with custom model."""
    with _factory_serving(llm_client) as factory:
        await llm_call_async("Hello", model="gpt-4o")

    assert factory.configs == [{"model": "gpt-4o"}]


@pytest.mark.no_db
async def test_llm_call_json_async() -> None:
    """Test llm_call_json_async returns parsed JSON using native JSON mode."""
    client = _replies('{"name": "Alice", "age": 30}')

    with _factory_serving(client):
        result = await llm_call_json_async("Extract info")

    assert result == {"name": "Alice", "age": 30}
    assert [c["method_name"] for c in client.get_calls()].count("generate_json") == 1


@pytest.mark.no_db
async def test_llm_call_json_async_with_schema() -> None:
    """Test llm_call_json_async with schema parameter includes schema in system message."""
    client = _replies('{"title": "Test"}')
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
    }

    with _factory_serving(client):
        result = await llm_call_json_async("Extract info", schema=schema)

    assert result == {"title": "Test"}
    messages = _messages_sent_to(client, "generate_json")
    assert len(messages) == 2
    system_content = messages[0].content
    assert isinstance(system_content, str)
    assert "schema" in system_content.lower()
    assert '"title"' in system_content


@pytest.mark.no_db
async def test_llm_call_json_async_with_system() -> None:
    """Test llm_call_json_async with custom system message."""
    client = _replies('{"result": "ok"}')

    with _factory_serving(client):
        await llm_call_json_async("Extract info", system="Be concise.")

    messages = _messages_sent_to(client, "generate_json")
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert isinstance(messages[0].content, str)
    assert "Be concise." in messages[0].content


@pytest.mark.no_db
async def test_llm_call_json_async_error_handling() -> None:
    """Test llm_call_json_async propagates StructuredOutputError."""
    client = _replies("this is not JSON")

    with (
        _factory_serving(client),
        pytest.raises(StructuredOutputError, match="invalid JSON"),
    ):
        await llm_call_json_async("Extract info")


@pytest.mark.no_db
async def test_llm_call_async_no_content() -> None:
    """Test that llm_call_async raises on empty LLM response."""
    with (
        _factory_serving(_replies(None)),
        pytest.raises(ValueError, match="LLM returned no content"),
    ):
        await llm_call_async("Hello")


@pytest.mark.no_db
async def test_llm_available_in_engine(llm_client: RuleBasedMockLLMClient) -> None:
    """Test that llm() is available in MontyEngine scripts."""
    with _factory_serving(llm_client):
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        result = await engine.evaluate_async("llm('Summarise this')")
    assert result == "Test LLM response"


@pytest.mark.no_db
async def test_llm_json_available_in_engine() -> None:
    """Test that llm_json() works from MontyEngine scripts."""
    with _factory_serving(_replies('{"summary": "short"}')):
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        result = await engine.evaluate_async("llm_json('Extract info')")
    assert result == {"summary": "short"}


@pytest.mark.no_db
async def test_llm_not_available_when_llm_api_disabled(
    llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test that llm() is not available when the LLM API is disabled."""
    with _factory_serving(llm_client):
        engine = MontyEngine(
            config=ScriptConfig(enable_llm_api=False),
            default_timezone=ZoneInfo("Australia/Sydney"),
        )
        with pytest.raises(ScriptExecutionError, match=r"name 'llm' is not defined"):
            await engine.evaluate_async("llm('test')")
    assert llm_client.get_calls() == []


async def test_script_llm_calls_get_the_deployment_llm_parameters(
    db_engine: AsyncEngine, llm_client: RuleBasedMockLLMClient
) -> None:
    """A script's model is served with the same parameters as everywhere else."""
    llm_parameters = {"gemini-": {"thinking_config": {"thinking_level": "low"}}}
    service = SimpleNamespace(app_config=SimpleNamespace(llm_parameters=llm_parameters))
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test-123",
        user_name="Test User",
        turn_id="turn-1",
        db_context=Database(engine=db_engine),
        processing_service=cast("ProcessingService", service),
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    with _factory_serving(llm_client) as factory:
        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        await engine.evaluate_async("llm('Summarise this')", execution_context=context)

    assert factory.configs == [
        {"model": DEFAULT_MODEL, "model_parameters": llm_parameters}
    ]
