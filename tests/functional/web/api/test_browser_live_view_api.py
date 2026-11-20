"""Tests for live browser view capture, streaming, and authenticated frame APIs."""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import UserMessage
from family_assistant.services.browser_live_view import BrowserLiveViewStore
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate
from family_assistant.tools import browser_backend as browser_backend_module
from family_assistant.tools import browser_session as browser_session_module
from family_assistant.tools.browser_session import browser_operation
from family_assistant.tools.types import (
    ToolAttachment,
    ToolCallBatch,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.utils.clock import SystemClock
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from tests.helpers import wait_for_condition

OWNER = "test_user"
PNG_BYTES_A = b"\x89PNG\r\n\x1a\nFRAME_A"
PNG_BYTES_B = b"\x89PNG\r\n\x1a\nFRAME_B"


@pytest_asyncio.fixture(scope="function")
async def test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _start_conversation(db: Database, *, user_id: str = OWNER) -> str:
    conversation_id = str(uuid.uuid4())
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Open example.com"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC) - timedelta(minutes=5),
        user_id=user_id,
    )
    return conversation_id


async def _create_delegation(
    db: Database,
    conversation_id: str,
    target_service_id: str = "browser_profile",
    *,
    source_profile_id: str = "default_assistant",
    parent_subconversation_id: str | None = None,
    user_id: str = OWNER,
) -> tuple[str, str]:
    delegation_id = f"del-{uuid.uuid4().hex[:8]}"
    subconversation_id = str(uuid.uuid4())
    await db.delegation_runs.create_run(
        DelegationRunCreate(
            delegation_id=delegation_id,
            task_id=f"task-{delegation_id}",
            source_profile_id=source_profile_id,
            target_service_id=target_service_id,
            interface_type="web",
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            source_subconversation_id=parent_subconversation_id,
            request_text="Browse the target site",
            content_parts_json=[],
            user_id=user_id,
        )
    )
    assert await db.delegation_runs.mark_handed_off(delegation_id, datetime.now(UTC))
    return delegation_id, subconversation_id


def _parse_sse_frames(body: str) -> list[tuple[str, dict[str, object]]]:
    frames: list[tuple[str, dict[str, object]]] = []
    for block in body.split("\n\n"):
        event_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            frames.append((event_name, json.loads("\n".join(data_lines))))
    return frames


class _FakeBrowserBackend:
    def __init__(
        self,
        *,
        backend_kind: Literal["local", "remote"] = "local",
        url: str = "https://example.com/start",
        screenshot_bytes: bytes = PNG_BYTES_A,
        fail_screenshot: bool = False,
    ) -> None:
        self.backend_kind: Literal["local", "remote"] = backend_kind
        self.current_url = url
        self.screen_width = 1280 if backend_kind == "remote" else 1024
        self.screen_height = 720 if backend_kind == "remote" else 768
        self._screenshot_bytes = screenshot_bytes
        self._fail_screenshot = fail_screenshot
        self.screenshot_calls = 0

    async def screenshot_png(self) -> bytes:
        self.screenshot_calls += 1
        if self._fail_screenshot:
            raise RuntimeError("lease held by human")
        return self._screenshot_bytes


def test_browser_live_view_store_lru_eviction() -> None:
    store = BrowserLiveViewStore(max_entries=2)
    f1 = store.set_frame(
        conversation_id="conv-1",
        subconversation_id=None,
        image_bytes=PNG_BYTES_A,
        mime_type="image/png",
        url="https://one.example",
        tool_name="browser_open",
        backend_kind="local",
        width=1024,
        height=768,
    )
    f2 = store.set_frame(
        conversation_id="conv-2",
        subconversation_id=None,
        image_bytes=PNG_BYTES_B,
        mime_type="image/png",
        url="https://two.example",
        tool_name="browser_click",
        backend_kind="remote",
        width=1280,
        height=720,
    )
    # Touch conv-1 so conv-2 becomes least recently used.
    assert store.get_frame("conv-1") == f1
    f3 = store.set_frame(
        conversation_id="conv-3",
        subconversation_id="sub-3",
        image_bytes=PNG_BYTES_A,
        mime_type="image/png",
        url="https://three.example",
        tool_name="click",
        backend_kind="remote",
        width=1280,
        height=720,
    )
    assert len({f1.frame_id, f2.frame_id, f3.frame_id}) == 3
    assert store.get_frame("conv-1") == f1
    assert store.get_frame("conv-2") is None
    assert store.get_frame("conv-3", "sub-3") == f3

    # A fresh store (simulating a server restart) emits distinct frame IDs.
    restarted_store = BrowserLiveViewStore(max_entries=2)
    f_after_restart = restarted_store.set_frame(
        conversation_id="conv-1",
        subconversation_id=None,
        image_bytes=PNG_BYTES_B,
        mime_type="image/png",
        url="https://one.example/restarted",
        tool_name="browser_open",
        backend_kind="local",
        width=1024,
        height=768,
    )
    assert f_after_restart.frame_id != f1.frame_id


@pytest.mark.asyncio
async def test_conversation_browser_view_endpoints_and_sse(
    test_client: AsyncClient,
    app_fixture: FastAPI,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conversation_id = await _start_conversation(db)

    empty = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/browser-view"
    )
    assert empty.status_code == 200
    assert empty.json() == {"available": False, "frame": None}

    missing_frame = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/browser-view/frame"
    )
    assert missing_frame.status_code == 404

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    turn = await hub.start_turn(
        conversation_id,
        turn_id="turn-1",
        user_id=OWNER,
        started_at=datetime.now(UTC),
    )

    frame_holder: list[str] = []

    async def _publish_and_finish() -> None:
        await wait_for_condition(
            lambda: hub.subscriber_count(conversation_id) > 0,
            timeout=5.0,
            description="conversation SSE subscriber attaches",
        )
        frame = await hub.publish_browser_frame(
            conversation_id=conversation_id,
            subconversation_id=None,
            turn_id="turn-1",
            image_bytes=PNG_BYTES_A,
            mime_type="image/png",
            url="https://news.ycombinator.com/",
            tool_name="browser_open",
            backend_kind="remote",
            width=1280,
            height=720,
        )
        frame_holder.append(frame.frame_id)
        await hub.end_turn(conversation_id, turn_id="turn-1", status="complete")

    producer = asyncio.create_task(_publish_and_finish())
    stream_res = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={
            "follow": "false",
            "from_seq": str(turn.first_seq),
            "event_types": "message,turn_started,turn_ended,browser_frame",
        },
    )
    await producer
    assert stream_res.status_code == 200
    assert len(frame_holder) == 1
    frame_id = frame_holder[0]

    sse_frames = _parse_sse_frames(stream_res.text)
    browser_events = [p for name, p in sse_frames if name == "browser_frame"]
    assert len(browser_events) == 1
    assert browser_events[0]["frame_id"] == frame_id
    assert browser_events[0]["url"] == "https://news.ycombinator.com/"
    assert browser_events[0]["backend_kind"] == "remote"
    assert browser_events[0]["tool_name"] == "browser_open"
    image_url = str(browser_events[0]["image_url"])

    meta = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/browser-view"
    )
    assert meta.status_code == 200
    assert meta.json()["available"] is True
    assert meta.json()["frame"]["frame_id"] == frame_id
    assert meta.json()["frame"]["image_url"] == image_url

    msgs = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert msgs.status_code == 200
    assert msgs.json()["browser_view"]["frame_id"] == frame_id

    image_res = await test_client.get(image_url)
    assert image_res.status_code == 200
    assert image_res.content == PNG_BYTES_A
    assert image_res.headers["content-type"].startswith("image/png")
    assert image_res.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_subconversation_browser_view_scoping_and_sse(
    test_client: AsyncClient,
    app_fixture: FastAPI,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conv_a = await _start_conversation(db, user_id=OWNER)
    conv_b = await _start_conversation(db, user_id=OWNER)
    other_user_conv = await _start_conversation(db, user_id="someone_else")
    _, sub_a = await _create_delegation(db, conv_a, "browser_profile", user_id=OWNER)
    _, other_sub = await _create_delegation(
        db, other_user_conv, "browser_profile", user_id="someone_else"
    )

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    frame = await hub.publish_browser_frame(
        conversation_id=conv_a,
        subconversation_id=sub_a,
        turn_id="sub-turn-1",
        image_bytes=PNG_BYTES_B,
        mime_type="image/png",
        url="https://example.org/pricing",
        tool_name="click",
        backend_kind="local",
        width=1024,
        height=768,
    )

    # Owner gets metadata in both /subconversations/.../messages and /browser-view.
    sub_msgs = await test_client.get(
        f"/api/v1/chat/conversations/{conv_a}/subconversations/{sub_a}/messages"
    )
    assert sub_msgs.status_code == 200
    assert sub_msgs.json()["browser_view"]["frame_id"] == frame.frame_id
    assert sub_msgs.json()["browser_view"]["backend_kind"] == "local"

    image_url = sub_msgs.json()["browser_view"]["image_url"]
    img = await test_client.get(image_url)
    assert img.status_code == 200
    assert img.content == PNG_BYTES_B
    assert img.headers["cache-control"] == "private, no-store"

    # Wrong conversation cannot access sub_a's frame.
    wrong_conv = await test_client.get(
        f"/api/v1/chat/conversations/{conv_b}/subconversations/{sub_a}/browser-view"
    )
    assert wrong_conv.status_code == 404
    wrong_frame = await test_client.get(
        f"/api/v1/chat/conversations/{conv_b}/subconversations/{sub_a}/browser-view/frame"
    )
    assert wrong_frame.status_code == 404

    # Another user's conversation is denied.
    forbidden = await test_client.get(
        f"/api/v1/chat/conversations/{other_user_conv}/subconversations/{other_sub}/browser-view"
    )
    assert forbidden.status_code in {403, 404}


@pytest.mark.asyncio
async def test_browser_operation_live_frame_capture_and_shared_tab(
    monkeypatch: pytest.MonkeyPatch,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    hub = ConversationStreamHub()
    fake_backend = _FakeBrowserBackend(
        backend_kind="remote",
        url="https://example.com/checkout",
        screenshot_bytes=PNG_BYTES_A,
    )

    async def _fake_existing_backend(
        exec_context: ToolExecutionContext,
    ) -> _FakeBrowserBackend:
        del exec_context
        return fake_backend

    monkeypatch.setattr(
        browser_backend_module,
        "get_existing_browser_backend",
        _fake_existing_backend,
    )

    @browser_operation("browser_test_dom")
    async def _dom_op(exec_context: ToolExecutionContext) -> ToolResult:
        del exec_context
        return ToolResult(
            data={"url": "https://example.com/checkout", "action": "clicked"}
        )

    @browser_operation("browser_test_visual")
    async def _visual_op(exec_context: ToolExecutionContext) -> ToolResult:
        del exec_context
        return ToolResult(
            data={"url": "https://example.com/visual"},
            attachments=[
                ToolAttachment(
                    content=PNG_BYTES_B,
                    mime_type="image/png",
                    description="Visual screenshot",
                )
            ],
        )

    @browser_operation("browser_request_handoff")
    async def _handoff_op(exec_context: ToolExecutionContext) -> ToolResult:
        del exec_context
        return ToolResult(data={"url": "https://example.com/login"})

    fake_service = SimpleNamespace(browser_live_view_publisher=hub)
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"
    sub_visual = f"sub-{uuid.uuid4().hex[:8]}"

    def _make_ctx(
        *,
        subconversation_id: str | None = None,
        batch: ToolCallBatch | None = None,
        call_id: str | None = None,
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            interface_type="web",
            conversation_id=conv_id,
            user_name="Alice",
            turn_id="turn-1",
            db_context=db,
            processing_service=fake_service,  # type: ignore[arg-type]
            clock=SystemClock(),
            event_sources={},
            plugins=None,  # type: ignore[arg-type]
            attachment_registry=None,
            credential_resolvers={},
            api_backend=None,
            timezone=UTC,  # type: ignore[arg-type]
            subconversation_id=subconversation_id,
            tool_call_batch=batch,
            tool_call_id=call_id,
        )

    try:
        # 1. DOM tool triggers screenshot capture on active backend without mutating ToolResult.
        res = await _dom_op(_make_ctx())
        assert res.attachments is None
        assert fake_backend.screenshot_calls == 1
        frame = hub.browser_live_view.get_frame(conv_id)
        assert frame is not None
        assert frame.image_bytes == PNG_BYTES_A
        assert frame.backend_kind == "remote"
        assert frame.tool_name == "browser_test_dom"

        # 2. Batch coalescing skips screenshot for non-final browser op in same batch.
        batch = ToolCallBatch([
            ("c1", "browser_test_dom"),
            ("c2", "browser_test_dom"),
        ])
        await _dom_op(_make_ctx(batch=batch, call_id="c1"))
        batch.mark_done("c1")
        assert fake_backend.screenshot_calls == 1
        await _dom_op(_make_ctx(batch=batch, call_id="c2"))
        batch.mark_done("c2")
        assert fake_backend.screenshot_calls == 2

        # 3. Handoff tool never attempts a screenshot.
        await _handoff_op(_make_ctx())
        assert fake_backend.screenshot_calls == 2

        # 4. Visual / computer-use tool reuses attachment bytes without extra screenshot_png(),
        #    and shared-tab child publishes to both child and parent conversation.
        browser_session_module._subconversation_scopes[sub_visual] = conv_id
        visual_res = await _visual_op(_make_ctx(subconversation_id=sub_visual))
        assert visual_res.attachments is not None
        assert fake_backend.screenshot_calls == 2
        child_frame = hub.browser_live_view.get_frame(conv_id, sub_visual)
        parent_frame = hub.browser_live_view.get_frame(conv_id, None)
        assert child_frame is not None and child_frame.image_bytes == PNG_BYTES_B
        assert parent_frame is not None and parent_frame.image_bytes == PNG_BYTES_B
        assert parent_frame.url == "https://example.com/visual"

        # 5. Screenshot failure never breaks the browser tool call.
        fake_backend._fail_screenshot = True
        safe_res = await _dom_op(_make_ctx())
        assert safe_res.get_data() == {
            "url": "https://example.com/checkout",
            "action": "clicked",
        }
        assert fake_backend.screenshot_calls == 3
    finally:
        browser_session_module._subconversation_scopes.pop(sub_visual, None)
        browser_session_module.BROWSER_TOOL_NAMES.discard("browser_test_dom")
        browser_session_module.BROWSER_TOOL_NAMES.discard("browser_test_visual")
        browser_session_module._sessions.pop(conv_id, None)
