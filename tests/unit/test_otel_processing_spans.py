"""Tests for OpenTelemetry spans in processing, Telegram handler, and task worker."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import family_assistant.processing.context as proc_context_module
import family_assistant.processing.service as proc_service_module
import family_assistant.processing.tool_execution as proc_tool_module
import family_assistant.task_worker as tw_module
import family_assistant.telegram.handler as tg_module
from family_assistant.config_models import ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.types import TaskDict
from family_assistant.tools import ToolNotFoundError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from telegram import Update

    from family_assistant.context_providers import ContextProvider
    from family_assistant.telegram.types import AttachmentData


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def processing_span_exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    test_tracer = provider.get_tracer("test")

    original_context_tracer = proc_context_module.tracer
    original_tool_tracer = proc_tool_module.tracer
    original_service_tracer = proc_service_module.tracer
    proc_context_module.tracer = test_tracer
    proc_tool_module.tracer = test_tracer
    proc_service_module.tracer = test_tracer

    yield exporter

    proc_context_module.tracer = original_context_tracer
    proc_tool_module.tracer = original_tool_tracer
    proc_service_module.tracer = original_service_tracer
    provider.shutdown()


@pytest.fixture
def telegram_span_exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    original_tracer = tg_module.tracer
    tg_module.tracer = provider.get_tracer("test")

    yield exporter

    tg_module.tracer = original_tracer
    provider.shutdown()


@pytest.fixture
def task_worker_span_exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    original_tracer = tw_module.tracer
    tw_module.tracer = provider.get_tracer("test")

    yield exporter

    tw_module.tracer = original_tracer
    provider.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockContextProvider:
    def __init__(
        self, name: str = "test_provider", fragments: list[str] | None = None
    ) -> None:
        self._name = name
        self._fragments = (
            fragments if fragments is not None else ["fragment1", "fragment2"]
        )

    @property
    def name(self) -> str:
        return self._name

    async def get_context_fragments(self) -> list[str]:
        return self._fragments


def _make_processing_service(
    context_providers: list[MockContextProvider] | None = None,
) -> ProcessingService:
    """Build a minimal ProcessingService with mocked dependencies."""
    mock_llm = MagicMock()
    mock_tools = MagicMock()
    service_config = ProcessingServiceConfig(
        prompts={"system": "test"},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=1.0,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.BLOCKED,
        id="test-profile",
    )
    mock_app_config = MagicMock()
    mock_app_config.attachment_config.large_tool_result_threshold_kb = 100
    providers: list[ContextProvider] = (
        list(context_providers) if context_providers else []
    )
    return ProcessingService(
        llm_client=mock_llm,
        tools_provider=mock_tools,
        service_config=service_config,
        context_providers=providers,
        server_url="http://localhost:8000",
        app_config=mock_app_config,
    )


def _make_task_dict(
    *,
    task_id: str = "task_001",
    task_type: str = "test_task",
    payload: dict[str, str] | None = None,
) -> TaskDict:
    return TaskDict(
        id=1,
        task_id=task_id,
        task_type=task_type,
        payload=payload or {"user_name": "TestUser"},
        scheduled_at=None,
        created_at=datetime.now(UTC),
        status="pending",
        locked_by=None,
        locked_at=None,
        error=None,
        retry_count=0,
        max_retries=3,
        recurrence_rule=None,
        original_task_id=None,
    )


# ---------------------------------------------------------------------------
# Tests for context.aggregate span
# ---------------------------------------------------------------------------


class TestContextAggregateSpan:
    @pytest.mark.asyncio
    async def test_context_aggregate_creates_span(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        service = _make_processing_service(
            context_providers=[
                MockContextProvider(fragments=["fragment1", "fragment2"])
            ]
        )

        result = await service.context_preparer.aggregate_context()

        assert "fragment1" in result
        spans = processing_span_exporter.get_finished_spans()
        agg_spans = [s for s in spans if s.name == "context.aggregate"]
        assert len(agg_spans) == 1
        span = agg_spans[0]
        assert span.attributes is not None
        assert span.attributes["context.provider_count"] == 1
        assert span.attributes["context.fragments_count"] == 2

    @pytest.mark.asyncio
    async def test_context_aggregate_with_no_providers(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        service = _make_processing_service(context_providers=[])

        result = await service.context_preparer.aggregate_context()

        assert not result
        spans = processing_span_exporter.get_finished_spans()
        agg_spans = [s for s in spans if s.name == "context.aggregate"]
        assert len(agg_spans) == 1
        span = agg_spans[0]
        assert span.attributes is not None
        assert span.attributes["context.provider_count"] == 0
        assert span.attributes["context.fragments_count"] == 0

    @pytest.mark.asyncio
    async def test_context_aggregate_multiple_providers(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        providers = [
            MockContextProvider(name="p1", fragments=["a"]),
            MockContextProvider(name="p2", fragments=["b", "c"]),
            MockContextProvider(name="p3", fragments=["d"]),
        ]
        service = _make_processing_service(context_providers=providers)

        result = await service.context_preparer.aggregate_context()

        assert "a" in result
        assert "d" in result
        spans = processing_span_exporter.get_finished_spans()
        agg_spans = [s for s in spans if s.name == "context.aggregate"]
        assert len(agg_spans) == 1
        span = agg_spans[0]
        assert span.attributes is not None
        assert span.attributes["context.provider_count"] == 3
        assert span.attributes["context.fragments_count"] == 4

    @pytest.mark.asyncio
    async def test_context_aggregate_provider_error_raises_and_records_span(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        class FailingProvider:
            name = "failing"

            async def get_context_fragments(self) -> list[str]:
                raise RuntimeError("provider broke")

        service = _make_processing_service()
        service.context_preparer.context_providers = [
            FailingProvider(),  # type: ignore[list-item]
            MockContextProvider(fragments=["ok"]),
        ]

        with pytest.raises(
            RuntimeError,
            match="Context provider 'failing' failed to provide fragments",
        ):
            await service.context_preparer.aggregate_context()

        spans = processing_span_exporter.get_finished_spans()
        agg_spans = [s for s in spans if s.name == "context.aggregate"]
        assert len(agg_spans) == 1
        span = agg_spans[0]
        assert span.attributes is not None
        assert span.attributes["context.provider_count"] == 2
        assert "context.fragments_count" not in span.attributes


# ---------------------------------------------------------------------------
# Tests for tool.execute.* span
# ---------------------------------------------------------------------------


class TestToolExecuteSpan:
    @pytest.mark.asyncio
    async def test_tool_execute_creates_span_on_success(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        service = _make_processing_service()
        service.tool_executor.tools_provider = MagicMock()
        service.tool_executor.tools_provider.execute_tool = AsyncMock(
            return_value="tool result text"
        )

        tool_call = ToolCallItem(
            id="call_abc",
            type="function",
            function=ToolCallFunction(name="get_weather", arguments='{"city": "NYC"}'),
        )
        db_context = AsyncMock()

        result = await service.tool_executor.execute(
            tool_call,
            interface_type="telegram",
            conversation_id="conv1",
            user_name="user1",
            turn_id="turn1",
            db_context=db_context,
            chat_interface=None,
        )

        assert result.stream_event.tool_result is not None
        spans = processing_span_exporter.get_finished_spans()
        tool_spans = [s for s in spans if s.name.startswith("tool.execute.")]
        assert len(tool_spans) == 1
        span = tool_spans[0]
        assert span.name == "tool.execute.get_weather"
        assert span.attributes is not None
        assert span.attributes["tool.name"] == "get_weather"
        assert span.attributes["tool.call_id"] == "call_abc"
        assert span.attributes["tool.status"] == "success"

    @pytest.mark.asyncio
    async def test_tool_execute_sets_error_on_not_found(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        service = _make_processing_service()
        service.tool_executor.tools_provider = MagicMock()
        service.tool_executor.tools_provider.execute_tool = AsyncMock(
            side_effect=ToolNotFoundError("no_such_tool")
        )

        tool_call = ToolCallItem(
            id="call_def",
            type="function",
            function=ToolCallFunction(name="no_such_tool", arguments="{}"),
        )
        db_context = AsyncMock()

        result = await service.tool_executor.execute(
            tool_call,
            interface_type="telegram",
            conversation_id="conv1",
            user_name="user1",
            turn_id="turn1",
            db_context=db_context,
            chat_interface=None,
        )

        assert "Error" in (result.stream_event.tool_result or "")
        spans = processing_span_exporter.get_finished_spans()
        tool_spans = [s for s in spans if s.name.startswith("tool.execute.")]
        assert len(tool_spans) == 1
        span = tool_spans[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes is not None
        assert span.attributes["tool.status"] == "error"

    @pytest.mark.asyncio
    async def test_tool_execute_sets_error_on_exception(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        service = _make_processing_service()
        service.tool_executor.tools_provider = MagicMock()
        service.tool_executor.tools_provider.execute_tool = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        tool_call = ToolCallItem(
            id="call_ghi",
            type="function",
            function=ToolCallFunction(name="exploding_tool", arguments="{}"),
        )
        db_context = AsyncMock()

        result = await service.tool_executor.execute(
            tool_call,
            interface_type="telegram",
            conversation_id="conv1",
            user_name="user1",
            turn_id="turn1",
            db_context=db_context,
            chat_interface=None,
        )

        assert "Error" in (result.stream_event.tool_result or "")
        spans = processing_span_exporter.get_finished_spans()
        tool_spans = [s for s in spans if s.name.startswith("tool.execute.")]
        assert len(tool_spans) == 1
        span = tool_spans[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes is not None
        assert span.attributes["tool.status"] == "error"
        exception_events = [e for e in span.events if e.name == "exception"]
        assert len(exception_events) >= 1

    @pytest.mark.asyncio
    async def test_tool_execute_records_result_size(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        service = _make_processing_service()
        service.tool_executor.tools_provider = MagicMock()
        service.tool_executor.tools_provider.execute_tool = AsyncMock(
            return_value="short answer"
        )

        tool_call = ToolCallItem(
            id="call_jkl",
            type="function",
            function=ToolCallFunction(name="simple_tool", arguments="{}"),
        )
        db_context = AsyncMock()

        await service.tool_executor.execute(
            tool_call,
            interface_type="telegram",
            conversation_id="conv1",
            user_name="user1",
            turn_id="turn1",
            db_context=db_context,
            chat_interface=None,
        )

        spans = processing_span_exporter.get_finished_spans()
        tool_spans = [s for s in spans if s.name.startswith("tool.execute.")]
        assert len(tool_spans) == 1
        span = tool_spans[0]
        assert span.attributes is not None
        assert span.attributes["tool.result_size"] == len("short answer")


# ---------------------------------------------------------------------------
# Tests for conversation.process span
# ---------------------------------------------------------------------------


async def _fake_llm_stream(
    *_args: object, **_kwargs: object
) -> AsyncIterator[LLMStreamEvent]:
    yield LLMStreamEvent(type="content", content="hello")


class TestConversationProcessSpan:
    @pytest.mark.asyncio
    async def test_conversation_process_creates_span(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        service = _make_processing_service()
        service.context_providers = []

        mock_llm = AsyncMock()
        mock_llm.generate_response_stream = _fake_llm_stream
        service.llm_client = mock_llm

        mock_db_context = AsyncMock()
        mock_db_context.engine = MagicMock()
        mock_db_context.engine.dialect = MagicMock()
        mock_db_context.engine.dialect.name = "sqlite"
        mock_db_context.message_history = AsyncMock()
        mock_db_context.message_history.get_recent = AsyncMock(return_value=[])
        mock_db_context.message_history.add_message = AsyncMock(return_value=1)

        events = []
        async for event in service.handle_chat_interaction_stream(
            db_context=mock_db_context,
            trigger_content_parts=[{"type": "text", "text": "hello"}],
            trigger_interface_message_id=None,
            interface_type="test",
            conversation_id="conv123",
            user_name="TestUser",
        ):
            events.append(event)

        spans = processing_span_exporter.get_finished_spans()
        conv_spans = [s for s in spans if s.name == "conversation.process"]
        assert len(conv_spans) == 1
        span = conv_spans[0]
        assert span.attributes is not None
        assert span.attributes["conversation.interface"] == "test"
        assert span.attributes["conversation.id"] == "conv123"
        assert span.attributes["conversation.user"] == "TestUser"

    @pytest.mark.asyncio
    async def test_conversation_process_sets_subconversation_id(
        self, processing_span_exporter: InMemorySpanExporter
    ) -> None:
        service = _make_processing_service()
        service.context_providers = []

        mock_llm = AsyncMock()
        mock_llm.generate_response_stream = _fake_llm_stream
        service.llm_client = mock_llm

        mock_db_context = AsyncMock()
        mock_db_context.engine = MagicMock()
        mock_db_context.engine.dialect = MagicMock()
        mock_db_context.engine.dialect.name = "sqlite"
        mock_db_context.message_history = AsyncMock()
        mock_db_context.message_history.get_recent = AsyncMock(return_value=[])
        mock_db_context.message_history.add_message = AsyncMock(return_value=1)

        async for _event in service.handle_chat_interaction_stream(
            db_context=mock_db_context,
            trigger_content_parts=[{"type": "text", "text": "hi"}],
            trigger_interface_message_id=None,
            interface_type="test",
            conversation_id="conv456",
            user_name="User2",
            subconversation_id="sub789",
        ):
            pass

        spans = processing_span_exporter.get_finished_spans()
        conv_spans = [s for s in spans if s.name == "conversation.process"]
        assert len(conv_spans) == 1
        span = conv_spans[0]
        assert span.attributes is not None
        assert span.attributes["conversation.subconversation_id"] == "sub789"


# ---------------------------------------------------------------------------
# Tests for telegram.process_batch span
# ---------------------------------------------------------------------------


class TestTelegramProcessBatchSpan:
    @pytest.mark.asyncio
    async def test_telegram_process_batch_creates_span(
        self, telegram_span_exporter: InMemorySpanExporter
    ) -> None:
        mock_user = MagicMock()
        mock_user.first_name = "Alice"

        mock_message = MagicMock()
        mock_message.message_id = 42
        mock_message.caption = None
        mock_message.text = "hello"
        mock_message.reply_to_message = None
        mock_message.forward_origin = None

        mock_update = MagicMock()
        mock_update.effective_user = mock_user
        mock_update.message = mock_message

        handler = MagicMock(spec=tg_module.TelegramUpdateHandler)
        handler.debug_mode = False
        handler.processing_service = AsyncMock()
        handler.telegram_service = MagicMock()
        handler.get_db_context_func = MagicMock()
        handler.developer_chat_id = None
        handler.confirmation_manager = MagicMock()

        batch: list[tuple[Update, list[AttachmentData] | None]] = [(mock_update, None)]

        with (
            patch.object(
                tg_module.TelegramUpdateHandler,
                "process_batch",
                tg_module.TelegramUpdateHandler.process_batch,
            ),
            contextlib.suppress(Exception),
        ):
            await tg_module.TelegramUpdateHandler.process_batch(
                handler, chat_id=123, batch=batch, context=MagicMock()
            )

        spans = telegram_span_exporter.get_finished_spans()
        tg_spans = [s for s in spans if s.name == "telegram.process_batch"]
        assert len(tg_spans) == 1
        span = tg_spans[0]
        assert span.attributes is not None
        assert span.attributes["telegram.chat_id"] == "123"
        assert span.attributes["telegram.batch_size"] == 1
        assert span.attributes["conversation.interface"] == "telegram"
        assert span.attributes["conversation.user"] == "Alice"

    @pytest.mark.asyncio
    async def test_telegram_process_batch_empty_batch_no_span(
        self, telegram_span_exporter: InMemorySpanExporter
    ) -> None:
        handler = MagicMock(spec=tg_module.TelegramUpdateHandler)

        await tg_module.TelegramUpdateHandler.process_batch(
            handler, chat_id=123, batch=[], context=MagicMock()
        )

        spans = telegram_span_exporter.get_finished_spans()
        tg_spans = [s for s in spans if s.name == "telegram.process_batch"]
        assert len(tg_spans) == 0

    @pytest.mark.asyncio
    async def test_telegram_process_batch_sets_error_on_exception(
        self, telegram_span_exporter: InMemorySpanExporter
    ) -> None:
        mock_user = MagicMock()
        mock_user.first_name = "Bob"
        mock_user.id = 123

        # Make message.reply_to_message raise to trigger error inside span
        mock_message = MagicMock()
        mock_message.message_id = 99
        mock_message.caption = None
        mock_message.text = "test"
        mock_message.forward_origin = None
        # Accessing reply_to_message will raise
        type(mock_message).reply_to_message = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("intentional"))
        )

        mock_update = MagicMock()
        mock_update.effective_user = mock_user
        mock_update.message = mock_message

        handler = MagicMock(spec=tg_module.TelegramUpdateHandler)
        handler.debug_mode = False
        handler.processing_service = AsyncMock()
        handler.telegram_service = MagicMock()
        handler.get_db_context = MagicMock()
        handler.developer_chat_id = None
        handler.confirmation_manager = MagicMock()

        batch: list[tuple[Update, list[AttachmentData] | None]] = [(mock_update, None)]

        with pytest.raises(RuntimeError, match="intentional"):
            await tg_module.TelegramUpdateHandler.process_batch(
                handler, chat_id=456, batch=batch, context=MagicMock()
            )

        spans = telegram_span_exporter.get_finished_spans()
        tg_spans = [s for s in spans if s.name == "telegram.process_batch"]
        assert len(tg_spans) == 1
        span = tg_spans[0]
        assert span.status.status_code == StatusCode.ERROR
        exception_events = [e for e in span.events if e.name == "exception"]
        assert len(exception_events) >= 1


# ---------------------------------------------------------------------------
# Tests for task.process.* span
# ---------------------------------------------------------------------------


def _make_task_worker_mock(
    task_handlers: dict[str, object],
) -> MagicMock:
    worker = MagicMock(spec=tw_module.TaskWorker)
    worker.worker_id = "test-worker"
    worker.task_handlers = task_handlers
    worker.processing_service = MagicMock()
    worker.processing_service.home_assistant_client = None
    worker.chat_interface = MagicMock()
    worker.chat_interfaces = None
    worker.event_sources = None
    worker.calendar_config = {}
    worker.timezone = ZoneInfo("UTC")
    worker.embedding_generator = MagicMock()
    worker.clock = MagicMock()
    worker.clock.now.return_value = datetime.now(UTC)
    worker.indexing_source = None
    worker.confirmation_result_waiters = None
    worker.handler_timeout = 300.0
    worker._handle_recurrence = AsyncMock()
    worker._handle_task_failure = AsyncMock()
    worker._update_last_activity = MagicMock()
    return worker


class TestTaskProcessSpan:
    @pytest.mark.asyncio
    async def test_task_process_creates_span_on_success(
        self, task_worker_span_exporter: InMemorySpanExporter
    ) -> None:
        handler_called = False

        async def dummy_handler(exec_context: object, payload: object) -> None:
            nonlocal handler_called
            handler_called = True

        worker = _make_task_worker_mock({"test_task": dummy_handler})
        task = _make_task_dict(task_id="task_001", task_type="test_task")

        db_context = AsyncMock()
        db_context.tasks = AsyncMock()
        wake_up_event = asyncio.Event()

        await tw_module.TaskWorker._process_task(
            worker, db_context, task, wake_up_event
        )

        assert handler_called
        spans = task_worker_span_exporter.get_finished_spans()
        task_spans = [s for s in spans if s.name.startswith("task.process.")]
        assert len(task_spans) == 1
        span = task_spans[0]
        assert span.name == "task.process.test_task"
        assert span.attributes is not None
        assert span.attributes["task.type"] == "test_task"
        assert span.attributes["task.id"] == "task_001"
        assert span.attributes["task.status"] == "success"

    @pytest.mark.asyncio
    async def test_task_process_no_span_when_no_handler(
        self, task_worker_span_exporter: InMemorySpanExporter
    ) -> None:
        worker = MagicMock(spec=tw_module.TaskWorker)
        worker.worker_id = "test-worker"
        worker.task_handlers = {}

        task = _make_task_dict(
            task_id="task_002", task_type="unknown_type", payload=None
        )

        db_context = AsyncMock()
        db_context.tasks = AsyncMock()
        wake_up_event = asyncio.Event()

        await tw_module.TaskWorker._process_task(
            worker, db_context, task, wake_up_event
        )

        spans = task_worker_span_exporter.get_finished_spans()
        task_spans = [s for s in spans if s.name.startswith("task.process.")]
        assert len(task_spans) == 0

    @pytest.mark.asyncio
    async def test_task_process_sets_error_on_handler_exception(
        self, task_worker_span_exporter: InMemorySpanExporter
    ) -> None:
        async def failing_handler(exec_context: object, payload: object) -> None:
            raise RuntimeError("handler exploded")

        worker = _make_task_worker_mock({"fail_task": failing_handler})
        task = _make_task_dict(
            task_id="task_003",
            task_type="fail_task",
            payload={"user_name": "FailUser"},
        )

        db_context = AsyncMock()
        db_context.tasks = AsyncMock()
        wake_up_event = asyncio.Event()

        await tw_module.TaskWorker._process_task(
            worker, db_context, task, wake_up_event
        )

        spans = task_worker_span_exporter.get_finished_spans()
        task_spans = [s for s in spans if s.name.startswith("task.process.")]
        assert len(task_spans) == 1
        span = task_spans[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes is not None
        assert span.attributes["task.status"] == "error"
        exception_events = [e for e in span.events if e.name == "exception"]
        assert len(exception_events) >= 1
