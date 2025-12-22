import asyncio
import logging
import os
import signal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from family_assistant.tools.mcp import MCPToolsProvider
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import find_free_port, wait_for_server

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

        command = [
            "mcp-proxy",
            "--port",
            str(self.port),
            "--host",
            self.host,
            "mcp-server-time",
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
        # Wait a bit to ensure port is freed
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Simulating wait for port release
        await asyncio.sleep(1)
        await self.start()


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
    mcp_config = {
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
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
    )
    args = {
        "time": "12:00",
        "source_timezone": "America/New_York",
        "target_timezone": "UTC",
    }

    logger.info("Executing tool before restart...")
    result1 = await mcp_provider.execute_tool("convert_time", args, context)
    assert "Error" not in result1

    # 3. Restart Server
    logger.info("Restarting MCP Proxy Server...")
    await mcp_proxy_controller.restart()

    # 4. Execute tool again - should fail initially but reconnect
    logger.info("Executing tool after restart...")
    result2 = await mcp_provider.execute_tool("convert_time", args, context)

    # 5. Verify success
    assert "Error" not in result2

    await mcp_provider.close()
