"""A minimal sync client: the protocol's executable specification.

The iOS and web engines implement the same store and reducer; tests drive this
one against the real server, through the impairment proxy when the network is
under test, and compare its store with a client that bootstrapped from
nothing. See docs/design/mobile-state-sync.md.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from family_assistant.web.routers.chat_api import (
    ConversationMessage,
    PendingDelegation,
    PendingToolConfirmation,
)
from family_assistant.web.routers.sync_api import (
    ConversationRow,
    SyncResponse,
    TurnProgress,
)

logger = logging.getLogger(__name__)


@dataclass
class SyncStore:
    """What a client knows. Changed only by :meth:`apply`."""

    # None until the first response: the next request bootstraps.
    cursor: int | None = None
    conversations: dict[str, ConversationRow] = field(default_factory=dict)
    messages: dict[str, dict[int, ConversationMessage]] = field(default_factory=dict)
    confirmations: list[PendingToolConfirmation] = field(default_factory=list)
    delegations: dict[str, list[PendingDelegation]] = field(default_factory=dict)
    running: set[str] = field(default_factory=set)
    active_turns: dict[str, TurnProgress] = field(default_factory=dict)
    focus_seq: dict[str, int] = field(default_factory=dict)

    def apply(self, response: SyncResponse) -> None:
        """The reducer: fold one response into the store."""
        if response.bootstrap:
            self.conversations = {}
        for row in response.conversations:
            self.conversations[row.conversation_id] = row
        for conversation_id in response.removed_conversation_ids:
            self.conversations.pop(conversation_id, None)
            self.messages.pop(conversation_id, None)
        for change in response.messages:
            held = (
                {}
                if change.replaces_history
                else self.messages.setdefault(change.conversation_id, {})
            )
            for message in change.messages:
                held[message.internal_id] = message
            for internal_id in change.removed_message_ids:
                held.pop(internal_id, None)
            self.messages[change.conversation_id] = held
        if response.confirmations is not None:
            self.confirmations = list(response.confirmations)
        for conversation_id, pending in response.delegations.items():
            if pending:
                self.delegations[conversation_id] = list(pending)
            else:
                self.delegations.pop(conversation_id, None)
        self.running = set(response.running_conversation_ids)
        self.active_turns = {turn.turn_id: turn for turn in response.active_turns}
        self.focus_seq = dict(response.focus_seq)
        self.cursor = response.cursor


@dataclass
class ReferenceSyncClient:
    """Keeps a :class:`SyncStore` current with long polls.

    Every failure is answered the same way: wait, then ask again from the last
    cursor applied. There is no reconnect state to get wrong.
    """

    http: httpx.AsyncClient
    focus: set[str] = field(default_factory=set)
    store: SyncStore = field(default_factory=SyncStore)
    wait_seconds: float = 20.0
    limit: int = 500
    retry_seconds: float = 0.5
    requests: int = 0
    failures: int = 0
    _needs_history: set[str] = field(default_factory=set)
    _applied: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_until(
        self, predicate: Callable[[SyncStore], bool], timeout: float
    ) -> bool:
        """Wait until the store satisfies ``predicate``; False after ``timeout``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate(self.store):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            applied = self._applied
            try:
                await asyncio.wait_for(applied.wait(), remaining)
            except TimeoutError:
                return predicate(self.store)
        return True

    def open(self, conversation_id: str) -> None:
        """Focus a conversation; its history arrives with the next response."""
        self.focus.add(conversation_id)
        self._needs_history.add(conversation_id)

    def _params(self, wait: float) -> dict[str, str | int | float]:
        params: dict[str, str | int | float] = {"limit": self.limit, "wait": wait}
        if self.store.cursor is not None:
            params["cursor"] = self.store.cursor
        if self.focus:
            params["focus"] = ",".join(sorted(self.focus))
        if self._needs_history:
            params["fresh"] = ",".join(sorted(self._needs_history))
        if self.store.running:
            params["running"] = ",".join(sorted(self.store.running))
        if self.store.focus_seq:
            params["focus_seq"] = ",".join(
                f"{conversation_id}:{seq}"
                for conversation_id, seq in sorted(self.store.focus_seq.items())
            )
        return params

    async def sync_once(self, wait: float = 0.0) -> SyncResponse:
        """One request, applied; repeats at once while the server has more."""
        while True:
            self.requests += 1
            response = await self.http.get("/api/v1/sync", params=self._params(wait))
            response.raise_for_status()
            body = SyncResponse.model_validate(response.json())
            self.store.apply(body)
            applied, self._applied = self._applied, asyncio.Event()
            applied.set()
            for change in body.messages:
                if change.replaces_history:
                    self._needs_history.discard(change.conversation_id)
            if body.bootstrap:
                self._needs_history.clear()
            if not body.has_more:
                return body
            wait = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        """Long-poll until ``stop`` is set."""
        while not stop.is_set():
            try:
                await self.sync_once(wait=self.wait_seconds)
            except (httpx.HTTPError, OSError) as error:
                self.failures += 1
                logger.debug("sync request failed, retrying: %r", error)
                try:
                    await asyncio.wait_for(stop.wait(), self.retry_seconds)
                except TimeoutError:
                    continue
