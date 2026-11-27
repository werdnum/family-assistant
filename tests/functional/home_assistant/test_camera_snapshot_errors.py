"""How get_camera_snapshot reports a camera that times out or answers with an error."""

import asyncio
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.plugins.home_assistant.client import HomeAssistantClientWrapper
from family_assistant.plugins.home_assistant.config import HomeAssistantConfig
from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.plugins.home_assistant.tools import get_camera_snapshot_tool
from family_assistant.plugins.runtime import ProfilePlugins
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.home_assistant_client import HA_TEST_TOKEN, make_home_assistant_client

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@asynccontextmanager
async def home_assistant_camera_proxy(
    handler: Handler,
) -> AsyncGenerator[str]:
    """A local server answering ``/api/camera_proxy/<entity_id>`` with ``handler``."""
    app = web.Application()
    app.router.add_get("/api/camera_proxy/{entity_id}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("/")).rstrip("/")
    finally:
        await server.close()


def wrapper_for(api_url: str, timeout_seconds: float) -> HomeAssistantClientWrapper:
    return make_home_assistant_client(
        api_url=api_url,
        token=HA_TEST_TOKEN,
        camera_snapshot_timeout_seconds=timeout_seconds,
    ).wrapper


def exec_context(
    db_engine: AsyncEngine, wrapper: HomeAssistantClientWrapper
) -> ToolExecutionContext:
    return ToolExecutionContext(
        conversation_id="test_conversation",
        interface_type="test",
        turn_id="turn_camera",
        user_name="test_user",
        db_context=Database(engine=db_engine),
        processing_service=None,
        clock=None,
        plugins=ProfilePlugins((HomeAssistantInstance(wrapper),)),
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_snapshot_timeout_names_camera_elapsed_and_limit(
    db_engine: AsyncEngine,
) -> None:
    """A camera that never answers yields an error naming it and the time limit."""
    released = asyncio.Event()

    async def never_answers(request: web.Request) -> web.Response:
        await released.wait()
        return web.Response(body=b"")

    async with home_assistant_camera_proxy(never_answers) as api_url:
        wrapper = wrapper_for(api_url, timeout_seconds=0.2)
        try:
            result = await get_camera_snapshot_tool(
                exec_context(db_engine, wrapper),
                camera_entity_id="camera.garage_camera",
            )
        finally:
            released.set()

    assert result.text is not None
    assert re.fullmatch(
        r"Error: Failed to retrieve camera snapshot: Camera camera\.garage_camera "
        r"did not return a snapshot within 0\.2s \(gave up after \d+\.\ds\)",
        result.text,
    )
    assert not result.attachments


@pytest.mark.asyncio
async def test_snapshot_http_error_keeps_status_and_body_excerpt(
    db_engine: AsyncEngine,
) -> None:
    """A non-2xx answer reports the status, the reason and the start of the body."""
    body = "upstream go2rtc stream failed to start\n" + "x" * 1000

    async def bad_gateway(request: web.Request) -> web.Response:
        return web.Response(status=502, text=body)

    async with home_assistant_camera_proxy(bad_gateway) as api_url:
        wrapper = wrapper_for(api_url, timeout_seconds=5)
        result = await get_camera_snapshot_tool(
            exec_context(db_engine, wrapper),
            camera_entity_id="camera.front_door_doorbell",
        )

    excerpt = ("upstream go2rtc stream failed to start " + "x" * 1000)[:200] + "…"
    assert result.text == (
        "Error: Failed to retrieve camera snapshot: Camera camera.front_door_doorbell "
        f"snapshot request failed with HTTP 502 Bad Gateway: {excerpt}"
    )


def test_snapshot_timeout_default_outlasts_camera_gateway() -> None:
    """The default limit is longer than the camera gateway's 35s upstream timeout."""
    assert HomeAssistantConfig().camera_snapshot_timeout_seconds > 35
