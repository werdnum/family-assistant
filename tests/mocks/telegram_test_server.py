"""Python wrapper for telegram-test-api Node.js server.

This module provides TelegramTestServer and TelegramTestClient classes
for managing the telegram-test-api subprocess and communicating with it
via HTTP for realistic Telegram bot testing.

Following patterns from radicale_server_session and HomeAssistant fixtures:
- asyncio.create_subprocess_exec for async process management
- Dynamic port allocation via find_free_port()
- Polling for server readiness
- Graceful cleanup with timeout fallback
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TypedDict

import aiohttp

logger = logging.getLogger(__name__)

# Path to the frontend directory where telegram-test-api is installed
FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"

# Path to the Node.js runner script
RUNNER_SCRIPT = Path(__file__).parent / "telegram_test_server_runner.js"


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
    """Payload for sending a message to telegram-test-api."""

    botToken: str
    date: int
    text: str
    from_: TelegramUser  # 'from' is a reserved keyword
    chat: TelegramChat
    entities: list[TelegramEntity]


# --- Response types for telegram-test-api's client API ---
# These are specific to the test server, not Telegram's actual API


class TestServerResponse(TypedDict, total=False):
    """Response from telegram-test-api's client API endpoints."""

    ok: bool


class BotMessageInUpdate(TypedDict, total=False):
    """Message sent by the bot, as returned in test server updates."""

    message_id: int
    text: str
    reply_markup: dict[str, list[list[dict[str, str]]]]  # inline_keyboard structure


class TestServerUpdate(TypedDict, total=False):
    """Update from telegram-test-api's getUpdates endpoint."""

    message: BotMessageInUpdate


class CallbackQueryPayload(TypedDict):
    """Payload for simulating a callback query via telegram-test-api."""

    botToken: str
    data: str
    from_: TelegramUser
    message: dict[str, int | TelegramChat]  # message_id, chat, date


class TelegramTestServer:
    """Manages telegram-test-api subprocess for testing.

    Follows the same patterns as Radicale/Home Assistant fixtures:
    - subprocess.Popen for process management
    - Dynamic port allocation via find_free_port()
    - Polling for server readiness
    - Graceful cleanup with timeout fallback
    """

    def __init__(self, port: int) -> None:
        """Initialize the server manager.

        Args:
            port: Port number to use (should come from find_free_port()).
        """
        self.port = port
        self.process: asyncio.subprocess.Process | None = None
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
        """Start the telegram-test-api server subprocess.

        Args:
            timeout: Maximum time to wait for server to be ready.

        Raises:
            RuntimeError: If server fails to start or become ready.
        """
        logger.info(f"Starting telegram-test-api on port {self.port}")
        logger.debug(f"Command: node {RUNNER_SCRIPT} {self.port} {self.host}")

        # Set NODE_PATH to frontend's node_modules so the runner can find telegram-test-api
        env = {**os.environ, "NODE_PATH": str(FRONTEND_DIR / "node_modules")}

        # Start the subprocess using asyncio
        self.process = await asyncio.create_subprocess_exec(
            "node",
            str(RUNNER_SCRIPT),
            str(self.port),
            self.host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Poll for server readiness
        try:
            await self._wait_for_server_ready(timeout=timeout)
            logger.info(f"telegram-test-api started successfully on port {self.port}")
        except Exception:
            # If startup fails, clean up the process
            await self.stop()
            raise

    async def _wait_for_server_ready(self, timeout: float) -> None:
        """Poll until server responds to HTTP requests.

        Similar to Radicale's socket polling and HA's endpoint polling.

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
                    f"telegram-test-api process died during startup.\n"
                    f"stdout: {stdout.decode() if stdout else ''}\n"
                    f"stderr: {stderr.decode() if stderr else ''}"
                )

            try:
                # Try to make a simple HTTP request to verify server is up
                # The bot API endpoint /getMe returns info about the bot
                async with (
                    aiohttp.ClientSession() as session,
                    session.get(
                        f"{self._api_url}/bottest_token/getMe",
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
            f"telegram-test-api did not become ready within {timeout}s. "
            f"Last error: {last_error}"
        )

    async def stop(self) -> None:
        """Stop the server with graceful termination.

        Like HA cleanup: terminate, wait with timeout, kill if needed.
        """
        if not self.process:
            return

        logger.info("Stopping telegram-test-api...")

        # Try graceful termination first
        self.process.terminate()
        try:
            # Wait up to 10 seconds for graceful shutdown
            await asyncio.wait_for(self.process.wait(), timeout=10.0)
            logger.info("telegram-test-api stopped gracefully")
        except TimeoutError:
            logger.warning("telegram-test-api did not stop gracefully, killing")
            self.process.kill()
            await self.process.wait()
            logger.info("telegram-test-api killed")

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
    """HTTP client for telegram-test-api's client endpoints.

    Uses aiohttp like Home Assistant tests for async HTTP requests.
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
            api_url: The base URL of the telegram-test-api server.
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

    def _make_message(
        self, text: str, is_command: bool = False
    ) -> TelegramMessagePayload:
        """Create a message payload for the client API.

        Args:
            text: The message text.
            is_command: Whether this is a bot command.

        Returns:
            The message payload dictionary.
        """
        message: TelegramMessagePayload = {
            "botToken": self.token,
            "date": int(time.time()),
            "text": text,
            "from_": {
                "id": self.user_id,
                "first_name": self.first_name,
                "username": self.user_name,
                "is_bot": False,
            },
            "chat": {
                "id": self.chat_id,
                "first_name": self.first_name,
                "username": self.user_name,
                "type": "private",
            },
        }

        if is_command and text.startswith("/"):
            # Add entity for bot command
            space_idx = text.find(" ")
            entity_length = space_idx if space_idx > 0 else len(text)
            message["entities"] = [
                {
                    "offset": 0,
                    "length": entity_length,
                    "type": "bot_command",
                }
            ]

        return message

    def _serialize_message(
        self, payload: TelegramMessagePayload
    ) -> dict[str, str | int | TelegramUser | TelegramChat | list[TelegramEntity]]:
        """Serialize a message payload for JSON, handling 'from' keyword.

        The Telegram API uses 'from' as a key, but it's a Python reserved word.
        We use 'from_' internally and convert it to 'from' for the API.

        Args:
            payload: The message payload with 'from_' key.

        Returns:
            A dict suitable for JSON serialization with 'from' key.
        """
        result: dict[
            str, str | int | TelegramUser | TelegramChat | list[TelegramEntity]
        ] = dict(payload)  # type: ignore[assignment]  # TypedDict to dict conversion
        if "from_" in result:
            result["from"] = result.pop("from_")
        return result

    async def send_message(self, text: str) -> TestServerResponse:
        """Simulate a user sending a message to the bot.

        Args:
            text: The message text.

        Returns:
            The API response.
        """
        message = self._make_message(text, is_command=False)
        json_payload = self._serialize_message(message)

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/sendMessage",
                json=json_payload,
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
        message = self._make_message(command, is_command=True)
        json_payload = self._serialize_message(message)

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/sendCommand",
                json=json_payload,
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
            "botToken": self.token,
            "data": callback_data,
            "from": {
                "id": self.user_id,
                "first_name": self.first_name,
                "username": self.user_name,
                "is_bot": False,
            },
            "message": {
                "message_id": message_id or 1,
                "chat": {
                    "id": self.chat_id,
                    "first_name": self.first_name,
                    "username": self.user_name,
                    "type": "private",
                },
                "date": int(time.time()),
            },
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/sendCallback",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            return await resp.json()

    async def get_updates(
        self,
        timeout: float = 5.0,
        poll_interval: float = 0.1,
    ) -> list[TestServerUpdate]:
        """Get messages that the bot has sent to this chat.

        This polls the server using getUpdatesHistory endpoint until messages
        are available or timeout. The getUpdatesHistory endpoint returns both
        bot and user messages, so we filter for bot messages only.

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
                session.post(
                    f"{self.api_url}/getUpdatesHistory",
                    json={"token": self.token},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp,
            ):
                data = await resp.json()
                result = data.get("result", [])

                logger.debug(
                    f"getUpdatesHistory returned {len(result)} items for token {self.token}"
                )
                if result:
                    logger.debug(f"First item structure: {list(result[0].keys())}")

                # Bot messages in telegram-test-api are stored differently
                # They have 'botToken' and 'message' keys
                bot_messages = [
                    update
                    for update in result
                    if update.get("botToken") and update.get("message")
                ]

                logger.debug(f"Found {len(bot_messages)} bot messages")

                # Filter by chat_id if we have messages
                # Note: telegram-test-api stores chat_id as a string directly on message,
                # not nested under message.chat.id
                if bot_messages:
                    chat_messages = [
                        msg
                        for msg in bot_messages
                        if str(msg.get("message", {}).get("chat_id", ""))
                        == str(self.chat_id)
                    ]
                    logger.debug(
                        f"Filtered to {len(chat_messages)} messages for chat_id {self.chat_id}"
                    )
                    if chat_messages and len(chat_messages) > initial_count:
                        return chat_messages

                if initial_count == 0:
                    initial_count = len(bot_messages)

            # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling for bot responses requires delay
            await asyncio.sleep(poll_interval)

        logger.debug("Timeout waiting for bot responses, returning empty list")
        return []

    async def get_updates_history(self) -> list[TestServerUpdate]:
        """Get the full history of messages for this bot token.

        Returns:
            List of all updates (both user and bot messages).
        """
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.api_url}/getUpdatesHistory",
                json={"token": self.token},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp,
        ):
            data = await resp.json()
            return data.get("result", [])
