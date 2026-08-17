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
    MessageSendParams,
    Part,
    Role,
    Task,
    TaskIdParams,
    TaskQueryParams,
    TaskState,
    TaskStatus,
)
from family_assistant.processing.protocol import (
    DelegationPermanentError as A2APermanentError,
)
from family_assistant.processing.protocol import (
    DelegationTaskNotFoundError as A2ATaskNotFoundError,
)
from family_assistant.processing.protocol import (
    DelegationTransientError as A2AClientError,
)
from family_assistant.utils.http_status import RETRYABLE_4XX_STATUSES

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


# JSON-RPC error code the A2A spec / FA server use for an unknown task id.
_TASK_NOT_FOUND_CODE = -32001


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
                # A local misconfiguration (e.g. a missing token_env): deterministic,
                # so fail fast rather than polling until the cap on every retry.
                raise A2APermanentError(f"Auth configuration error: {exc}") from exc
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
            message = f"Failed to discover agent at {self._agent_url}: {exc}"
            # A 4xx card fetch is deterministic (bad agent-card URL / bad auth):
            # fail fast rather than polling to the cap. 5xx / network errors are
            # transient, as are the retryable 4xx statuses (408 request timeout,
            # 425 too early, 429 rate limited).
            if (
                400 <= exc.status_code < 500
                and exc.status_code not in RETRYABLE_4XX_STATUSES
            ):
                raise A2APermanentError(message) from exc
            raise A2AClientError(message) from exc
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

    async def submit(
        self,
        content_parts: list[ContentPartDict],
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Task:
        """Submit a message without blocking and return the task as-is.

        Sends a single non-streaming ``message/send`` with
        ``configuration.blocking=false``. An async-capable server returns a
        non-terminal (``working``) task immediately for the caller to poll via
        :meth:`get_task`; a synchronous server may instead return a terminal
        task, which the caller can use directly. Unlike :meth:`send_message`,
        this never blocks waiting for the task to reach a terminal state.

        Raises:
            A2AClientError: On network errors or protocol errors.
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
        params = MessageSendParams(
            message=message,
            configuration=a2a_types.MessageSendConfiguration(blocking=False),
        )
        result = await self._call_jsonrpc(
            card.url,
            "message/send",
            params.model_dump(by_alias=True, exclude_none=True),
        )
        # message/send may return a Message (immediate reply) instead of a Task;
        # treat it as a completed task, like the streaming send_message path.
        if result.get("kind") == "message":
            return self._message_to_completed_task(
                Message.model_validate(result), context_id
            )
        return Task.model_validate(result)

    async def get_task(self, task_id: str) -> Task:
        """Fetch the current state of a remote task via ``tasks/get``.

        Raises:
            A2AClientError: On network errors or protocol errors.
        """
        card = await self.discover()
        params = TaskQueryParams(id=task_id)
        result = await self._call_jsonrpc(
            card.url, "tasks/get", params.model_dump(by_alias=True, exclude_none=True)
        )
        return Task.model_validate(result)

    async def cancel_task(self, task_id: str) -> Task:
        """Request cancellation of a remote task via ``tasks/cancel``.

        Raises:
            A2AClientError: On network errors or protocol errors (including a
                task that is not cancelable).
        """
        card = await self.discover()
        params = TaskIdParams(id=task_id)
        result = await self._call_jsonrpc(
            card.url,
            "tasks/cancel",
            params.model_dump(by_alias=True, exclude_none=True),
        )
        return Task.model_validate(result)

    async def _call_jsonrpc(
        self,
        url: str,
        method: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        """POST a single JSON-RPC request and return the ``result`` object."""
        client = self._get_httpx_client()
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        try:
            response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            # Transport error (timeout / connection): the request may or may not
            # have reached the remote — transient.
            raise A2AClientError(
                f"A2A {method} request to {url} failed: {exc}"
            ) from exc
        if response.status_code != 200:
            # 4xx is a deterministic client error (auth, bad request); 5xx is a
            # transient server error that may recover. A 404 specifically means
            # the remote has no such task — a cue to re-submit, not to fail.
            # Retryable 4xx (timeout / too early / rate limited) stay transient.
            message = f"A2A {method} returned HTTP {response.status_code} from {url}"
            if response.status_code == 404:
                raise A2ATaskNotFoundError(message)
            if (
                400 <= response.status_code < 500
                and response.status_code not in RETRYABLE_4XX_STATUSES
            ):
                raise A2APermanentError(message)
            raise A2AClientError(message)
        try:
            payload = response.json()
        except ValueError as exc:
            # A 200 with a non-JSON body (e.g. a proxy / CDN HTML page) is a
            # transient infrastructure glitch, not a protocol error — keep it in
            # the A2AClientError hierarchy so the caller retries rather than
            # crashing with a raw JSONDecodeError.
            raise A2AClientError(
                f"A2A {method} returned a non-JSON response from {url}"
            ) from exc
        error = payload.get("error")
        if error:
            # The remote answered with a definitive JSON-RPC error — deterministic.
            # A task-not-found code is distinguished so the worker can re-submit a
            # run whose original message/send may never have landed.
            message = f"A2A {method} error {error.get('code')}: {error.get('message')}"
            if error.get("code") == _TASK_NOT_FOUND_CODE:
                raise A2ATaskNotFoundError(message)
            raise A2APermanentError(message)
        result = payload.get("result")
        if result is None:
            raise A2AClientError(f"A2A {method} returned no result")
        return result

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
            # A deterministic local input error: the same parts would fail
            # identically on every retry, so fail fast instead of polling to
            # the cap on the async delegation path.
            raise A2APermanentError(
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
