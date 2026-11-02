"""The impairment proxy does to a connection what each impairment says it does.

The benchmark's numbers are only as good as the network it simulates, so each
impairment is checked against a real socket here.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, Awaitable, Callable

import pytest
import pytest_asyncio

from tests.netem.proxy import EventStreamHold, ImpairedNetworkProxy, Impairment

pytestmark = pytest.mark.no_db

ServerHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(ConnectionError):
        while data := await reader.read(4096):
            writer.write(data)
            await writer.drain()
    writer.close()


@contextlib.asynccontextmanager
async def _upstream(handler: ServerHandler) -> AsyncGenerator[int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def echo_proxy() -> AsyncGenerator[ImpairedNetworkProxy]:
    async with (
        _upstream(_echo) as port,
        ImpairedNetworkProxy("127.0.0.1", port, seed=1) as proxy,
    ):
        yield proxy


async def _connect(
    proxy: ImpairedNetworkProxy,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", proxy.port)


async def _round_trip(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, payload: bytes
) -> bytes:
    writer.write(payload)
    await writer.drain()
    return await asyncio.wait_for(reader.readexactly(len(payload)), timeout=10)


async def test_latency_applies_in_each_direction(
    echo_proxy: ImpairedNetworkProxy,
) -> None:
    echo_proxy.set_impairment(Impairment(latency_s=0.2))
    reader, writer = await _connect(echo_proxy)

    started = time.monotonic()
    await _round_trip(reader, writer, b"ping")

    assert time.monotonic() - started >= 0.4
    writer.close()


async def test_latency_is_paid_once_for_a_response_spanning_many_reads(
    echo_proxy: ImpairedNetworkProxy,
) -> None:
    echo_proxy.set_impairment(Impairment(latency_s=0.05))
    reader, writer = await _connect(echo_proxy)
    payload = b"x" * (256 * 1024)

    started = time.monotonic()
    await _round_trip(reader, writer, payload)

    # Sixteen 16 KiB reads each way; paying latency per read would take 1.6 s.
    assert time.monotonic() - started < 0.5
    writer.close()


async def test_jitter_never_reorders_bytes(echo_proxy: ImpairedNetworkProxy) -> None:
    echo_proxy.set_impairment(Impairment(latency_s=0.01, jitter_s=0.05))
    reader, writer = await _connect(echo_proxy)
    payload = b"".join(f"{i:04d}".encode() for i in range(200))

    for i in range(0, len(payload), 40):
        writer.write(payload[i : i + 40])
        await writer.drain()
    echoed = await asyncio.wait_for(reader.readexactly(len(payload)), timeout=10)

    assert echoed == payload
    writer.close()


async def test_blackhole_delays_delivery_without_resetting(
    echo_proxy: ImpairedNetworkProxy,
) -> None:
    reader, writer = await _connect(echo_proxy)
    await _round_trip(reader, writer, b"warm")

    echo_proxy.blackhole(0.5)
    started = time.monotonic()
    echoed = await _round_trip(reader, writer, b"after")

    assert echoed == b"after"
    assert time.monotonic() - started >= 0.5
    writer.close()


async def test_reset_all_connections_aborts_open_connections(
    echo_proxy: ImpairedNetworkProxy,
) -> None:
    reader, writer = await _connect(echo_proxy)
    await _round_trip(reader, writer, b"warm")

    echo_proxy.reset_all_connections()

    with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
        await _round_trip(reader, writer, b"lost")


async def test_mean_connection_lifetime_resets_long_lived_connections(
    echo_proxy: ImpairedNetworkProxy,
) -> None:
    echo_proxy.set_impairment(Impairment(mean_connection_lifetime_s=0.1))
    reader, writer = await _connect(echo_proxy)

    with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
        await asyncio.wait_for(reader.readexactly(1), timeout=10)

    assert echo_proxy.stats.connections_reset == 1
    writer.close()


async def test_a_lifetime_applied_later_resets_connections_already_open(
    echo_proxy: ImpairedNetworkProxy,
) -> None:
    reader, writer = await _connect(echo_proxy)
    await _round_trip(reader, writer, b"warm")

    echo_proxy.set_impairment(Impairment(mean_connection_lifetime_s=0.1))

    with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
        await asyncio.wait_for(reader.readexactly(1), timeout=10)
    writer.close()


async def test_idle_cut_closes_quiet_connections(
    echo_proxy: ImpairedNetworkProxy,
) -> None:
    echo_proxy.set_impairment(Impairment(idle_cut_s=0.2))
    reader, writer = await _connect(echo_proxy)

    assert await asyncio.wait_for(reader.read(1), timeout=10) == b""
    assert echo_proxy.stats.connections_idle_cut == 1
    writer.close()


_SSE_HEAD = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/event-stream\r\n"
    b"Cache-Control: no-cache\r\n\r\n"
)


def _sse_server(
    released: asyncio.Event,
) -> ServerHandler:
    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(_SSE_HEAD)
        writer.write(b"event: heartbeat\ndata: {}\n\n")
        await writer.drain()
        await released.wait()
        writer.close()

    return handler


async def test_event_stream_body_is_held_until_upstream_closes() -> None:
    released = asyncio.Event()
    async with (
        _upstream(_sse_server(released)) as port,
        ImpairedNetworkProxy(
            "127.0.0.1", port, Impairment(event_stream_buffer_bytes=64 * 1024)
        ) as proxy,
    ):
        reader, writer = await _connect(proxy)
        writer.write(b"GET /stream HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)

        body_before_close = asyncio.create_task(reader.read(1024))
        done, _ = await asyncio.wait({body_before_close}, timeout=0.3)
        released.set()
        body = await asyncio.wait_for(body_before_close, timeout=10)

        assert b"text/event-stream" in head
        assert not done
        assert body.startswith(b"event: heartbeat")
        writer.close()


async def test_buffering_turned_on_mid_stream_holds_a_stream_already_open() -> None:
    send_more = asyncio.Event()
    released = asyncio.Event()

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(_SSE_HEAD)
        writer.write(b"event: heartbeat\ndata: {}\n\n")
        await writer.drain()
        await send_more.wait()
        writer.write(b"event: turn_ended\ndata: {}\n\n")
        await writer.drain()
        await released.wait()
        writer.close()

    async with (
        _upstream(handler) as port,
        ImpairedNetworkProxy("127.0.0.1", port) as proxy,
    ):
        reader, writer = await _connect(proxy)
        writer.write(b"GET /stream HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"event: heartbeat"), timeout=10)
        await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=10)

        proxy.set_impairment(Impairment(event_stream_buffer_bytes=64 * 1024))
        send_more.set()
        held = asyncio.create_task(reader.read(1024))
        done, _ = await asyncio.wait({held}, timeout=0.3)
        released.set()
        body = await asyncio.wait_for(held, timeout=10)

        assert not done
        assert body.startswith(b"event: turn_ended")
        writer.close()


def test_a_response_head_split_across_reads_is_still_recognised() -> None:
    hold = EventStreamHold()

    first = hold.feed(b"HTTP/1.1 200 OK\r\nContent-Ty", 1024)
    second = hold.feed(b"pe: text/event-stream\r\n\r\nevent: heartbeat\n\n", 1024)

    assert first == []
    assert second == [b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"]
