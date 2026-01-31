"""
Functional tests verifying Telegram attachments are passed to LLM.

Tests use real AttachmentRegistry and ProcessingService, mocking only
the LLM responses via RuleBasedMockLLMClient.

These tests verify that video, audio, and document attachments sent via Telegram
are correctly passed to the LLM, not just images.

Uses telegram-bot-api-mock for realistic HTTP-level testing - no mocking of
get_file or other Telegram API methods.
"""

from typing import Any, cast

import pytest
from telegram import Update
from telegram.ext import Application, ContextTypes

from family_assistant.llm.messages import ImageUrlContentPart
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

from .conftest import TelegramHandlerTestFixture

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
    application: Application[Any, Any, Any, Any, Any, Any],
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

        # Create video with minimal MP4 header
        video_bytes = (
            b"\x00\x00\x00\x1c"  # box size
            b"ftyp"  # box type
            b"mp42"  # major brand
            b"\x00\x00\x00\x00"  # minor version
            b"mp42"  # compatible brand 1
            b"isom"  # compatible brand 2
            b"\x00" * 100  # padding
        )

        # Send video via the test client - this makes a real HTTP request
        result = await fix.telegram_client.send_video(
            video_content=video_bytes,
            filename="test_video.mp4",
            caption="What's happening in this video?",
        )

        # Parse the response into an Update object
        assert result.get("ok") is True, f"Failed to send video: {result}"
        update_data = result.get("result", {})
        update = Update.de_json(data=update_data, bot=fix.bot)
        assert update is not None
        assert update.message is not None

        # Create context and call the handler
        context = create_mock_context(fix.application)
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

        # Send video via the test client
        result = await fix.telegram_client.send_video(
            video_content=video_bytes,
            filename="cat_video.mp4",
            caption="Check out my cat doing something funny!",
        )

        # Parse the response into an Update object
        assert result.get("ok") is True
        update = Update.de_json(data=result.get("result", {}), bot=fix.bot)
        assert update is not None
        context = create_mock_context(fix.application)

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

        # Send audio via the test client
        result = await fix.telegram_client.send_audio(
            audio_content=audio_bytes,
            filename="test_audio.mp3",
            caption="What song is this?",
        )

        # Parse the response into an Update object
        assert result.get("ok") is True, f"Failed to send audio: {result}"
        update = Update.de_json(data=result.get("result", {}), bot=fix.bot)
        assert update is not None
        context = create_mock_context(fix.application)

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

        # Send document via the test client
        result = await fix.telegram_client.send_document(
            document_content=pdf_bytes,
            filename="report.pdf",
            caption="Summarize this document",
            mime_type="application/pdf",
        )

        # Parse the response into an Update object
        assert result.get("ok") is True, f"Failed to send document: {result}"
        update = Update.de_json(data=result.get("result", {}), bot=fix.bot)
        assert update is not None
        context = create_mock_context(fix.application)

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

        # Send photo via the test client
        result = await fix.telegram_client.send_photo(
            photo_content=photo_bytes,
            filename="test_image.png",
            caption="What's in this photo?",
        )

        # Parse the response into an Update object
        assert result.get("ok") is True, f"Failed to send photo: {result}"
        update = Update.de_json(data=result.get("result", {}), bot=fix.bot)
        assert update is not None
        context = create_mock_context(fix.application)

        await fix.handler.message_handler(update, context)

        assert image_seen["value"], "LLM should have received image content part"
