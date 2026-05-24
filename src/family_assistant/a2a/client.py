"""A2A client wrapper for Family Assistant integration.

Wraps the a2a-sdk client to handle agent card discovery, message sending,
and content part conversion for remote profile delegation.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

import a2a.types as a2a_types
import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.client.errors import (
    A2AClientError as SdkClientError,
)
from a2a.client.errors import (
    A2AClientHTTPError as SdkHTTPError,
)
from a2a.client.errors import (
    A2AClientJSONRPCError as SdkJSONRPCError,
)
from a2a.client.errors import (
    A2AClientTimeoutError as SdkTimeoutError,
)

from family_assistant.a2a.converters import content_parts_to_a2a_parts
from family_assistant.a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    Task,
    TaskQueryParams,
    TaskState,
    TaskStatus,
)

if TYPE_CHECKING:
    from family_assistant.a2a.auth import A2AAuthConfig
    from family_assistant.llm.content_parts import ContentPartDict

logger = logging.getLogger(__name__)

# Limit on base64-encoded size in the JSON-RPC payload (not decoded file size).
# Base64 inflates by ~33%, so this allows ~7.5 MB raw files.
MAX_INLINE_ATTACHMENT_BYTES = 10 * 1024 * 1024
POLL_INTERVAL_SECONDS = 1.0
TERMINAL_TASK_STATES = {
    TaskState.completed,
    TaskState.failed,
    TaskState.canceled,
    TaskState.rejected,
    TaskState.auth_required,
    TaskState.input_required,
}


class A2AClientError(Exception):
    """Base error for A2A client operations."""


class A2AClientWrapper:
    """Wraps the a2a-sdk client for Family Assistant integration.

    Handles agent card discovery/caching, message sending via JSON-RPC,
    and content part conversion using existing FA converters.
    """

    def __init__(
        self,
        agent_url: str,
        auth_config: A2AAuthConfig | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._agent_url = agent_url
        self._auth_config = auth_config
        self._timeout = timeout
        self._agent_card: AgentCard | None = None
        self._httpx_client: httpx.AsyncClient | None = None

    def _get_httpx_client(self) -> httpx.AsyncClient:
        if self._httpx_client is None or self._httpx_client.is_closed:
            try:
                auth = self._auth_config.to_httpx_auth() if self._auth_config else None
            except ValueError as exc:
                raise A2AClientError(f"Auth configuration error: {exc}") from exc
            self._httpx_client = httpx.AsyncClient(
                auth=auth,
                timeout=httpx.Timeout(self._timeout),
            )
        return self._httpx_client

    async def discover(self) -> AgentCard:
        """Fetch and cache the agent card."""
        if self._agent_card is not None:
            return self._agent_card

        client = self._get_httpx_client()
        resolver = A2ACardResolver(httpx_client=client, base_url=self._agent_url)
        try:
            self._agent_card = await resolver.get_agent_card()
        except SdkHTTPError as exc:
            raise A2AClientError(
                f"Failed to discover agent at {self._agent_url}: {exc}"
            ) from exc
        logger.info(
            "Discovered A2A agent '%s' at %s with %d skills",
            self._agent_card.name,
            self._agent_url,
            len(self._agent_card.skills) if self._agent_card.skills else 0,
        )
        return self._agent_card

    async def send_message(
        self,
        content_parts: list[ContentPartDict],
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Task:
        """Send a message to the remote A2A agent and return the completed task.

        Args:
            content_parts: FA content parts to send.
            context_id: Optional context ID for conversation grouping.
            task_id: Optional task ID to continue a prior task.
            metadata: Optional metadata (e.g., profile selection).

        Returns:
            The A2A Task from the remote agent.

        Raises:
            A2AClientError: On network errors, unexpected task states, or protocol errors.
        """
        card = await self.discover()
        a2a_parts = self._convert_and_validate_parts(content_parts)

        message = Message(
            role=Role.user,
            parts=a2a_parts,
            message_id=str(uuid.uuid4()),
            context_id=context_id,
            task_id=task_id,
            metadata=metadata,
        )

        client = self._get_httpx_client()
        try:
            a2a_client = ClientFactory(
                ClientConfig(
                    streaming=True,
                    polling=False,
                    httpx_client=client,
                    accepted_output_modes=card.default_output_modes or [],
                )
            ).create(card)
            latest_task: Task | None = None
            async for event in a2a_client.send_message(message):
                if isinstance(event, Message):
                    return self._message_to_completed_task(event, context_id)
                task, _update = event
                latest_task = task
                if self._is_terminal(task):
                    return task
            if latest_task is None:
                raise A2AClientError("A2A agent returned no message or task")
            return await self._poll_until_terminal(a2a_client, latest_task)
        except SdkClientError as exc:
            raise A2AClientError(self._format_sdk_error(exc, card.url)) from exc

    async def _poll_until_terminal(self, a2a_client: Client, task: Task) -> Task:
        """Poll tasks/get for agents that return a non-terminal message/send Task."""
        deadline = time.monotonic() + self._timeout
        latest_task = task
        while not self._is_terminal(latest_task):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise A2AClientError(
                    f"Timed out waiting for A2A task {latest_task.id} to reach a terminal state "
                    f"(last state: {latest_task.status.state})"
                )
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
            latest_task = await a2a_client.get_task(TaskQueryParams(id=latest_task.id))
        return latest_task

    @staticmethod
    def _is_terminal(task: Task) -> bool:
        return task.status.state in TERMINAL_TASK_STATES

    @staticmethod
    def _message_to_completed_task(
        message: Message, fallback_context_id: str | None
    ) -> Task:
        return Task(
            id=message.task_id or str(uuid.uuid4()),
            context_id=message.context_id or fallback_context_id or str(uuid.uuid4()),
            status=TaskStatus(state=TaskState.completed, message=message),
            history=[message],
        )

    @staticmethod
    def _format_sdk_error(exc: SdkClientError, url: str) -> str:
        if isinstance(exc, SdkJSONRPCError):
            return f"A2A JSON-RPC error {exc.error.code}: {exc.error.message}"
        if isinstance(exc, SdkTimeoutError):
            return f"Timeout connecting to A2A agent at {url}: {exc.message}"
        if isinstance(exc, SdkHTTPError):
            if exc.status_code == 503 and "Network communication error" in exc.message:
                return f"Cannot connect to A2A agent at {url}: {exc.message}"
            return f"HTTP {exc.status_code} from A2A agent at {url}: {exc.message}"
        return f"A2A client error communicating with {url}: {exc}"

    def _convert_and_validate_parts(
        self, content_parts: list[ContentPartDict]
    ) -> list[Part]:
        """Convert FA content parts to A2A parts with size validation."""
        a2a_parts = content_parts_to_a2a_parts(content_parts)
        for part in a2a_parts:
            self._validate_part_size(part)
        return a2a_parts

    @staticmethod
    def _validate_part_size(part: Part) -> None:
        """Reject inline file parts exceeding MAX_INLINE_ATTACHMENT_BYTES."""
        inner = part.root
        if not isinstance(inner, a2a_types.FilePart):
            return
        file = inner.file
        if not isinstance(file, a2a_types.FileWithBytes):
            return
        byte_len = len(file.bytes) if file.bytes else 0
        if byte_len > MAX_INLINE_ATTACHMENT_BYTES:
            raise A2AClientError(
                f"Inline attachment size ({byte_len} bytes) exceeds "
                f"limit ({MAX_INLINE_ATTACHMENT_BYTES} bytes). "
                f"Consider using URI-based file transfer."
            )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._httpx_client and not self._httpx_client.is_closed:
            await self._httpx_client.aclose()
            self._httpx_client = None
        self._agent_card = None
