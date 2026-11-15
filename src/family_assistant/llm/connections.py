"""SDK clients shared by every model client on the same provider endpoint.

A provider SDK client holds the expensive part of talking to a provider: an
HTTP connection pool bound to one endpoint and one credential. A model client
is a model name and its request parameters on top of one. Building an SDK
client per model client multiplied connection pools by every tier entry and
profile; borrowing from this registry keeps one per endpoint, however many
model clients use it.

The registry owns what it builds, so a model client that borrowed its SDK
client leaves closing it to the registry's owner.
"""

from __future__ import annotations

import inspect
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectionKey",
    "PlanCredentialError",
    "PlanTokenSource",
    "ProviderConnections",
]


class PlanCredentialError(Exception):
    """A subscription plan's access token could not be produced.

    The message is written for the user. ``transient`` failures may succeed on
    retry; the rest need someone to sign in again.
    """

    transient: bool = False


class PlanTokenSource(Protocol):
    """Where a subscription-plan client gets its bearer token."""

    async def access_token(self) -> str:
        """A currently valid access token; raises PlanCredentialError."""
        ...

    def reject_access_token(self) -> None:
        """Record that the provider refused the token last handed out."""
        ...


@dataclass(frozen=True)
class ConnectionKey:
    """What makes two SDK clients interchangeable."""

    provider: str
    base_url: str | None
    api_key: str

    def __repr__(self) -> str:
        # The key is logged when a connection is built; the credential is not.
        return f"ConnectionKey(provider={self.provider!r}, base_url={self.base_url!r})"


@dataclass
class _Connection:
    client: object
    close: Callable[[], Awaitable[None] | None]


class ProviderConnections:
    """Builds each provider SDK client once, and closes them all together."""

    def __init__(self) -> None:
        self._connections: dict[ConnectionKey, _Connection] = {}
        # The deployment's ChatGPT plan sign-in, installed once the database
        # is up. `provider: chatgpt` clients cannot be built without it.
        self.chatgpt_plan: PlanTokenSource | None = None
        # Some clients are built off the event loop (the tool-call reviewer
        # builds its own in a worker thread), and two builders racing for one
        # key would leave a connection nothing closes.
        self._lock = threading.Lock()

    def get[C](
        self,
        key: ConnectionKey,
        build: Callable[[], C],
        close: Callable[[C], Awaitable[None] | None],
    ) -> C:
        """The SDK client for *key*, built with *build* on first request."""
        with self._lock:
            connection = self._connections.get(key)
            if connection is None:
                client = build()
                logger.info("Created shared provider connection %r", key)
                connection = _Connection(client=client, close=lambda: close(client))
                self._connections[key] = connection
        # The key carries the provider, and each provider asks for its own
        # client type, so the stored client is the one this caller built.
        return cast("C", connection.client)

    def __len__(self) -> int:
        return len(self._connections)

    async def aclose(self) -> None:
        """Close every connection, attempting each even if one fails."""
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        first_error: Exception | None = None
        for connection in connections:
            try:
                result = connection.close()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                logger.exception("Error closing provider connection")
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error
