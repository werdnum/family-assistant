"""
Functional tests verifying Telegram attachments are passed to LLM.

Tests use real AttachmentRegistry and ProcessingService, mocking only
the LLM responses via RuleBasedMockLLMClient.

These tests verify that video, audio, and document attachments sent via Telegram
are correctly passed to the LLM, not just images.
"""

import io
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Audio, Bot, Chat, Document, Message, PhotoSize, Update, User, Video
from telegram.ext import Application, ContextTypes

from family_assistant.llm.messages import ImageUrlContentPart
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

from .conftest import TelegramHandlerTestFixture


def create_mock_get_file(file_data: bytes, file_size: int = 1000) -> AsyncMock:
    """Create a mock get_file method that returns a downloadable File object.

    Since Bot/ExtBot objects are frozen, we mock get_file at the media class level
    (Video, Audio, Document, PhotoSize) rather than on the bot instance.
    """

    async def mock_download(out: io.BytesIO) -> None:
        out.write(file_data)

    mock_file = AsyncMock()
    mock_file.file_size = file_size
    mock_file.download_to_memory = mock_download

    return AsyncMock(return_value=mock_file)


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
# Helper: Create Mock Context
# ============================================================================


def create_mock_context(
    application: Application[Any, Any, Any, Any, Any, Any] | AsyncMock,
    bot_data: dict[Any, Any] | None = None,
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a mock CallbackContext."""
    context = ContextTypes.DEFAULT_TYPE(
        application=application, chat_id=123, user_id=12345
    )
    if bot_data:
        context.bot_data.update(bot_data)
    return context


# ============================================================================
# Helper: Create Mock Updates with Various Media Types
# ============================================================================


def create_mock_update(
    message_text: str,
    chat_id: int = 123,
    user_id: int = 12345,
    message_id: int = 101,
    reply_to_message: Message | None = None,
) -> Update:
    """Creates a mock Telegram Update object for a text message."""
    user = User(id=user_id, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=message_text,
        reply_to_message=reply_to_message,
    )
    update = Update(update_id=1, message=message)
    return update


def create_video_update(
    chat_id: int = 123,
    message_id: int = 1,
    user_id: int = 12345,
    caption: str | None = None,
    video_bytes: bytes | None = None,
    file_size: int = 1000,
    mime_type: str = "video/mp4",
    bot: Bot | None = None,
) -> Update:
    """Create a Telegram Update with a video using real PTB objects."""
    user = User(id=user_id, is_bot=False, first_name="Test", last_name="User")
    chat = Chat(id=chat_id, type="private")

    # Real Video object - only get_file() needs mocking
    video = Video(
        file_id=f"video_file_{message_id}",
        file_unique_id=f"video_unique_{message_id}",
        width=1920,
        height=1080,
        duration=10,
        mime_type=mime_type,
        file_size=file_size,
        file_name="test_video.mp4",
    )

    # Create message with real objects
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        caption=caption,
        video=video,
    )

    update = Update(update_id=100 + message_id, message=message)

    # Set bot on video so it can call get_file
    # The actual get_file mock should be set up by the test using mock_bot_get_file
    if bot:
        video.set_bot(bot)

    return update


def create_audio_update(
    chat_id: int = 123,
    message_id: int = 1,
    user_id: int = 12345,
    caption: str | None = None,
    audio_bytes: bytes | None = None,
    file_size: int = 1000,
    mime_type: str = "audio/mpeg",
    bot: Bot | None = None,
) -> Update:
    """Create a Telegram Update with audio using real PTB objects."""
    user = User(id=user_id, is_bot=False, first_name="Test", last_name="User")
    chat = Chat(id=chat_id, type="private")

    # Real Audio object
    audio = Audio(
        file_id=f"audio_file_{message_id}",
        file_unique_id=f"audio_unique_{message_id}",
        duration=180,
        mime_type=mime_type,
        file_size=file_size,
        file_name="test_audio.mp3",
    )

    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        caption=caption,
        audio=audio,
    )

    update = Update(update_id=100 + message_id, message=message)

    # Set bot on audio so it can call get_file
    # The actual get_file mock should be set up by the test using mock_bot_get_file
    if bot:
        audio.set_bot(bot)

    return update


def create_document_update(
    chat_id: int = 123,
    message_id: int = 1,
    user_id: int = 12345,
    caption: str | None = None,
    document_bytes: bytes | None = None,
    file_name: str = "document.pdf",
    mime_type: str = "application/pdf",
    file_size: int = 1000,
    bot: Bot | None = None,
) -> Update:
    """Create a Telegram Update with a document using real PTB objects."""
    user = User(id=user_id, is_bot=False, first_name="Test", last_name="User")
    chat = Chat(id=chat_id, type="private")

    # Real Document object
    document = Document(
        file_id=f"doc_file_{message_id}",
        file_unique_id=f"doc_unique_{message_id}",
        file_name=file_name,
        mime_type=mime_type,
        file_size=file_size,
    )

    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        caption=caption,
        document=document,
    )

    update = Update(update_id=100 + message_id, message=message)

    # Set bot on document so it can call get_file
    # The actual get_file mock should be set up by the test using mock_bot_get_file
    if bot:
        document.set_bot(bot)

    return update


def create_mock_update_with_photo(
    message_text: str = "",
    chat_id: int = 123,
    user_id: int = 12345,
    message_id: int = 101,
    photo_file_id: str = "test_photo_123",
    photo_bytes: bytes | None = None,
    bot: Bot | None = None,
) -> Update:
    """Creates a mock Telegram Update object for a message with a photo."""
    # Create mock photo bytes if not provided
    if photo_bytes is None:
        # Simple test image data (1x1 PNG)
        photo_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
            b"\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04"
            b"\x00\x01\xdd\x8d\xb4\x1c\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    user = User(id=user_id, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")

    # Create PhotoSize objects (Telegram sends multiple sizes)
    photo_small = PhotoSize(
        file_id=f"{photo_file_id}_small",
        file_unique_id=f"{photo_file_id}_small_unique",
        width=100,
        height=100,
        file_size=len(photo_bytes),
    )
    photo_large = PhotoSize(
        file_id=photo_file_id,
        file_unique_id=f"{photo_file_id}_unique",
        width=400,
        height=400,
        file_size=len(photo_bytes),
    )

    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=message_text,
        photo=[photo_small, photo_large],
    )

    # Set bot on photo objects so they can call get_file
    # The actual get_file mock should be set up by the test using mock_bot_get_file
    if bot:
        message.set_bot(bot)
        for photo in message.photo:
            photo.set_bot(bot)

    update = Update(update_id=1, message=message)
    return update


# ============================================================================
# Test Classes
# ============================================================================


class TestTelegramVideoToLLM:
    """Test video attachments are passed to LLM."""

    @pytest.mark.asyncio
    async def test_video_passed_to_llm(
        self, telegram_handler_fixture: TelegramHandlerTestFixture
    ) -> None:
        """Video attachment should appear in LLM content parts as data URI."""
        fix = telegram_handler_fixture
        mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)

        # Configure LLM to expect video content
        video_seen = {"value": False}

        def video_matcher(args: MatcherArgs) -> bool:
            if has_video_content(args):
                video_seen["value"] = True
                return True
            return False

        mock_llm.rules = [
            (video_matcher, LLMOutput(content="I can see and analyze your video!")),
        ]
        mock_llm.default_response = LLMOutput(content="No video found")

        # Create video update with minimal MP4 header
        video_bytes = (
            b"\x00\x00\x00\x1c"  # box size
            b"ftyp"  # box type
            b"mp42"  # major brand
            b"\x00\x00\x00\x00"  # minor version
            b"mp42"  # compatible brand 1
            b"isom"  # compatible brand 2
            b"\x00" * 100  # padding
        )
        update = create_video_update(
            caption="What's happening in this video?",
            video_bytes=video_bytes,
            bot=fix.bot,
        )
        context = create_mock_context(fix.application)

        # Mock Video.get_file at the class level (bot is frozen, can't patch directly)
        with patch.object(Video, "get_file", create_mock_get_file(video_bytes)):
            await fix.handler.message_handler(update, context)

        # Assert: Video was passed to LLM
        assert video_seen["value"], "LLM should have received video content part"
        assert len(mock_llm._calls) >= 1

    @pytest.mark.asyncio
    async def test_video_with_caption_both_passed(
        self, telegram_handler_fixture: TelegramHandlerTestFixture
    ) -> None:
        """Video + caption should result in text + video content parts."""
        fix = telegram_handler_fixture
        mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)

        caption_and_video = {"caption": False, "video": False}

        def check_both(args: MatcherArgs) -> bool:
            messages = args.get("messages", [])
            for msg in messages:
                content = getattr(msg, "content", None)
                if isinstance(content, str) and "my cat" in content.lower():
                    caption_and_video["caption"] = True
                elif isinstance(content, list):
                    for part in content:
                        if hasattr(part, "text") and "my cat" in part.text.lower():
                            caption_and_video["caption"] = True
                        elif isinstance(part, ImageUrlContentPart):
                            url = part.image_url.get("url", "")
                            if url.startswith("data:video/"):
                                caption_and_video["video"] = True
            return caption_and_video["caption"] and caption_and_video["video"]

        mock_llm.rules = [
            (check_both, LLMOutput(content="I see your cat video!")),
        ]

        video_bytes = b"\x00" * 100  # Minimal video content
        update = create_video_update(
            caption="Check out my cat doing something funny!",
            video_bytes=video_bytes,
            bot=fix.bot,
        )
        context = create_mock_context(fix.application)

        with patch.object(Video, "get_file", create_mock_get_file(video_bytes)):
            await fix.handler.message_handler(update, context)

        assert caption_and_video["caption"], "Caption text should be in LLM input"
        assert caption_and_video["video"], "Video content should be in LLM input"


class TestTelegramAudioToLLM:
    """Test audio attachments are passed to LLM."""

    @pytest.mark.asyncio
    async def test_audio_passed_to_llm(
        self, telegram_handler_fixture: TelegramHandlerTestFixture
    ) -> None:
        """Audio attachment should appear in LLM content parts."""
        fix = telegram_handler_fixture
        mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)

        audio_seen = {"value": False}

        def audio_matcher(args: MatcherArgs) -> bool:
            if has_audio_content(args):
                audio_seen["value"] = True
                return True
            return False

        mock_llm.rules = [
            (audio_matcher, LLMOutput(content="I can hear the audio!")),
        ]
        mock_llm.default_response = LLMOutput(content="No audio found")

        audio_bytes = b"ID3" + b"\x00" * 100  # Fake MP3 header
        update = create_audio_update(
            caption="What song is this?",
            audio_bytes=audio_bytes,
            bot=fix.bot,
        )
        context = create_mock_context(fix.application)

        with patch.object(Audio, "get_file", create_mock_get_file(audio_bytes)):
            await fix.handler.message_handler(update, context)

        assert audio_seen["value"], "LLM should have received audio content part"


class TestTelegramDocumentToLLM:
    """Test document attachments are passed to LLM."""

    @pytest.mark.asyncio
    async def test_pdf_passed_to_llm(
        self, telegram_handler_fixture: TelegramHandlerTestFixture
    ) -> None:
        """PDF document should appear in LLM content parts."""
        fix = telegram_handler_fixture
        mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)

        pdf_seen = {"value": False}

        def pdf_matcher(args: MatcherArgs) -> bool:
            if has_pdf_content(args):
                pdf_seen["value"] = True
                return True
            return False

        mock_llm.rules = [
            (pdf_matcher, LLMOutput(content="I can read the PDF!")),
        ]
        mock_llm.default_response = LLMOutput(content="No PDF found")

        pdf_bytes = b"%PDF-1.4\n%mock PDF content"
        update = create_document_update(
            caption="Summarize this document",
            document_bytes=pdf_bytes,
            file_name="report.pdf",
            mime_type="application/pdf",
            bot=fix.bot,
        )
        context = create_mock_context(fix.application)

        with patch.object(Document, "get_file", create_mock_get_file(pdf_bytes)):
            await fix.handler.message_handler(update, context)

        assert pdf_seen["value"], "LLM should have received PDF content part"


class TestTelegramImageStillWorks:
    """Ensure images still work after adding video/audio support."""

    @pytest.mark.asyncio
    async def test_image_passed_to_llm(
        self, telegram_handler_fixture: TelegramHandlerTestFixture
    ) -> None:
        """Image attachment should still be passed to LLM correctly."""
        fix = telegram_handler_fixture
        mock_llm = cast("RuleBasedMockLLMClient", fix.mock_llm)

        image_seen = {"value": False}

        def image_matcher(args: MatcherArgs) -> bool:
            if has_image_content(args):
                image_seen["value"] = True
                return True
            return False

        mock_llm.rules = [
            (image_matcher, LLMOutput(content="I can see the image!")),
        ]
        mock_llm.default_response = LLMOutput(content="No image found")

        # Simple test image data (1x1 PNG)
        photo_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
            b"\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04"
            b"\x00\x01\xdd\x8d\xb4\x1c\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        update = create_mock_update_with_photo(
            message_text="What's in this photo?",
            photo_bytes=photo_bytes,
            bot=fix.bot,
        )
        context = create_mock_context(fix.application)

        with patch.object(PhotoSize, "get_file", create_mock_get_file(photo_bytes)):
            await fix.handler.message_handler(update, context)

        assert image_seen["value"], "LLM should have received image content part"
