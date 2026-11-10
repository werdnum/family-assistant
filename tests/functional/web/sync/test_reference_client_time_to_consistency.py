"""How long the sync protocol's reference client takes to show a change.

The same scenarios and profiles as the web client's benchmark, driven against
``GET /v1/sync`` long polls instead of the event streams, so both clients land
in one table. Changes are committed straight to the database over a perfect
link; only the client's requests cross the impairment proxy.

    SYNC_BENCHMARK=1 pytest tests/functional/web/sync -n 0 --db sqlite -q -s

See docs/design/mobile-state-sync.md, "Benchmark: time-to-consistency".
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.storage.database import Database
from family_assistant.web.routers.sync_api import SyncResponse
from tests.functional.web.conftest import find_free_port_with_socket
from tests.functional.web.sync.benchmark import (
    ScenarioResult,
    full_run_requested,
    runs_per_scenario,
)
from tests.helpers import wait_for_condition
from tests.netem.profiles import PROFILES
from tests.netem.proxy import ImpairedNetworkProxy
from tests.sync.reference_client import ReferenceSyncClient, SyncStore

OWNER = "test_user"

_FULL = full_run_requested()
_PROFILE_NAMES = list(PROFILES) if _FULL else ["good", "hotel_wifi"]
_SCENARIOS = (
    [
        "cold_load",
        "commit_during_load",
        "connected_idle",
        "after_blackhole",
        "after_handover",
        "other_conversation",
    ]
    if _FULL
    else ["connected_idle", "after_handover", "commit_during_load"]
)
_DEADLINE_S = 60.0 if _FULL else 30.0
_BLACKHOLE_S = 20.0 if _FULL else 3.0
_REGRESSION_BOUND_S = 5.0
_LONG_POLL_S = 20.0


@pytest_asyncio.fixture
async def live_server_port(app_fixture: FastAPI) -> AsyncGenerator[int]:
    port, sock = find_free_port_with_socket()
    server = uvicorn.Server(
        uvicorn.Config(app_fixture, log_level="warning", lifespan="off")
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    await wait_for_condition(lambda: server.started, timeout=10, description="server")
    yield port
    server.should_exit = True
    await serving
    sock.close()


@pytest_asyncio.fixture
async def proxy(live_server_port: int) -> AsyncGenerator[ImpairedNetworkProxy]:
    async with ImpairedNetworkProxy(
        "127.0.0.1", live_server_port, seed=live_server_port
    ) as impaired:
        yield impaired


@contextlib.asynccontextmanager
async def _running_client(
    proxy: ImpairedNetworkProxy, *, focus: str
) -> AsyncGenerator[ReferenceSyncClient]:
    timeout = httpx.Timeout(
        connect=10.0, read=_LONG_POLL_S + 15.0, write=10.0, pool=10.0
    )
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{proxy.port}", timeout=timeout
    ) as http:
        client = ReferenceSyncClient(http=http, wait_seconds=_LONG_POLL_S)
        client.open(focus)
        stop = asyncio.Event()
        loop_task = asyncio.create_task(client.run(stop))
        try:
            yield client
        finally:
            stop.set()
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task


def _shows_reply(conversation_id: str, text: str) -> Callable[[SyncStore], bool]:
    def predicate(store: SyncStore) -> bool:
        return any(
            message.content == text
            for message in store.messages.get(conversation_id, {}).values()
        )

    return predicate


def _lists_reply(conversation_id: str, text: str) -> Callable[[SyncStore], bool]:
    def predicate(store: SyncStore) -> bool:
        row = store.conversations.get(conversation_id)
        return row is not None and row.last_message == text

    return predicate


async def _new_conversation(db: Database) -> str:
    conversation_id = str(uuid.uuid4())
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="hello"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id=OWNER,
    )
    return conversation_id


async def _commit_reply(db: Database, conversation_id: str, text: str) -> float:
    await db.message_history.add_message(
        AssistantMessage(content=text),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
    )
    return time.monotonic()


def _elapsed(seen: bool, since: float) -> float | None:
    return max(0.0, time.monotonic() - since) if seen else None


class _Scenarios:
    def __init__(
        self,
        db: Database,
        proxy: ImpairedNetworkProxy,
        client: ReferenceSyncClient,
        focus: str,
        other: str,
    ) -> None:
        self.db = db
        self.proxy = proxy
        self.client = client
        self.focus = focus
        self.other = other

    async def connected_idle(self, text: str) -> float | None:
        committed = await _commit_reply(self.db, self.focus, text)
        return _elapsed(
            await self.client.wait_until(_shows_reply(self.focus, text), _DEADLINE_S),
            committed,
        )

    async def after_blackhole(self, text: str) -> float | None:
        self.proxy.blackhole(_BLACKHOLE_S)
        outage_ends = time.monotonic() + _BLACKHOLE_S
        committed = await _commit_reply(self.db, self.focus, text)
        seen = await self.client.wait_until(
            _shows_reply(self.focus, text), _BLACKHOLE_S + _DEADLINE_S
        )
        return _elapsed(seen, max(committed, outage_ends))

    async def after_handover(self, text: str) -> float | None:
        self.proxy.reset_all_connections()
        committed = await _commit_reply(self.db, self.focus, text)
        return _elapsed(
            await self.client.wait_until(_shows_reply(self.focus, text), _DEADLINE_S),
            committed,
        )

    async def other_conversation(self, text: str) -> float | None:
        committed = await _commit_reply(self.db, self.other, text)
        return _elapsed(
            await self.client.wait_until(_lists_reply(self.other, text), _DEADLINE_S),
            committed,
        )

    async def cold_load(self, text: str) -> float | None:
        await _commit_reply(self.db, self.focus, text)
        started = time.monotonic()
        async with _running_client(self.proxy, focus=self.focus) as fresh:
            seen = await fresh.wait_until(_shows_reply(self.focus, text), _DEADLINE_S)
        return _elapsed(seen, started)

    async def commit_during_load(self, text: str) -> float | None:
        async with _running_client(self.proxy, focus=self.focus) as fresh:
            committed = await _commit_reply(self.db, self.focus, text)
            seen = await fresh.wait_until(_shows_reply(self.focus, text), _DEADLINE_S)
        return _elapsed(seen, committed)

    def runner(self, name: str) -> Callable[[str], Awaitable[float | None]]:
        return getattr(self, name)


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
@pytest.mark.parametrize("scenario", _SCENARIOS)
async def test_reference_client_time_to_consistency(
    scenario: str,
    profile_name: str,
    db_engine: AsyncEngine,
    proxy: ImpairedNetworkProxy,
    benchmark_results: list[ScenarioResult],
) -> None:
    db = Database(engine=db_engine)
    focus = await _new_conversation(db)
    other = await _new_conversation(db)

    async with _running_client(proxy, focus=focus) as client:
        assert await client.wait_until(
            lambda store: focus in store.messages and other in store.conversations,
            _DEADLINE_S,
        ), "the client never bootstrapped"
        proxy.set_impairment(PROFILES[profile_name])
        scenarios = _Scenarios(db, proxy, client, focus, other)
        run = scenarios.runner(scenario)
        result = ScenarioResult(
            client="reference",
            scenario=scenario,
            profile=profile_name,
            deadline_s=_DEADLINE_S,
        )
        before_runs = replace(proxy.stats)
        for _ in range(runs_per_scenario()):
            result.add(await run(f"reply {uuid.uuid4().hex[:10]}"))
        result.record_traffic(proxy.stats, since=before_runs)
    benchmark_results.append(result)

    if not _FULL:
        assert result.timeouts == 0, f"{scenario}: the client never caught up"
        p50 = result.percentile(0.5)
        assert p50 is not None
        assert p50 < _REGRESSION_BOUND_S, f"{scenario}: {p50:.1f}s to catch up"


async def test_reference_client_reaches_the_state_a_fresh_client_bootstraps_to(
    db_engine: AsyncEngine, proxy: ImpairedNetworkProxy
) -> None:
    """Through repeated resets, the long-polling client ends where a new one starts."""
    db = Database(engine=db_engine)
    focus = await _new_conversation(db)
    proxy.set_impairment(PROFILES["lossy_cellular"])

    async with _running_client(proxy, focus=focus) as client:
        for index in range(10):
            await _commit_reply(db, focus, f"reply {index}")
            if index % 3 == 0:
                proxy.reset_all_connections()
        assert await client.wait_until(_shows_reply(focus, "reply 9"), _DEADLINE_S), (
            "the client never caught up"
        )
        async with _running_client(proxy, focus=focus) as fresh:
            assert await fresh.wait_until(_shows_reply(focus, "reply 9"), _DEADLINE_S)
            assert client.store.messages == fresh.store.messages
            assert client.store.conversations == fresh.store.conversations


async def test_the_stream_sends_a_sync_frame_when_a_change_commits(
    db_engine: AsyncEngine, live_server_port: int
) -> None:
    db = Database(engine=db_engine)
    focus = await _new_conversation(db)
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    async with (
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{live_server_port}", timeout=timeout
        ) as http,
        http.stream(
            "GET", "/api/v1/sync/stream", params={"focus": focus, "fresh": focus}
        ) as stream,
    ):
        frames: list[SyncResponse] = []
        committed = False
        event_type = ""
        async for line in stream.aiter_lines():
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event_type == "sync":
                frames.append(SyncResponse.model_validate_json(line[5:]))
                if not committed:
                    await _commit_reply(db, focus, "streamed reply")
                    committed = True
                elif any(
                    message.content == "streamed reply"
                    for change in frames[-1].messages
                    for message in change.messages
                ):
                    break

    assert frames[0].bootstrap is True
    assert frames[-1].bootstrap is False
