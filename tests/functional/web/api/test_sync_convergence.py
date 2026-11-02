"""Whatever happens and however a client pages through it, it converges.

Each case is a seeded random walk of writes (replies, new conversations,
deletions, in-place edits, read markers, confirmations, delegations) with the
client syncing at random points, at random page sizes, opening conversations
as it goes. At the end the client must hold exactly what a client that
bootstrapped from nothing holds. A failing seed replays exactly.

See docs/design/mobile-state-sync.md, "Deterministic client scenarios".
"""

import random
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate
from tests.sync.reference_client import ReferenceSyncClient, SyncStore

OWNER = "test_user"
STEPS = 60


@pytest_asyncio.fixture
async def http(app_fixture: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = ASGITransport(app=app_fixture)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


class _World:
    """Writes to the database the way the application's writers do."""

    def __init__(self, db: Database, rng: random.Random) -> None:
        self.db = db
        self.rng = rng
        self.conversations: list[str] = []
        self.pending_confirmations: list[str] = []
        self.pending_delegations: list[str] = []

    async def new_conversation(self) -> None:
        conversation_id = str(uuid.uuid4())
        await self.db.message_history.add_message(
            UserMessage.from_trusted_user(content=f"start {len(self.conversations)}"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
            user_id=OWNER,
        )
        self.conversations.append(conversation_id)

    async def reply(self) -> None:
        await self.db.message_history.add_message(
            AssistantMessage(content=f"reply {self.rng.random():.6f}"),
            interface_type="web",
            conversation_id=self.rng.choice(self.conversations),
            timestamp=datetime.now(UTC),
        )

    async def _some_assistant_message(self) -> int | None:
        rows = await self.db.fetch_all(
            select(message_history_table.c.internal_id).where(
                message_history_table.c.role == "assistant"
            )
        )
        return self.rng.choice(rows)["internal_id"] if rows else None

    async def delete_reply(self) -> None:
        internal_id = await self._some_assistant_message()
        if internal_id is not None:
            await self.db.execute(
                message_history_table.delete().where(
                    message_history_table.c.internal_id == internal_id
                )
            )

    async def edit_reply(self) -> None:
        internal_id = await self._some_assistant_message()
        if internal_id is not None:
            await self.db.message_history.update_error_traceback(
                internal_id, f"trace {self.rng.random():.6f}"
            )

    async def read(self) -> None:
        await self.db.conversation_threads.mark_read(
            self.rng.choice(self.conversations),
            viewer_user_id=OWNER,
            through_message_id=self.rng.randint(1, 10_000),
            now=datetime.now(UTC),
        )

    async def ask_confirmation(self) -> None:
        request_id = str(uuid.uuid4())
        await self.db.confirmation_requests.create(
            request_id=request_id,
            target_user_id=OWNER,
            tool_name="list_notes",
            tool_args={},
            tool_call_id=None,
            source_message_internal_id=None,
            confirmation_prompt="Allow?",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.pending_confirmations.append(request_id)

    async def answer_confirmation(self) -> None:
        if not self.pending_confirmations:
            return
        request_id = self.pending_confirmations.pop(
            self.rng.randrange(len(self.pending_confirmations))
        )
        await self.db.confirmation_requests.reject_pending(
            request_id=request_id,
            resolving_user_id=OWNER,
            resolving_interface="web",
            now=datetime.now(UTC),
        )

    async def delegate(self) -> None:
        delegation_id = f"run-{uuid.uuid4().hex[:8]}"
        await self.db.delegation_runs.create_run(
            DelegationRunCreate(
                delegation_id=delegation_id,
                task_id=f"task-{delegation_id}",
                source_profile_id="default_assistant",
                target_service_id="research_profile",
                interface_type="web",
                conversation_id=self.rng.choice(self.conversations),
                subconversation_id=f"sub-{delegation_id}",
                request_text="Please look into it.",
                content_parts_json=[],
                user_id=OWNER,
            )
        )
        await self.db.delegation_runs.mark_handed_off(delegation_id, datetime.now(UTC))
        self.pending_delegations.append(delegation_id)

    async def deliver_delegation(self) -> None:
        if not self.pending_delegations:
            return
        delegation_id = self.pending_delegations.pop(
            self.rng.randrange(len(self.pending_delegations))
        )
        await self.db.delegation_runs.mark_notified(
            delegation_id=delegation_id,
            result_message_internal_id=None,
            notified_at=datetime.now(UTC),
        )


def _comparable(store: SyncStore) -> tuple[object, ...]:
    return (
        store.conversations,
        store.messages,
        sorted(confirmation.request_id for confirmation in store.confirmations),
        store.delegations,
    )


@pytest.mark.parametrize("seed", range(8))
async def test_an_incremental_client_converges_to_a_fresh_bootstrap(
    seed: int, http: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    rng = random.Random(seed)
    world = _World(Database(engine=db_engine), rng)
    await world.new_conversation()
    client = ReferenceSyncClient(http=http)
    writes = [
        world.new_conversation,
        world.reply,
        world.reply,
        world.reply,
        world.delete_reply,
        world.edit_reply,
        world.read,
        world.ask_confirmation,
        world.answer_confirmation,
        world.delegate,
        world.deliver_delegation,
    ]

    for _ in range(STEPS):
        roll = rng.random()
        if roll < 0.25:
            client.limit = rng.choice([1, 2, 3, 500])
            await client.sync_once()
        elif roll < 0.3:
            client.open(rng.choice(world.conversations))
        else:
            await rng.choice(writes)()
    client.limit = 500
    await client.sync_once()

    fresh = ReferenceSyncClient(http=http)
    for conversation_id in client.focus:
        fresh.open(conversation_id)
    await fresh.sync_once()

    assert _comparable(client.store) == _comparable(fresh.store)
