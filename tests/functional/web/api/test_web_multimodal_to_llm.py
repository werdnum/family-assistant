"""
Functional tests verifying Web UI attachments are passed to LLM.

Tests use real AttachmentRegistry and ProcessingService, mocking only
the LLM responses via RuleBasedMockLLMClient.

These tests verify that video, audio, and document attachments sent via the
web chat API are correctly passed to the LLM, not just images.
"""

import base64
import io
import json
import tempfile
from collections.abc import AsyncGenerator, Generator
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.context_providers import (
    CalendarContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
)
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import ImageUrlContentPart
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage import init_db
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    ConfirmingToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    ToolsProvider,
)
from family_assistant.web.app_creator import app as actual_app
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    import httpx

    from family_assistant.tools.types import CalendarConfig


# ============================================================================
# Helper: Content Part Matchers
# ============================================================================


def has_media_content_part(args: MatcherArgs, mime_prefix: str) -> bool:
    """Check if LLM messages contain a media content part with given MIME prefix."""
    messages = args.get("messages", [])
    for msg in messages:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for part in content:
                if isinstance(part, ImageUrlContentPart):
                    url = part.image_url.get("url", "")
                    # Data URI format: data:video/mp4;base64,...
                    if url.startswith(f"data:{mime_prefix}"):
                        return True
    return False


def has_image_content(args: MatcherArgs) -> bool:
    """Check if LLM messages contain image content."""
    return has_media_content_part(args, "image/")


def has_video_content(args: MatcherArgs) -> bool:
    """Check if LLM messages contain video content."""
    return has_media_content_part(args, "video/")


def has_audio_content(args: MatcherArgs) -> bool:
    """Check if LLM messages contain audio content."""
    return has_media_content_part(args, "audio/")


def has_pdf_content(args: MatcherArgs) -> bool:
    """Check if LLM messages contain PDF content."""
    return has_media_content_part(args, "application/pdf")


# ============================================================================
# Helper: Create Test Files
# ============================================================================


def create_test_image_base64(
    width: int = 100, height: int = 100, color: str = "blue"
) -> str:
    """Create a test image and return as base64 data URL."""
    img = Image.new("RGB", (width, height), color=color)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    image_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{image_data}"


def create_test_video_base64() -> str:
    """Create minimal MP4 bytes and return as base64 data URL."""
    # Minimal ftyp box for MP4
    video_bytes = (
        b"\x00\x00\x00\x1c"  # box size (28 bytes)
        b"ftyp"  # box type
        b"mp42"  # major brand
        b"\x00\x00\x00\x00"  # minor version
        b"mp42"  # compatible brand 1
        b"isom"  # compatible brand 2
        b"\x00" * 100  # padding
    )
    video_data = base64.b64encode(video_bytes).decode("utf-8")
    return f"data:video/mp4;base64,{video_data}"


def create_test_audio_base64() -> str:
    """Create minimal audio bytes and return as base64 data URL."""
    # Fake MP3 with ID3 header
    audio_bytes = b"ID3" + b"\x00" * 100
    audio_data = base64.b64encode(audio_bytes).decode("utf-8")
    return f"data:audio/mpeg;base64,{audio_data}"


def create_test_pdf_base64() -> str:
    """Create minimal PDF and return as base64 data URL."""
    pdf_bytes = b"%PDF-1.4\n%test PDF content\n%%EOF"
    pdf_data = base64.b64encode(pdf_bytes).decode("utf-8")
    return f"data:application/pdf;base64,{pdf_data}"


async def parse_streaming_response(
    response: "httpx.Response",
) -> tuple[str, list[dict]]:
    """Parse SSE streaming response and return (text_content, events)."""
    text_content = ""
    events = []
    content = response.content.decode("utf-8")

    current_event_type = None
    for line in content.strip().split("\n"):
        if line.startswith("event:"):
            current_event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current_event_type:
            data_str = line.split(":", 1)[1].strip()
            if data_str:
                try:
                    data = json.loads(data_str)
                    events.append({"type": current_event_type, "data": data})
                    if "content" in data:
                        text_content += data["content"]
                except json.JSONDecodeError:
                    pass

    return text_content, events


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture(scope="function")
async def db_context(
    db_engine: AsyncEngine,
) -> AsyncGenerator[DatabaseContext]:
    """Provides a DatabaseContext for a single test function."""
    async with get_db_context(engine=db_engine) as ctx:
        yield ctx


@pytest.fixture(scope="function")
def mock_processing_service_config() -> ProcessingServiceConfig:
    """Provides a mock ProcessingServiceConfig for tests."""
    return ProcessingServiceConfig(
        prompts={
            "system_prompt": (
                "You are a test assistant. Current time: {current_time}. "
                "Server URL: {server_url}. "
                "Context: {aggregated_other_context}"
            )
        },
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={
            "enable_local_tools": ["add_or_update_note"],
            "enable_mcp_server_ids": [],
            "confirm_tools": [],
        },
        delegation_security_level="confirm",
        id="multimodal_api_test_profile",
    )


@pytest.fixture(scope="function")
def mock_llm_client() -> RuleBasedMockLLMClient:
    """Provides a RuleBasedMockLLMClient for tests."""
    return RuleBasedMockLLMClient(rules=[])


@pytest_asyncio.fixture(scope="function")
async def test_tools_provider(
    mock_processing_service_config: ProcessingServiceConfig,
) -> ToolsProvider:
    """Provides a ToolsProvider configured for testing."""
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
        embedding_generator=None,
        calendar_config=cast(
            "CalendarConfig", {"caldav": {"calendar_urls": ["http://test.com"]}}
        ),
    )
    mock_mcp_provider = AsyncMock(spec=MCPToolsProvider)
    mock_mcp_provider.get_tool_definitions.return_value = []
    mock_mcp_provider.execute_tool.return_value = "MCP tool executed (mock)."
    mock_mcp_provider.close.return_value = None

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mock_mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    confirming_provider = ConfirmingToolsProvider(
        wrapped_provider=composite_provider,
        tools_requiring_confirmation=set(
            mock_processing_service_config.tools_config.get("confirm_tools", [])
        ),
    )
    await confirming_provider.get_tool_definitions()
    return confirming_provider


@pytest.fixture(scope="function")
def test_attachment_registry(
    db_engine: AsyncEngine,
) -> "Generator[AttachmentRegistry]":
    """Provides an AttachmentRegistry with temp storage for tests."""
    with tempfile.TemporaryDirectory() as temp_storage:
        registry = AttachmentRegistry(
            storage_path=temp_storage,
            db_engine=db_engine,
        )
        yield registry


@pytest.fixture(scope="function")
def test_processing_service(
    mock_llm_client: RuleBasedMockLLMClient,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config: ProcessingServiceConfig,
    db_context: DatabaseContext,
    test_attachment_registry: AttachmentRegistry,
) -> ProcessingService:
    """Creates a ProcessingService instance with mock/test components."""
    captured_engine = db_context.engine

    async def get_entered_db_context_for_provider() -> DatabaseContext:
        async with get_db_context(engine=captured_engine) as new_ctx:
            return new_ctx

    notes_provider = NotesContextProvider(
        get_db_context_func=get_entered_db_context_for_provider,
        prompts=mock_processing_service_config.prompts,
    )
    calendar_provider = CalendarContextProvider(
        calendar_config=cast(
            "CalendarConfig", {"caldav": {"calendar_urls": ["http://test.com"]}}
        ),
        timezone_str=mock_processing_service_config.timezone_str,
        prompts=mock_processing_service_config.prompts,
    )
    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={}, prompts=mock_processing_service_config.prompts
    )
    context_providers = [notes_provider, calendar_provider, known_users_provider]

    return ProcessingService(
        llm_client=mock_llm_client,
        tools_provider=test_tools_provider,
        service_config=mock_processing_service_config,
        context_providers=context_providers,
        server_url="http://testserver",
        app_config=AppConfig(),
        attachment_registry=test_attachment_registry,
    )


@pytest_asyncio.fixture(scope="function")
async def app_fixture(
    db_engine: AsyncEngine,
    test_processing_service: ProcessingService,
    test_tools_provider: ToolsProvider,
    mock_llm_client: RuleBasedMockLLMClient,
    test_attachment_registry: AttachmentRegistry,
) -> AsyncGenerator[FastAPI]:
    """Creates a FastAPI application instance for testing."""
    app = FastAPI(
        title=actual_app.title,
        docs_url=actual_app.docs_url,
        redoc_url=actual_app.redoc_url,
        middleware=actual_app.user_middleware,
    )
    app.include_router(actual_app.router)

    app.state.processing_service = test_processing_service
    app.state.tools_provider = test_tools_provider
    app.state.database_engine = db_engine
    app.state.config = AppConfig(
        database_url=str(db_engine.url),
    )
    app.state.llm_client = mock_llm_client
    app.state.debug_mode = False

    app.state.web_chat_interface = WebChatInterface(db_engine)
    app.state.attachment_registry = test_attachment_registry

    async with get_db_context(engine=db_engine) as temp_db_ctx:
        await init_db(db_engine)
        await temp_db_ctx.init_vector_db()

    yield app


@pytest_asyncio.fixture(scope="function")
async def test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Provides an HTTPX AsyncClient for the test FastAPI app."""
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ============================================================================
# Test Classes
# ============================================================================


class TestWebImageUpload:
    """Test image uploads via web API."""

    @pytest.mark.asyncio
    async def test_image_upload_passed_to_llm(
        self, test_client: AsyncClient, mock_llm_client: RuleBasedMockLLMClient
    ) -> None:
        """Image uploaded via /api/chat should be in LLM content."""
        image_seen = {"value": False}

        def image_matcher(args: MatcherArgs) -> bool:
            if has_image_content(args):
                image_seen["value"] = True
                return True
            return False

        mock_llm_client.rules = [
            (image_matcher, LLMOutput(content="I can see the image you uploaded!")),
        ]
        mock_llm_client.default_response = LLMOutput(content="No image found")

        image_url = create_test_image_base64()

        response = await test_client.post(
            "/api/v1/chat/send_message_stream",
            json={
                "prompt": "What do you see in this image?",
                "attachments": [
                    {"type": "image", "content": image_url, "name": "test_image.png"}
                ],
            },
        )

        assert response.status_code == 200
        assert image_seen["value"], "LLM should have received image content part"


class TestWebVideoUpload:
    """Test video uploads via web API."""

    @pytest.mark.asyncio
    async def test_video_upload_passed_to_llm(
        self, test_client: AsyncClient, mock_llm_client: RuleBasedMockLLMClient
    ) -> None:
        """Video uploaded via web should be in LLM content."""
        video_seen = {"value": False}

        def video_matcher(args: MatcherArgs) -> bool:
            if has_video_content(args):
                video_seen["value"] = True
                return True
            return False

        mock_llm_client.rules = [
            (video_matcher, LLMOutput(content="I can analyze your video!")),
        ]
        mock_llm_client.default_response = LLMOutput(content="No video found")

        video_url = create_test_video_base64()

        response = await test_client.post(
            "/api/v1/chat/send_message_stream",
            json={
                "prompt": "What's happening in this video?",
                "attachments": [
                    {"type": "video", "content": video_url, "name": "test_video.mp4"}
                ],
            },
        )

        assert response.status_code == 200
        # This will FAIL initially - videos aren't processed
        assert video_seen["value"], "LLM should have received video content part"


class TestWebAudioUpload:
    """Test audio uploads via web API."""

    @pytest.mark.asyncio
    async def test_audio_upload_passed_to_llm(
        self, test_client: AsyncClient, mock_llm_client: RuleBasedMockLLMClient
    ) -> None:
        """Audio uploaded via web should be in LLM content."""
        audio_seen = {"value": False}

        def audio_matcher(args: MatcherArgs) -> bool:
            if has_audio_content(args):
                audio_seen["value"] = True
                return True
            return False

        mock_llm_client.rules = [
            (audio_matcher, LLMOutput(content="I can hear your audio!")),
        ]
        mock_llm_client.default_response = LLMOutput(content="No audio found")

        audio_url = create_test_audio_base64()

        response = await test_client.post(
            "/api/v1/chat/send_message_stream",
            json={
                "prompt": "What song is playing?",
                "attachments": [
                    {"type": "audio", "content": audio_url, "name": "song.mp3"}
                ],
            },
        )

        assert response.status_code == 200
        # This will FAIL initially - audio isn't processed
        assert audio_seen["value"], "LLM should have received audio content part"


class TestWebDocumentUpload:
    """Test document uploads via web API."""

    @pytest.mark.asyncio
    async def test_pdf_upload_passed_to_llm(
        self, test_client: AsyncClient, mock_llm_client: RuleBasedMockLLMClient
    ) -> None:
        """PDF uploaded via web should be in LLM content."""
        pdf_seen = {"value": False}

        def pdf_matcher(args: MatcherArgs) -> bool:
            if has_pdf_content(args):
                pdf_seen["value"] = True
                return True
            return False

        mock_llm_client.rules = [
            (pdf_matcher, LLMOutput(content="I can read your PDF!")),
        ]
        mock_llm_client.default_response = LLMOutput(content="No PDF found")

        pdf_url = create_test_pdf_base64()

        response = await test_client.post(
            "/api/v1/chat/send_message_stream",
            json={
                "prompt": "Summarize this document",
                "attachments": [
                    {"type": "document", "content": pdf_url, "name": "report.pdf"}
                ],
            },
        )

        assert response.status_code == 200
        assert pdf_seen["value"], "LLM should have received PDF content part"
