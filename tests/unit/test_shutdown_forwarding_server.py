"""The uvicorn server's exit signal starts the app's shutdown at once.

Without forwarding, uvicorn holds the signal until it has drained every
connection, while the chat streams holding connections open wait for the app's
shutdown -- so turns are killed instead of suspended.
"""

import signal

import uvicorn
from fastapi import FastAPI

from family_assistant.assistant import ShutdownForwardingServer
from tests.helpers import wait_for_condition


async def test_exit_signal_is_forwarded_to_the_app_shutdown() -> None:
    received: list[str] = []
    server = ShutdownForwardingServer(
        uvicorn.Config(FastAPI()), on_exit_signal=received.append
    )

    server.handle_exit(signal.SIGTERM, None)

    await wait_for_condition(lambda: received, description="signal forwarded")
    assert received == ["SIGTERM"]
    assert server.should_exit
