"""A2A client wrapper for Family Assistant integration.

Wraps the a2a-sdk client to handle agent card discovery, message sending,
and content part conversion for remote profile delegation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, NoReturn

import a2a.compat.v0_3.types as a2a_types
import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.client.errors import (
    A2AClientError as SdkClientError,
)
from a2a.client.errors import (
    A2AClientTimeoutError as SdkTimeoutError,
)
from a2a.client.errors import AgentCardResolutionError
from a2a.compat.v0_3.conversions import (
    to_compat_message,
    to_compat_task,
    to_core_message,
)
from a2a.types import (
    AgentCard as CoreAgentCard,
)
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    SendMessageConfiguration,
    SendMessageRequest,
)
from a2a.types import (
    Task as CoreTask,
)
from a2a.types import (
    TaskState as CoreTaskState,
)
from a2a.utils.errors import JSON_RPC_ERROR_CODE_MAP
from a2a.utils.errors import A2AError as SdkProtocolError
from a2a.utils.errors import TaskNotFoundError as SdkTaskNotFoundError

from family_assistant.a2a.attachments import (
    MAX_INLINE_ATTACHMENT_BYTES,
    A2AAttachmentTransfer,
)
from family_assistant.a2a.converters import text_content_parts_to_a2a_parts
from family_assistant.a2a.types import (
    Message,
    Part,
    Role,
    Task,
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

POLL_INTERVAL_SECONDS = 1.0
CORE_TERMINAL_TASK_STATES = {
    CoreTaskState.TASK_STATE_COMPLETED,
    CoreTaskState.TASK_STATE_FAILED,
    CoreTaskState.TASK_STATE_CANCELED,
    CoreTaskState.TASK_STATE_REJECTED,
    CoreTaskState.TASK_STATE_AUTH_REQUIRED,
    CoreTaskState.TASK_STATE_INPUT_REQUIRED,
}
_HTTP_STATUS_PATTERN = re.compile(r"HTTP (?:Error:? )?(\d{3})", re.IGNORECASE)


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
        attachments: A2AAttachmentTransfer | None = None,
    ) -> None:
        self._agent_url = agent_url
        self._auth_config = auth_config
        self._timeout = timeout
        self._attachments = attachments
        self._agent_card: CoreAgentCard | None = None
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

    async def discover(self) -> CoreAgentCard:
        """Fetch and cache a v1 core agent card.

        The SDK resolver normalizes both v1 cards and legacy v0.3 cards into
        the same protobuf representation. The selected interface retains its
        protocol version so :class:`ClientFactory` can choose the matching
        native or compatibility transport.
        """
        if self._agent_card is not None:
            return self._agent_card

        client = self._get_httpx_client()
        resolver = A2ACardResolver(httpx_client=client, base_url=self._agent_url)
        try:
            self._agent_card = await resolver.get_agent_card()
        except AgentCardResolutionError as exc:
            message = f"Failed to discover agent at {self._agent_url}: {exc}"
            # A 4xx card fetch is deterministic (bad agent-card URL / bad auth):
            # fail fast rather than polling to the cap. 5xx / network errors are
            # transient, as are the retryable 4xx statuses (408 request timeout,
            # 425 too early, 429 rate limited).
            if (
                exc.status_code is not None
                and 400 <= exc.status_code < 500
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
        acting_user_id: str | None = None,
    ) -> Task:
        """Send a message to the remote A2A agent and return the completed task.

        Args:
            content_parts: FA content parts to send.
            context_id: Optional context ID for conversation grouping.
            task_id: Optional task ID to continue a prior task.
            metadata: Optional metadata (e.g., profile selection).
            acting_user_id: Canonical id of the user on whose behalf the
                attachments referenced by ``content_parts`` are read.

        Returns:
            The A2A Task from the remote agent.

        Raises:
            A2AClientError: On network errors, unexpected task states, or protocol errors.
        """
        card = await self.discover()
        a2a_parts = await self._convert_and_validate_parts(
            content_parts, acting_user_id=acting_user_id
        )

        message = Message(
            role=Role.user,
            parts=a2a_parts,
            message_id=str(uuid.uuid4()),
            context_id=context_id,
            task_id=task_id,
            metadata=metadata,
        )

        try:
            a2a_client = self._create_client(card, streaming=True, polling=False)
            latest_task: CoreTask | None = None
            latest_task_id: str | None = None
            request = SendMessageRequest(message=to_core_message(message))
            async for response in a2a_client.send_message(request):
                if response.HasField("message"):
                    return self._message_to_completed_task(
                        to_compat_message(response.message), context_id
                    )
                if response.HasField("task"):
                    latest_task = response.task
                    latest_task_id = latest_task.id
                    if self._is_core_terminal(latest_task):
                        return to_compat_task(latest_task)
                elif response.HasField("status_update"):
                    latest_task_id = response.status_update.task_id
                    if response.status_update.status.state in CORE_TERMINAL_TASK_STATES:
                        task = await a2a_client.get_task(
                            GetTaskRequest(id=latest_task_id)
                        )
                        return to_compat_task(task)
            if latest_task is None:
                if latest_task_id is None:
                    raise A2AClientError("A2A agent returned no message or task")
                latest_task = await a2a_client.get_task(
                    GetTaskRequest(id=latest_task_id)
                )
            return await self._poll_until_terminal(a2a_client, latest_task)
        except (SdkProtocolError, ValueError) as exc:
            self._raise_sdk_error(exc, self._card_url(card))

    async def _poll_until_terminal(self, a2a_client: Client, task: CoreTask) -> Task:
        """Poll tasks/get for agents that return a non-terminal message/send Task."""
        deadline = time.monotonic() + self._timeout
        latest_task = task
        while not self._is_core_terminal(latest_task):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise A2AClientError(
                    f"Timed out waiting for A2A task {latest_task.id} to reach a terminal state "
                    f"(last state: {latest_task.status.state})"
                )
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
            latest_task = await a2a_client.get_task(GetTaskRequest(id=latest_task.id))
        return to_compat_task(latest_task)

    async def submit(
        self,
        content_parts: list[ContentPartDict],
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, object] | None = None,
        acting_user_id: str | None = None,
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
        a2a_parts = await self._convert_and_validate_parts(
            content_parts, acting_user_id=acting_user_id
        )
        message = Message(
            role=Role.user,
            parts=a2a_parts,
            message_id=str(uuid.uuid4()),
            context_id=context_id,
            task_id=task_id,
            metadata=metadata,
        )
        request = SendMessageRequest(
            message=to_core_message(message),
            configuration=SendMessageConfiguration(return_immediately=True),
        )
        try:
            a2a_client = self._create_client(card, streaming=False, polling=True)
            async for response in a2a_client.send_message(request):
                if response.HasField("message"):
                    return self._message_to_completed_task(
                        to_compat_message(response.message), context_id
                    )
                if response.HasField("task"):
                    return to_compat_task(response.task)
            raise A2AClientError("A2A agent returned no message or task")
        except (SdkProtocolError, ValueError) as exc:
            self._raise_sdk_error(exc, self._card_url(card))

    async def get_task(self, task_id: str) -> Task:
        """Fetch the current state of a remote task via ``tasks/get``.

        Raises:
            A2AClientError: On network errors or protocol errors.
        """
        card = await self.discover()
        try:
            client = self._create_client(card, streaming=False, polling=True)
            return to_compat_task(await client.get_task(GetTaskRequest(id=task_id)))
        except (SdkProtocolError, ValueError) as exc:
            self._raise_sdk_error(exc, self._card_url(card))

    async def cancel_task(self, task_id: str) -> Task:
        """Request cancellation of a remote task via ``tasks/cancel``.

        Raises:
            A2AClientError: On network errors or protocol errors (including a
                task that is not cancelable).
        """
        card = await self.discover()
        try:
            client = self._create_client(card, streaming=False, polling=True)
            return to_compat_task(
                await client.cancel_task(CancelTaskRequest(id=task_id))
            )
        except (SdkProtocolError, ValueError) as exc:
            self._raise_sdk_error(exc, self._card_url(card))

    def _create_client(
        self,
        card: CoreAgentCard,
        *,
        streaming: bool,
        polling: bool,
    ) -> Client:
        return ClientFactory(
            ClientConfig(
                streaming=streaming,
                polling=polling,
                httpx_client=self._get_httpx_client(),
                accepted_output_modes=list(card.default_output_modes),
            )
        ).create(card)

    @staticmethod
    def _card_url(card: CoreAgentCard) -> str:
        if card.supported_interfaces:
            return card.supported_interfaces[0].url
        return "unknown A2A endpoint"

    @staticmethod
    def _raise_sdk_error(exc: Exception, url: str) -> NoReturn:
        if isinstance(exc, SdkTaskNotFoundError):
            raise A2ATaskNotFoundError(f"A2A task not found at {url}: {exc}") from exc
        if isinstance(exc, SdkTimeoutError):
            raise A2AClientError(
                f"Timeout connecting to A2A agent at {url}: {exc}"
            ) from exc

        match = _HTTP_STATUS_PATTERN.search(str(exc))
        if match is not None:
            status_code = int(match.group(1))
            message = f"HTTP {status_code} from A2A agent at {url}: {exc}"
            if status_code == 404:
                raise A2ATaskNotFoundError(message) from exc
            if 400 <= status_code < 500 and status_code not in RETRYABLE_4XX_STATUSES:
                raise A2APermanentError(message) from exc
            raise A2AClientError(message) from exc

        if isinstance(exc, SdkClientError):
            if "Network communication error" in str(exc):
                raise A2AClientError(
                    f"Cannot connect to A2A agent at {url}: {exc}"
                ) from exc
            raise A2AClientError(
                f"A2A client error communicating with {url}: {exc}"
            ) from exc

        if isinstance(exc, SdkProtocolError):
            code = next(
                (
                    error_code
                    for error_type, error_code in JSON_RPC_ERROR_CODE_MAP.items()
                    if isinstance(exc, error_type)
                ),
                None,
            )
            detail = f"{code}: {exc}" if code is not None else str(exc)
            raise A2APermanentError(f"A2A JSON-RPC error {detail}") from exc

        raise A2APermanentError(f"Cannot create A2A client for {url}: {exc}") from exc

    @staticmethod
    def _is_core_terminal(task: CoreTask) -> bool:
        return task.status.state in CORE_TERMINAL_TASK_STATES

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

    async def _convert_and_validate_parts(
        self, content_parts: list[ContentPartDict], *, acting_user_id: str | None
    ) -> list[Part]:
        """Convert FA content parts to A2A parts with size validation.

        Attachment bytes are resolved and inlined when this client was given an
        attachment transfer; without one, an attachment part is a deterministic
        local error rather than a bare identifier on the wire.
        """
        try:
            a2a_parts = (
                await self._attachments.to_a2a_parts(
                    content_parts, acting_user_id=acting_user_id
                )
                if self._attachments is not None
                else text_content_parts_to_a2a_parts(content_parts)
            )
        except ValueError as exc:
            # Deterministic: the same content parts fail identically on retry.
            raise A2APermanentError(f"Cannot send message parts: {exc}") from exc
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
