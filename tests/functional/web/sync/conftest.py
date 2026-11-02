"""Fixtures for the sync time-to-consistency benchmark.

The browser reaches the server only through an ``ImpairedNetworkProxy``; the
benchmark's own driver talks to the server directly, so changes are committed
at a known instant over a perfect link and only the client under test sees the
bad network.
"""

import socket
import time
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from family_assistant.assistant import Assistant
from family_assistant.llm import LLMOutput
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from tests.functional.web.sync.benchmark import (
    ScenarioResult,
    full_run_requested,
    render_markdown,
    write_report,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)
from tests.netem.proxy import ImpairedNetworkProxy

BENCH_PREFIX = "bench"


def reply_marker(token: str) -> str:
    """Text that appears only once the assistant's reply is visible.

    The prompt carries the bare token, so waiting for the token alone would
    succeed as soon as the user's own message showed up.
    """
    return f"{token}-done"


def _bench_reply(args: MatcherArgs) -> LLMOutput:
    token = get_last_message_text(args["messages"]).split()[-1]
    return LLMOutput(content=f"Reply {reply_marker(token)}")


@pytest.fixture
def mock_llm_client() -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(
        rules=[
            (
                lambda args: get_last_message_text(args["messages"]).startswith(
                    BENCH_PREFIX
                ),
                _bench_reply,
            )
        ],
        default_response=LLMOutput(content="Unexpected prompt"),
    )


class TurnDriver:
    """Commits turns over a perfect link and reports when each one ended."""

    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=60.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete_turn(self, conversation_id: str, token: str) -> float:
        """Run a turn to completion; return ``time.monotonic()`` at ``turn_ended``.

        The reply is persisted before ``turn_ended`` is published, so this is
        the earliest moment any client could show it.
        """
        response = await self._client.post(
            "/api/v1/chat/turns",
            json={
                "turn_id": str(uuid.uuid4()),
                "conversation_id": conversation_id,
                "prompt": f"{BENCH_PREFIX} {token}",
                "interface_type": "web",
            },
        )
        response.raise_for_status()
        first_seq = response.json()["first_seq"]
        async with self._client.stream(
            "GET",
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": first_seq, "follow": "false"},
        ) as stream:
            async for line in stream.aiter_lines():
                if line.strip() == "event: turn_ended":
                    return time.monotonic()
        raise AssertionError(f"stream for {conversation_id} closed before turn_ended")


@pytest_asyncio.fixture
async def turn_driver(
    web_only_assistant: Assistant,
    api_socket_and_port: tuple[int, socket.socket],
) -> AsyncGenerator[TurnDriver]:
    port, _ = api_socket_and_port
    driver = TurnDriver(f"http://127.0.0.1:{port}")
    yield driver
    await driver.aclose()


@pytest_asyncio.fixture
async def impaired_proxy(
    web_only_assistant: Assistant,
    api_socket_and_port: tuple[int, socket.socket],
) -> AsyncGenerator[ImpairedNetworkProxy]:
    port, _ = api_socket_and_port
    async with ImpairedNetworkProxy("127.0.0.1", port, seed=port) as proxy:
        yield proxy


@pytest.fixture
def stream_hub(web_only_assistant: Assistant) -> ConversationStreamHub:
    assert web_only_assistant.fastapi_app is not None
    return web_only_assistant.fastapi_app.state.conversation_stream_hub


@pytest.fixture(scope="session")
def benchmark_results(
    pytestconfig: pytest.Config,
) -> Iterator[list[ScenarioResult]]:
    results: list[ScenarioResult] = []
    yield results
    if not results or not full_run_requested():
        return
    path = write_report(results, Path(pytestconfig.rootpath) / "test-results")
    print(f"\n=== Sync benchmark results ({path}) ===\n{render_markdown(results)}")
