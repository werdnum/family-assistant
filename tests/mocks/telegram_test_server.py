"""Python wrapper for telegram-bot-api-mock server.

This module provides TelegramTestServer and TelegramTestClient classes
for managing telegram-bot-api-mock and communicating with it via HTTP
for realistic Telegram bot testing.

Uses telegram-bot-api-mock Python library instead of Node.js telegram-test-api.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Any, TypedDict

import aiohttp

if TYPE_CHECKING:
    from asyncio.subprocess import Process

logger = logging.getLogger(__name__)


# --- TypedDict definitions for Telegram API structures ---


class TelegramUser(TypedDict):
    """Telegram user object."""

    id: int
    first_name: str
    username: str
    is_bot: bool


class TelegramChat(TypedDict):
    """Telegram chat object."""

    id: int
    first_name: str
    username: str
    type: str


class TelegramEntity(TypedDict):
    """Telegram message entity (for commands, etc.)."""

    offset: int
    length: int
    type: str


class TelegramMessagePayload(TypedDict, total=False):
    """Payload for sending a message to telegram-bot-api-mock."""

    bot_token: str
    chat_id: int
    text: str
    from_user: TelegramUser


# --- Response types for telegram-bot-api-mock's client API ---


class TestServerResponse(TypedDict, total=False):
    """Response from telegram-bot-api-mock's client API endpoints."""

    ok: bool
    result: Any


class BotMessageInUpdate(TypedDict, total=False):
    """Message sent by the bot, as returned in test server updates."""

    message_id: int
    text: str
    reply_markup: dict[str, list[list[dict[str, str]]]]  # inline_keyboard structure


class TestServerUpdate(TypedDict, total=False):
    """Update from telegram-bot-api-mock's getUpdates endpoint."""

    message: BotMessageInUpdate


class CallbackQueryPayload(TypedDict):
    """Payload for simulating a callback query via telegram-bot-api-mock."""

    bot_token: str
    chat_id: int
    message_id: int
    callback_data: str
    from_user: TelegramUser


class ChatAction(TypedDict):
    """Active chat action from the server."""

    chat_id: int
    action: str
    timestamp: float


class TelegramTestServer:
    """Manages telegram-bot-api-mock server for testing.

    Runs the Python FastAPI app using uvicorn in a subprocess.
    """

    def __init__(self, port: int) -> None:
        """Initialize the server manager.

        Args:
            port: Port number to use (should come from find_free_port()).
        """
        self.port = port
        self.process: Process | None = None
        self.host = "127.0.0.1"
        self._api_url = f"http://{self.host}:{self.port}"

    @property
    def api_url(self) -> str:
        """Get the base API URL for the test server."""
        return self._api_url

    def get_bot_api_url(self) -> str:
        """Get the bot API base URL for python-telegram-bot.

        This is the URL that should be passed to python-telegram-bot's
        ApplicationBuilder().base_url() method. The format matches Telegram's
        default: 'https://api.telegram.org/bot' - note the '/bot' suffix.

        python-telegram-bot will append the token to this URL, resulting in:
        '{base_url}{token}' -> 'http://host:port/bot{token}'

        Returns:
            The base URL ending with '/bot' (e.g., 'http://localhost:9000/bot').
        """
        return f"{self._api_url}/bot"

    async def start(self, timeout: float = 30.0) -> None:
        """Start the telegram-bot-api-mock server subprocess.

        Args:
            timeout: Maximum time to wait for server to be ready.

        Raises:
            RuntimeError: If server fails to start or become ready.
        """
        logger.info(f"Starting telegram-bot-api-mock on port {self.port}")

        # Start uvicorn as a subprocess running telegram-bot-api-mock
        self.process = await asyncio.create_subprocess_exec(
            "python",
            "-m",
            "uvicorn",
            "telegram_bot_api_mock:create_app",
            "--factory",
            "--host",
            self.host,
            "--port",
            str(self.port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Poll for server readiness
        try:
            await self._wait_for_server_ready(timeout=timeout)
            logger.info(
                f"telegram-bot-api-mock started successfully on port {self.port}"
            )
        except Exception:
            # If startup fails, clean up the process
            await self.stop()
            raise

    async def _wait_for_server_ready(self, timeout: float) -> None:
        """Poll until server responds to HTTP requests.

        Args:
            timeout: Maximum time to wait.

        Raises:
            RuntimeError: If server doesn't become ready in time.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_error: Exception | None = None

        while loop.time() < deadline:
            # Check if process has died (returncode is None while running)
            if self.process and self.process.returncode is not None:
                stdout, stderr = await self.process.communicate()
                raise RuntimeError(
                    f"telegram-bot-api-mock process died during startup.\n"
                    f"stdout: {stdout.decode() if stdout else ''}\n"
                    f"stderr: {stderr.decode() if stderr else ''}"
                )

            try:
                # Try to make a simple HTTP request to verify server is up
                # The bot API endpoint /getMe returns info about the bot
                # Use a valid token format: <numeric_id>:<secret>
                async with (
                    aiohttp.ClientSession() as session,
                    session.get(
                        f"{self._api_url}/bot123456789:testcheck/getMe",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as resp,
                ):
                    # Server is ready if we get any response (even an error)
                    if resp.status in {200, 401, 404}:
                        return
            except (TimeoutError, aiohttp.ClientError) as e:
                last_error = e
                # ast-grep-ignore: no-asyncio-sleep-in-tests - Server startup polling requires delay
                await asyncio.sleep(0.5)

        raise RuntimeError(
            f"telegram-bot-api-mock did not become ready within {timeout}s. "
            f"Last error: {last_error}"
        )

    async def stop(self) -> None:
        """Stop the server with graceful termination."""
        if not self.process:
            return

        logger.info("Stopping telegram-bot-api-mock...")

        # Try graceful termination first
        self.process.terminate()
        try:
            # Wait up to 10 seconds for graceful shutdown
            await asyncio.wait_for(self.process.wait(), timeout=10.0)
            logger.info("telegram-bot-api-mock stopped gracefully")
        except TimeoutError:
            logger.warning("telegram-bot-api-mock did not stop gracefully, killing")
            self.process.kill()
            await self.process.wait()
            logger.info("telegram-bot-api-mock killed")

        self.process = None

    def get_client(
        self,
        token: str,
        user_id: int = 1,
        chat_id: int = 1,
        first_name: str = "TestUser",
        user_name: str = "testuser",
    ) -> TelegramTestClient:
        """Get a client for simulating user messages.

        Args:
            token: The bot token to interact with.
            user_id: The simulated user's ID.
            chat_id: The chat ID for messages.
            first_name: The user's first name.
            user_name: The user's username.

        Returns:
            A TelegramTestClient instance.
        """
        return TelegramTestClient(
            api_url=self._api_url,
            token=token,
            user_id=user_id,
            chat_id=chat_id,
            first_name=first_name,
            user_name=user_name,
        )


class TelegramTestClient:
    """HTTP client for telegram-bot-api-mock's client endpoints.

    Uses aiohttp for async HTTP requests.
    This client simulates a Telegram user sending messages to the bot.
    """

    def __init__(
        self,
        api_url: str,
        token: str,
        user_id: int = 1,
        chat_id: int = 1,
        first_name: str = "TestUser",
        user_name: str = "testuser",
    ) -> None:
        """Initialize the client.

        Args:
            api_url: The base URL of the telegram-bot-api-mock server.
            token: The bot token.
            user_id: The simulated user's ID.
            chat_id: The chat ID for messages.
            first_name: The user's first name.
            user_name: The user's username.
        """
        self.api_url = api_url
        self.token = token
        self.user_id = user_id
        self.chat_id = chat_id
        self.first_name = first_name
        self.user_name = user_name

    def _make_from_user(self) -> TelegramUser:
        """Create the from_user object for requests.

        Returns:
            A TelegramUser dictionary.
        """
        return {
            "id": self.user_id,
            "first_name": self.first_name,
            "username": self.user_name,
            "is_bot": False,
        }

    async def send_message(self, text: str) -> TestServerResponse:
        """Simulate a user sending a message to the bot.

        Args:
            text: The message text.

        Returns:
            The API response.
        """
        payload = {
            "bot_token": self.token,
            "chat_id": self.chat_id,
            "text": text,
            "from_user": self._make_from_user(),
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/client/sendMessage",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            return await resp.json()

    async def send_command(self, command: str) -> TestServerResponse:
        """Simulate a user sending a bot command.

        Args:
            command: The command text (e.g., "/start", "/help").

        Returns:
            The API response.
        """
        payload = {
            "bot_token": self.token,
            "chat_id": self.chat_id,
            "command": command,
            "from_user": self._make_from_user(),
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/client/sendCommand",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            return await resp.json()

    async def send_callback(
        self, callback_data: str, message_id: int | None = None
    ) -> TestServerResponse:
        """Simulate a user clicking an inline keyboard button.

        Args:
            callback_data: The callback data from the button.
            message_id: The message ID (optional).

        Returns:
            The API response.
        """
        payload = {
            "bot_token": self.token,
            "chat_id": self.chat_id,
            "message_id": message_id or 1,
            "callback_data": callback_data,
            "from_user": self._make_from_user(),
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/client/sendCallback",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            return await resp.json()

    async def send_photo(
        self,
        photo_content: bytes,
        filename: str = "photo.jpg",
        caption: str | None = None,
    ) -> TestServerResponse:
        """Simulate a user sending a photo to the bot.

        Args:
            photo_content: The raw photo bytes.
            filename: The filename for the photo.
            caption: Optional caption for the photo.

        Returns:
            The API response containing the created Update.
        """
        # ast-grep-ignore: no-dict-any - Flexible JSON payload for HTTP request
        payload: dict[str, Any] = {
            "bot_token": self.token,
            "chat_id": self.chat_id,
            "photo": base64.b64encode(photo_content).decode("utf-8"),
            "filename": filename,
            "from_user": self._make_from_user(),
        }
        if caption is not None:
            payload["caption"] = caption

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/client/sendPhoto",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp,
        ):
            return await resp.json()

    async def send_video(
        self,
        video_content: bytes,
        filename: str = "video.mp4",
        caption: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
    ) -> TestServerResponse:
        """Simulate a user sending a video to the bot.

        Args:
            video_content: The raw video bytes.
            filename: The filename for the video.
            caption: Optional caption for the video.
            width: Video width in pixels.
            height: Video height in pixels.
            duration: Video duration in seconds.

        Returns:
            The API response containing the created Update.
        """
        # ast-grep-ignore: no-dict-any - Flexible JSON payload for HTTP request
        payload: dict[str, Any] = {
            "bot_token": self.token,
            "chat_id": self.chat_id,
            "video": base64.b64encode(video_content).decode("utf-8"),
            "filename": filename,
            "from_user": self._make_from_user(),
        }
        if caption is not None:
            payload["caption"] = caption
        if width is not None:
            payload["width"] = width
        if height is not None:
            payload["height"] = height
        if duration is not None:
            payload["duration"] = duration

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/client/sendVideo",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp,
        ):
            return await resp.json()

    async def send_audio(
        self,
        audio_content: bytes,
        filename: str = "audio.mp3",
        caption: str | None = None,
        duration: int | None = None,
        performer: str | None = None,
        title: str | None = None,
    ) -> TestServerResponse:
        """Simulate a user sending an audio file to the bot.

        Args:
            audio_content: The raw audio bytes.
            filename: The filename for the audio.
            caption: Optional caption for the audio.
            duration: Audio duration in seconds.
            performer: Performer of the audio.
            title: Title of the audio track.

        Returns:
            The API response containing the created Update.
        """
        # ast-grep-ignore: no-dict-any - Flexible JSON payload for HTTP request
        payload: dict[str, Any] = {
            "bot_token": self.token,
            "chat_id": self.chat_id,
            "audio": base64.b64encode(audio_content).decode("utf-8"),
            "filename": filename,
            "from_user": self._make_from_user(),
        }
        if caption is not None:
            payload["caption"] = caption
        if duration is not None:
            payload["duration"] = duration
        if performer is not None:
            payload["performer"] = performer
        if title is not None:
            payload["title"] = title

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/client/sendAudio",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp,
        ):
            return await resp.json()

    async def send_document(
        self,
        document_content: bytes,
        filename: str,
        caption: str | None = None,
        mime_type: str | None = None,
    ) -> TestServerResponse:
        """Simulate a user sending a document to the bot.

        Args:
            document_content: The raw document bytes.
            filename: The filename for the document.
            caption: Optional caption for the document.
            mime_type: MIME type of the document.

        Returns:
            The API response containing the created Update.
        """
        # ast-grep-ignore: no-dict-any - Flexible JSON payload for HTTP request
        payload: dict[str, Any] = {
            "bot_token": self.token,
            "chat_id": self.chat_id,
            "document": base64.b64encode(document_content).decode("utf-8"),
            "filename": filename,
            "from_user": self._make_from_user(),
        }
        if caption is not None:
            payload["caption"] = caption
        if mime_type is not None:
            payload["mime_type"] = mime_type

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/client/sendDocument",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp,
        ):
            return await resp.json()

    async def get_media(self, file_id: str) -> tuple[bytes, str, str] | None:
        """Download media content by file_id.

        Args:
            file_id: The unique identifier of the file.

        Returns:
            Tuple of (content_bytes, filename, mime_type) or None if not found.
        """
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{self.api_url}/client/getMedia/{file_id}",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp,
        ):
            if resp.status == 404:
                return None
            content = await resp.read()
            # Extract filename from Content-Disposition header
            content_disposition = resp.headers.get("Content-Disposition", "")
            filename = "unknown"
            if 'filename="' in content_disposition:
                start = content_disposition.index('filename="') + len('filename="')
                end = content_disposition.index('"', start)
                filename = content_disposition[start:end]
            mime_type = resp.headers.get("Content-Type", "application/octet-stream")
            return content, filename, mime_type

    async def get_updates(
        self,
        timeout: float = 5.0,
        poll_interval: float = 0.1,
    ) -> list[TestServerUpdate]:
        """Get messages that the bot has sent to this chat.

        This polls the server's client/getUpdates endpoint until messages
        are available or timeout.

        Args:
            timeout: Maximum time to wait for updates.
            poll_interval: Time between poll attempts.

        Returns:
            List of updates (messages) from the bot.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        initial_count = 0  # Track initial message count to detect new messages

        while loop.time() < deadline:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    f"{self.api_url}/client/getUpdates",
                    params={"bot_token": self.token, "chat_id": self.chat_id},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp,
            ):
                data = await resp.json()
                result = data.get("result", [])

                logger.debug(
                    f"getUpdates returned {len(result)} items for token {self.token}"
                )

                if result and len(result) > initial_count:
                    return result

                if initial_count == 0:
                    initial_count = len(result)

            # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling for bot responses requires delay
            await asyncio.sleep(poll_interval)

        logger.debug("Timeout waiting for bot responses, returning empty list")
        return []

    async def get_updates_history(self) -> list[TestServerUpdate]:
        """Get the full history of updates for this bot token.

        Returns:
            List of all updates (both user and bot messages).
        """
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{self.api_url}/client/getUpdatesHistory",
                params={"bot_token": self.token},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp,
        ):
            data = await resp.json()
            return data.get("result", [])

    async def get_chat_actions(self) -> list[ChatAction]:
        """Get active chat actions for this chat.

        Chat actions (typing indicators, etc.) expire after 5 seconds.

        Returns:
            List of active chat actions.
        """
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{self.api_url}/client/getChatActions",
                params={"bot_token": self.token, "chat_id": self.chat_id},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp,
        ):
            data = await resp.json()
            return data.get("result", [])
