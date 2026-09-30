import asyncio
import json
import logging
import os
import signal
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from family_assistant.tools.mcp import MCPToolsProvider
from family_assistant.tools.types import MCPServerConfig, ToolExecutionContext
from tests.helpers import find_free_port, require_executable, wait_for_server

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

# --- Controller ---


class MCPProxyController:
    def __init__(self, port: int) -> None:
        self.port = port
        self.process: asyncio.subprocess.Process | None = None
        self.host = "127.0.0.1"
        self.sse_url = f"http://{self.host}:{self.port}/sse"

    async def start(self) -> None:
        if self.process:
            return

        mcp_proxy_command = require_executable("mcp-proxy")
        mcp_server_time_command = require_executable("mcp-server-time")
        command = [
            mcp_proxy_command,
            "--port",
            str(self.port),
            "--host",
            self.host,
            mcp_server_time_command,
        ]
        logger.info(f"Starting MCP proxy server: {' '.join(command)}")
        self.process = await asyncio.create_subprocess_exec(
            *command, start_new_session=True, stderr=asyncio.subprocess.PIPE
        )
        await wait_for_server(self.sse_url, timeout=30.0)

    async def stop(self) -> None:
        if not self.process:
            return

        logger.info("Stopping MCP proxy server...")
        if self.process.returncode is None:
            try:
                if self.process.pid:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                # Also send SIGINT to the main process
                self.process.send_signal(signal.SIGINT)
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except Exception as e:
                logger.warning(f"Error stopping proxy: {e}")
                if self.process.returncode is None:
                    try:
                        self.process.kill()
                        await self.process.wait()
                    except Exception:
                        pass
        self.process = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()


def assert_noon_new_york_converted_to_utc(result: str) -> None:
    try:
        conversion = json.loads(result)
    except json.JSONDecodeError:
        pytest.fail(f"convert_time did not return a conversion: {result!r}")
    source = conversion["source"]
    target = conversion["target"]
    assert source["timezone"] == "America/New_York", conversion
    assert target["timezone"] == "UTC", conversion
    source_time = datetime.fromisoformat(source["datetime"])
    target_time = datetime.fromisoformat(target["datetime"])
    assert source_time.time() == time(12, 0), conversion
    assert target_time.utcoffset() == timedelta(0), conversion
    assert target_time == source_time, conversion


@pytest_asyncio.fixture
async def mcp_proxy_controller() -> "AsyncGenerator[MCPProxyController]":
    port = find_free_port()
    controller = MCPProxyController(port)
    await controller.start()
    yield controller
    await controller.stop()


@pytest.mark.asyncio
async def test_mcp_sse_restart(mcp_proxy_controller: MCPProxyController) -> None:
    """
    Test that MCP client can handle server restart (SSE disconnect).
    """
    # 1. Initialize MCP Provider
    mcp_config: dict[str, MCPServerConfig] = {
        "time_sse": {
            "transport": "sse",
            "url": mcp_proxy_controller.sse_url,
        }
    }
    mcp_provider = MCPToolsProvider(mcp_server_configs=mcp_config)
    await mcp_provider.initialize()

    # 2. Execute tool successfully
    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="456",
        user_name="tester",
        turn_id="turn1",
        db_context=MagicMock(),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )
    args = {
        "time": "12:00",
        "source_timezone": "America/New_York",
        "target_timezone": "UTC",
    }

    logger.info("Executing tool before restart...")
    result1 = await mcp_provider.execute_tool("convert_time", args, context)
    assert_noon_new_york_converted_to_utc(result1)
    version_before_restart = mcp_provider.descriptors_version

    # 3. Restart Server
    logger.info("Restarting MCP Proxy Server...")
    await mcp_proxy_controller.restart()

    # 4. Execute tool again - should fail initially but reconnect
    logger.info("Executing tool after restart...")
    result2 = await mcp_provider.execute_tool("convert_time", args, context)

    # 5. Verify success
    assert_noon_new_york_converted_to_utc(result2)

    # 6. Reconnecting must advance the descriptors version so downstream caches
    #    (policy/on-demand) rebuild instead of serving a stale tool list.
    assert mcp_provider.descriptors_version > version_before_restart

    await mcp_provider.close()
