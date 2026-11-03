"""The MCP event source against a fake Events-extension server."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from family_assistant.config_models import MCPEventSourceConfig, MCPServerConfig
from family_assistant.events.mcp_client import (
    EVENTS_EXTENSION,
    PROTOCOL_VERSION,
    MCPEventsClient,
    MCPEventsError,
)
from family_assistant.events.mcp_source import MCPEventSource
from family_assistant.events.processor import EventProcessor
from family_assistant.storage.database import Database
from family_assistant.storage.events import EventActionType, EventSourceType
from family_assistant.storage.tasks import tasks_table
from family_assistant.tools.automations import (
    create_automation_tool,
    update_automation_tool,
)
from family_assistant.tools.mcp_events import list_mcp_events_tool
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.storage.types import MatchConditions

ANDREW = "andrew@example.com"
TEIJA = "teija@example.com"
TOKENS = {"token-andrew": "andrew", "token-teija": "teija"}

# ast-grep-ignore: no-dict-any - the fake server speaks raw protocol JSON
EVENT_TYPES: list[dict[str, Any]] = [
    {
        "name": "inbox.changed",
        "description": "Something changed in a person's inbox",
        "delivery": ["poll", "webhook"],
        "inputSchema": {
            "type": "object",
            "properties": {"actor": {"type": "string"}},
            "additionalProperties": False,
        },
        "payloadSchema": {"type": "object"},
    },
    {
        "name": "push.only",
        "description": "Only offered as a webhook",
        "delivery": ["webhook"],
        "inputSchema": {"type": "object"},
    },
]


@dataclass
class FakeEventsServer:
    """A stateless 2026-07-28 server with one ordered feed per principal."""

    # ast-grep-ignore: no-dict-any - the fake server speaks raw protocol JSON
    feeds: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # ast-grep-ignore: no-dict-any - the fake server speaks raw protocol JSON
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, principal: str, category: str, title: str) -> None:
        self.feeds.setdefault(principal, []).append({
            "category": category,
            "task": {"title": title},
        })

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["MCP-Protocol-Version"] == PROTOCOL_VERSION
        assert request.headers["Mcp-Method"] == body["method"]
        meta = body["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == PROTOCOL_VERSION
        assert isinstance(meta["io.modelcontextprotocol/clientCapabilities"], dict)
        principal = TOKENS.get(
            request.headers.get("Authorization", "").removeprefix("Bearer ")
        )
        if principal is None:
            return httpx.Response(401, json={"error": "unauthorized"})
        params = body.get("params") or {}
        self.requests.append((body["method"], params))
        if body["method"] == "server/discover":
            # ast-grep-ignore: no-dict-any - the fake server speaks raw protocol JSON
            result: dict[str, Any] = {
                "capabilities": {"extensions": {EVENTS_EXTENSION: {}}}
            }
        elif body["method"] == "events/list":
            result = {"events": EVENT_TYPES}
        elif body["method"] == "events/poll":
            feed = self.feeds.get(principal, [])
            if params.get("cursor") is None:
                result = {"events": [], "cursor": str(len(feed)), "hasMore": False}
            else:
                start = int(params["cursor"])
                result = {
                    "events": [
                        {
                            "eventId": f"evt_{principal}_{index}",
                            "name": params["name"],
                            "timestamp": "2026-10-21T09:00:00Z",
                            "data": item,
                            "cursor": str(index + 1),
                        }
                        for index, item in enumerate(feed[start:], start=start)
                    ],
                    "cursor": str(len(feed)),
                    "hasMore": False,
                    "nextPollMs": 5000,
                }
        else:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )


@pytest.fixture
def fake_server() -> FakeEventsServer:
    return FakeEventsServer()


@pytest.fixture
def http_client(fake_server: FakeEventsServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake_server.handle))


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> dict[str, MCPServerConfig]:
    monkeypatch.setenv("FAKE_TOKEN_ANDREW", "token-andrew")
    monkeypatch.setenv("FAKE_TOKEN_TEIJA", "token-teija")
    return {
        "tasks": MCPServerConfig.model_validate({
            "transport": "streamable_http",
            "url": "https://tasks.example/mcp",
            "user_auth": {
                "users": {
                    ANDREW: {
                        "token_env": "FAKE_TOKEN_ANDREW",
                        "expected_user_id": "andrew",
                    },
                    TEIJA: {
                        "token_env": "FAKE_TOKEN_TEIJA",
                        "expected_user_id": "teija",
                    },
                }
            },
        }),
        "local": MCPServerConfig.model_validate({"command": "true"}),
    }


def make_source(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
) -> MCPEventSource:
    return MCPEventSource(
        servers=servers,
        database=lambda: Database(db_engine),
        config=MCPEventSourceConfig(),
        http_client=http_client,
    )


async def make_processor(
    db_engine: AsyncEngine, *, trusted_event_server_ids: tuple[str, ...] = ()
) -> EventProcessor:
    processor = EventProcessor(
        sources={},
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
        trusted_event_server_ids=trusted_event_server_ids,
    )
    await processor.start()
    return processor


async def add_listener(
    db: Database,
    name: str,
    user_id: str,
    extra: dict[str, str] | None = None,
    *,
    one_time: bool = False,
    processing_profile_id: str | None = None,
) -> int:
    return await db.events.create_event_listener(
        name=name,
        source_id=EventSourceType.mcp,
        match_conditions={
            "server_id": "tasks",
            "event": "inbox.changed",
            "arguments.actor": "me",
            **(extra or {}),
        },
        conversation_id=f"conv-{name}",
        interface_type="web",
        action_type=EventActionType.wake_llm,
        action_config={"context": "Something landed in the inbox"},
        created_by_user_id=user_id,
        one_time=one_time,
        processing_profile_id=processing_profile_id,
    )


async def executions(db: Database, listener_id: int) -> int:
    listener = await db.events.get_event_listener_by_id(listener_id)
    assert listener is not None
    return listener["daily_executions"] or 0


async def poll_all(source: MCPEventSource) -> None:
    for running in list(source._running.values()):
        await source.poll_once(running)


@pytest.mark.asyncio
async def test_poll_delivers_new_events_once_across_restarts(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
    fake_server: FakeEventsServer,
) -> None:
    db = Database(db_engine)
    fake_server.publish("andrew", "handoff", "Before the automation existed")
    listener_id = await add_listener(db, "inbox", ANDREW)
    processor = await make_processor(db_engine)

    source = make_source(db_engine, servers, http_client)
    source.processor = processor
    await source.reconcile(start_polling=False)
    await poll_all(source)
    assert await executions(db, listener_id) == 0

    fake_server.publish("andrew", "handoff", "Sort out the gutters")
    await poll_all(source)
    assert await executions(db, listener_id) == 1
    polls = [
        params for method, params in fake_server.requests if method == "events/poll"
    ]
    assert polls[-1]["arguments"] == {"actor": "me"}

    # A restarted source resumes from the stored cursor instead of "now", so
    # nothing published while it was down is lost and nothing is replayed.
    fake_server.publish("andrew", "handoff", "Published during the restart")
    restarted = make_source(db_engine, servers, http_client)
    restarted.processor = processor
    await restarted.reconcile(start_polling=False)
    await poll_all(restarted)
    await poll_all(restarted)
    assert await executions(db, listener_id) == 2


@pytest.mark.parametrize(
    ("trusted_servers", "expected_profile", "expected_trusted"),
    [((), "event_handler", None), (("tasks",), "default_assistant", True)],
)
@pytest.mark.asyncio
async def test_a_trusted_events_server_wakes_as_the_creating_profile(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
    fake_server: FakeEventsServer,
    trusted_servers: tuple[str, ...],
    expected_profile: str,
    expected_trusted: bool | None,
) -> None:
    db = Database(db_engine)
    await add_listener(db, "inbox", ANDREW, processing_profile_id="default_assistant")
    processor = await make_processor(
        db_engine, trusted_event_server_ids=trusted_servers
    )
    source = make_source(db_engine, servers, http_client)
    source.processor = processor
    await source.reconcile(start_polling=False)
    await poll_all(source)

    fake_server.publish("andrew", "handoff", "Sort out the gutters")
    await poll_all(source)

    [task] = await db.fetch_all(
        select(tasks_table.c.payload).where(tasks_table.c.task_type == "llm_callback")
    )
    payload = task["payload"]
    assert payload["processing_profile_id"] == expected_profile
    assert payload.get("tool_call_review_trigger_payload_trusted") == expected_trusted
    assert payload["tool_call_review_trigger_payload_present"] is True


@pytest.mark.asyncio
async def test_events_reach_only_the_creators_listeners(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
    fake_server: FakeEventsServer,
) -> None:
    db = Database(db_engine)
    andrew_listener = await add_listener(db, "andrew-inbox", ANDREW)
    teija_listener = await add_listener(db, "teija-inbox", TEIJA)
    processor = await make_processor(db_engine)
    source = make_source(db_engine, servers, http_client)
    source.processor = processor
    await source.reconcile(start_polling=False)
    assert len(source._running) == 2
    await poll_all(source)

    fake_server.publish("andrew", "handoff", "Andrew's private task")
    await poll_all(source)

    assert await executions(db, andrew_listener) == 1
    assert await executions(db, teija_listener) == 0


@pytest.mark.asyncio
async def test_other_conditions_filter_delivered_events(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
    fake_server: FakeEventsServer,
) -> None:
    db = Database(db_engine)
    handoffs = await add_listener(
        db, "handoffs", ANDREW, extra={"data.category": "handoff"}
    )
    everything = await add_listener(db, "everything", ANDREW)
    processor = await make_processor(db_engine)
    source = make_source(db_engine, servers, http_client)
    source.processor = processor
    await source.reconcile(start_polling=False)
    # Both listeners watch the same thing as the same person: one subscription.
    assert len(source._running) == 1
    await poll_all(source)

    fake_server.publish("andrew", "deadline", "Renew the rego")
    fake_server.publish("andrew", "handoff", "Book the plumber")
    await poll_all(source)

    assert await executions(db, handoffs) == 1
    assert await executions(db, everything) == 2


@pytest.mark.asyncio
async def test_disabling_the_automation_stops_its_subscription(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
) -> None:
    db = Database(db_engine)
    listener_id = await add_listener(db, "inbox", ANDREW)
    source = make_source(db_engine, servers, http_client)
    await source.reconcile(start_polling=False)
    assert len(source._running) == 1

    listener = await db.events.get_event_listener_by_id(listener_id)
    assert listener is not None
    await db.events.update_event_listener_enabled(
        listener_id, listener["conversation_id"], enabled=False
    )
    await source.reconcile(start_polling=False)
    assert source._running == {}


@pytest.mark.asyncio
async def test_validate_listener_checks_the_live_server(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
) -> None:
    source = make_source(db_engine, servers, http_client)
    base: MatchConditions = {"server_id": "tasks", "event": "inbox.changed"}

    assert (
        await source.validate_listener({**base, "arguments.actor": "me"}, ANDREW)
    ).valid

    unknown = await source.validate_listener({**base, "event": "nope"}, ANDREW)
    assert not unknown.valid
    assert unknown.errors[0].similar_values == ["inbox.changed", "push.only"]

    webhook_only = await source.validate_listener(
        {**base, "event": "push.only"}, ANDREW
    )
    assert not webhook_only.valid
    assert "polling" in webhook_only.errors[0].error

    bad_argument = await source.validate_listener(
        {**base, "arguments.colour": "red"}, ANDREW
    )
    assert not bad_argument.valid

    no_credential = await source.validate_listener(base, "someone@example.com")
    assert not no_credential.valid
    assert "credential" in no_credential.errors[0].error

    stdio = await source.validate_listener({**base, "server_id": "local"}, ANDREW)
    assert not stdio.valid

    structured = await source.validate_listener(
        {**base, "arguments.actor": ["me"]},  # pyright: ignore[reportArgumentType]
        ANDREW,
    )
    assert not structured.valid
    assert "string, number or boolean" in structured.errors[0].error

    assert source.event_servers(ANDREW) == ["tasks"]


@pytest.mark.asyncio
async def test_client_reads_a_reply_sent_as_server_sent_events() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        reply = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"events": [{"name": "a.b", "delivery": ["poll"]}]},
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"event: message\ndata: {json.dumps(reply)}\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MCPEventsClient(
            url="https://x.example/mcp", token=None, http_client=http
        )
        [event_type] = await client.list_events()
    assert event_type.name == "a.b"
    assert event_type.supports_poll


def tool_context(
    db: Database, source: MCPEventSource, user_id: str
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="telegram",
        conversation_id="chat-1",
        user_name="Andrew",
        user_id=user_id,
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        processing_profile_id="default_assistant",
        clock=None,
        plugins=None,
        event_sources={EventSourceType.mcp.value: source},
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_agent_creates_an_mcp_automation_from_discovered_events(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
) -> None:
    db = Database(db_engine)
    source = make_source(db_engine, servers, http_client)
    context = tool_context(db, source, ANDREW)

    listed = (await list_mcp_events_tool(context)).get_data()
    assert isinstance(listed, dict)
    [server] = listed["servers"]
    assert server["server_id"] == "tasks"
    assert {event["name"]: event["pollable"] for event in server["events"]} == {
        "inbox.changed": True,
        "push.only": False,
    }

    refused = await create_automation_tool(
        exec_context=context,
        name="Webhook only",
        automation_type="event",
        trigger_config={
            "event_source": "mcp",
            "event_filter": {"server_id": "tasks", "event": "push.only"},
        },
        action_type="wake_llm",
        action_config={"context": "Tell me"},
    )
    assert "polling" in refused.get_text()

    created = await create_automation_tool(
        exec_context=context,
        name="Inbox",
        automation_type="event",
        trigger_config={
            "event_source": "mcp",
            "event_filter": {
                "server_id": "tasks",
                "event": "inbox.changed",
                "arguments.actor": "me",
            },
        },
        action_type="wake_llm",
        action_config={"context": "Tell me what landed in my inbox"},
    )
    assert "Created event automation" in created.get_text(), created.get_text()

    await source.reconcile(start_polling=False)
    assert len(source._running) == 1
    running = next(iter(source._running.values()))
    assert running.subscription.user_id == ANDREW
    assert running.subscription.argument_dict == {"actor": "me"}


@pytest.mark.asyncio
async def test_a_one_time_automation_fires_once_per_page(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
    fake_server: FakeEventsServer,
) -> None:
    db = Database(db_engine)
    listener_id = await add_listener(db, "once", ANDREW, one_time=True)
    processor = await make_processor(db_engine)
    source = make_source(db_engine, servers, http_client)
    source.processor = processor
    await source.reconcile(start_polling=False)
    await poll_all(source)

    fake_server.publish("andrew", "handoff", "First")
    fake_server.publish("andrew", "handoff", "Second")
    await poll_all(source)

    assert await executions(db, listener_id) == 1


@pytest.mark.asyncio
async def test_a_re_enabled_automation_starts_from_now(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
    fake_server: FakeEventsServer,
) -> None:
    db = Database(db_engine)
    listener_id = await add_listener(db, "inbox", ANDREW)
    listener = await db.events.get_event_listener_by_id(listener_id)
    assert listener is not None
    processor = await make_processor(db_engine)
    source = make_source(db_engine, servers, http_client)
    source.processor = processor
    await source.reconcile(start_polling=False)
    await poll_all(source)

    await db.events.update_event_listener_enabled(
        listener_id, listener["conversation_id"], enabled=False
    )
    await source.reconcile(start_polling=False)
    fake_server.publish("andrew", "handoff", "While it was off")
    await db.events.update_event_listener_enabled(
        listener_id, listener["conversation_id"], enabled=True
    )
    await source.reconcile(start_polling=False)
    await poll_all(source)
    await poll_all(source)
    assert await executions(db, listener_id) == 0

    fake_server.publish("andrew", "handoff", "After it came back")
    await poll_all(source)
    assert await executions(db, listener_id) == 1


@pytest.mark.asyncio
async def test_discovery_reports_an_unreachable_server_instead_of_hiding_it(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as http:
        source = make_source(db_engine, servers, http)
        listed = (
            await list_mcp_events_tool(
                tool_context(Database(db_engine), source, ANDREW)
            )
        ).get_data()
    assert isinstance(listed, dict)
    [server] = listed["servers"]
    assert server["server_id"] == "tasks"
    assert "connection refused" in server["error"]


@pytest.mark.asyncio
async def test_an_edited_trigger_is_matched_with_its_new_conditions(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
    fake_server: FakeEventsServer,
) -> None:
    db = Database(db_engine)
    listener_id = await add_listener(db, "inbox", ANDREW)
    listener = await db.events.get_event_listener_by_id(listener_id)
    assert listener is not None
    processor = await make_processor(db_engine)
    source = make_source(db_engine, servers, http_client)
    source.processor = processor
    await source.reconcile(start_polling=False)
    await poll_all(source)

    await db.events.update_event_listener(
        listener_id=listener_id,
        conversation_id=listener["conversation_id"],
        name=listener["name"],
        description=None,
        match_conditions={
            "server_id": "tasks",
            "event": "inbox.changed",
            "arguments.actor": "teija",
        },
        action_config=listener["action_config"],
        one_time=False,
        enabled=True,
        created_by_user_id=ANDREW,
    )
    await source.reconcile(start_polling=False)
    await poll_all(source)
    fake_server.publish("andrew", "handoff", "Seen through the edited watch")
    await poll_all(source)

    assert await executions(db, listener_id) == 1


@pytest.mark.asyncio
async def test_an_action_edit_rechecks_the_trigger_for_the_new_poller(
    db_engine: AsyncEngine,
    servers: dict[str, MCPServerConfig],
    http_client: httpx.AsyncClient,
) -> None:
    db = Database(db_engine)
    listener_id = await add_listener(db, "inbox", ANDREW)
    source = make_source(db_engine, servers, http_client)

    refused = await update_automation_tool(
        exec_context=tool_context(db, source, "guest@example.com"),
        automation_id=listener_id,
        automation_type="event",
        action_config={"context": "Tell the guest"},
    )
    assert "no tasks credential" in refused.get_text(), refused.get_text()
    listener = await db.events.get_event_listener_by_id(listener_id)
    assert listener is not None
    assert listener["created_by_user_id"] == ANDREW

    updated = await update_automation_tool(
        exec_context=tool_context(db, source, TEIJA),
        automation_id=listener_id,
        automation_type="event",
        action_config={"context": "Tell Teija"},
    )
    assert "Error" not in updated.get_text(), updated.get_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        pytest.param("not an event", id="not-an-object"),
        pytest.param(
            {"eventId": "evt_1", "name": "a.b", "timestamp": "2026-10-21T09:00:00Z"},
            id="no-data",
        ),
        pytest.param(
            {"name": "a.b", "timestamp": "2026-10-21T09:00:00Z", "data": {}},
            id="no-event-id",
        ),
        pytest.param(
            {"eventId": "evt_1", "name": "a.b", "data": {}}, id="no-timestamp"
        ),
    ],
)
async def test_a_malformed_page_is_not_skipped(event: object) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = {"events": [event], "cursor": "9"}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MCPEventsClient(
            url="https://x.example/mcp", token=None, http_client=http
        )
        with pytest.raises(MCPEventsError, match="malformed"):
            await client.poll("a.b", {}, "1")


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [9, {"at": 9}])
async def test_a_page_with_a_malformed_cursor_is_not_accepted(cursor: object) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = {"events": [], "cursor": cursor}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MCPEventsClient(
            url="https://x.example/mcp", token=None, http_client=http
        )
        with pytest.raises(MCPEventsError, match="malformed cursor"):
            await client.poll("a.b", {}, "1")


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [None, {"name": "a.b"}])
async def test_an_events_list_without_a_list_is_not_read_as_empty(
    events: object,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"events": events}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MCPEventsClient(
            url="https://x.example/mcp", token=None, http_client=http
        )
        with pytest.raises(MCPEventsError, match="no list of events"):
            await client.list_events()


@pytest.mark.asyncio
async def test_a_server_without_replay_may_return_a_null_cursor() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        event = {
            "eventId": "evt_1",
            "name": "a.b",
            "timestamp": "2026-10-21T09:00:00Z",
            "data": {},
        }
        result = {"events": [event], "cursor": None}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MCPEventsClient(
            url="https://x.example/mcp", token=None, http_client=http
        )
        page = await client.poll("a.b", {}, None)
    assert [event.event_id for event in page.events] == ["evt_1"]
    assert page.cursor is None
