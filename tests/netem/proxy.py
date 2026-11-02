"""A TCP proxy that makes a good network behave like a bad one.

Tests and the sync benchmark put it between a client and a real server to
reproduce the conditions mobile clients meet: high and variable latency, a slow
link, connections that are reset, stretches where packets vanish without a
reset, front doors that cut idle connections, and front doors that buffer
``text/event-stream`` responses instead of passing them through.

Everything is seeded, so a scenario that fails can be replayed. Impairments can
be changed while connections are open, which is how a scenario starts a
blackhole or a hand-over at a chosen moment rather than on a timer.

See docs/design/mobile-state-sync.md, "Measurement and test infrastructure".
"""

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 16 * 1024
_EVENT_STREAM_MARKER = b"content-type: text/event-stream"


@dataclass(frozen=True)
class Impairment:
    """How the link behaves. The default is a perfect link.

    Latency and jitter apply to each direction, so the round trip is about
    twice ``latency_s``. Ordering is preserved within a direction: jitter
    delays a chunk, it never lets a later chunk overtake it, because TCP does
    not deliver out of order either.
    """

    latency_s: float = 0.0
    jitter_s: float = 0.0
    bandwidth_bytes_per_s: float | None = None
    # Mean lifetime of a connection before the proxy resets it (RST on both
    # sides). Exponentially distributed, so short-lived and long-lived requests
    # are both exposed. None means connections are never reset.
    mean_connection_lifetime_s: float | None = None
    # Close a connection on which no byte has moved in either direction for
    # this long, like a load balancer's idle timeout.
    idle_cut_s: float | None = None
    # Hold the body of a text/event-stream response until this many bytes
    # have accumulated or the upstream closes, like a buffering front door.
    # The response head is passed through, as Envoy does once it is flushed.
    event_stream_buffer_bytes: int | None = None


@dataclass
class ProxyStats:
    connections_opened: int = 0
    connections_reset: int = 0
    connections_idle_cut: int = 0
    bytes_to_server: int = 0
    bytes_to_client: int = 0


@dataclass(eq=False)
class _Connection:
    client_writer: asyncio.StreamWriter
    upstream_writer: asyncio.StreamWriter
    last_activity: float
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def abort(self) -> None:
        for writer in (self.client_writer, self.upstream_writer):
            transport = writer.transport
            if not transport.is_closing():
                transport.abort()

    def close(self) -> None:
        for writer in (self.client_writer, self.upstream_writer):
            if not writer.transport.is_closing():
                writer.close()


class ImpairedNetworkProxy:
    """Listen on a local port and forward to ``upstream_host:upstream_port``."""

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        impairment: Impairment | None = None,
        *,
        seed: int = 0,
    ) -> None:
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._impairment = impairment or Impairment()
        self._random = random.Random(seed)
        self._server: asyncio.Server | None = None
        self._connections: set[_Connection] = set()
        self._blackhole_until = 0.0
        self._blackhole_changed = asyncio.Event()
        self.stats = ProxyStats()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("proxy is not started")
        return self._server.sockets[0].getsockname()[1]

    @property
    def impairment(self) -> Impairment:
        return self._impairment

    def set_impairment(self, impairment: Impairment) -> None:
        """Change the link for new and already-open connections alike."""
        self._impairment = impairment

    def blackhole(self, duration_s: float) -> None:
        """Stop delivering bytes in both directions for ``duration_s``.

        Nothing is reset: connections stay open and bytes sent meanwhile are
        delivered when the window ends, as TCP retransmission would deliver
        them once the path comes back. A client that relies on the connection
        noticing the outage on its own will not notice.
        """
        loop = asyncio.get_running_loop()
        self._blackhole_until = max(self._blackhole_until, loop.time() + duration_s)
        self._blackhole_changed.set()

    def reset_all_connections(self) -> int:
        """Reset every open connection, like a Wi-Fi to cellular hand-over."""
        connections = list(self._connections)
        for connection in connections:
            connection.abort()
        self.stats.connections_reset += len(connections)
        return len(connections)

    @property
    def open_connections(self) -> int:
        return len(self._connections)

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = await asyncio.start_server(self._handle_client, host, port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for connection in list(self._connections):
            connection.abort()
            for task in connection.tasks:
                task.cancel()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> "ImpairedNetworkProxy":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self._upstream_host, self._upstream_port
            )
        except OSError:
            logger.warning("Proxy could not reach upstream", exc_info=True)
            client_writer.transport.abort()
            return

        loop = asyncio.get_running_loop()
        connection = _Connection(
            client_writer=client_writer,
            upstream_writer=upstream_writer,
            last_activity=loop.time(),
        )
        self._connections.add(connection)
        self.stats.connections_opened += 1
        connection.tasks = [
            asyncio.create_task(
                self._pump(connection, client_reader, upstream_writer, to_server=True)
            ),
            asyncio.create_task(
                self._pump(connection, upstream_reader, client_writer, to_server=False)
            ),
            asyncio.create_task(self._supervise(connection)),
        ]
        try:
            await asyncio.gather(*connection.tasks[:2], return_exceptions=True)
        finally:
            connection.tasks[2].cancel()
            self._connections.discard(connection)
            connection.close()

    async def _supervise(self, connection: _Connection) -> None:
        """Enforce the connection lifetime and the idle cut.

        Both follow the impairment in force now, not the one the connection
        opened under, so a profile applied mid-scenario reaches connections
        that were already open.
        """
        loop = asyncio.get_running_loop()
        scheduled_for: float | None = None
        reset_at: float | None = None
        while True:
            lifetime = self._impairment.mean_connection_lifetime_s
            if lifetime != scheduled_for:
                scheduled_for = lifetime
                reset_at = (
                    loop.time() + self._random.expovariate(1.0 / lifetime)
                    if lifetime
                    else None
                )
            # ast-grep-ignore: no-asyncio-sleep-in-tests - the proxy's supervisor ticks on wall-clock time by design; it is the network simulator, not a test waiting on a condition
            await asyncio.sleep(0.05)
            now = loop.time()
            if reset_at is not None and now >= reset_at:
                self.stats.connections_reset += 1
                connection.abort()
                return
            idle_cut = self._impairment.idle_cut_s
            if idle_cut is not None and now - connection.last_activity >= idle_cut:
                self.stats.connections_idle_cut += 1
                connection.close()
                return

    async def _pump(
        self,
        connection: _Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        to_server: bool,
    ) -> None:
        try:
            await self._forward(connection, reader, writer, to_server=to_server)
        except (ConnectionError, OSError):
            connection.abort()

    async def _forward(
        self,
        connection: _Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        to_server: bool,
    ) -> None:
        """Read as bytes arrive and deliver each chunk when the link would.

        Reading and delivering run separately, so latency is paid once per
        chunk in flight rather than once per chunk in turn: a response that
        spans many reads takes one round trip plus its serialisation time, as
        on a real link.
        """
        queue: asyncio.Queue[tuple[float, bytes] | None] = asyncio.Queue()
        sender = asyncio.create_task(
            self._send_in_order(connection, queue, writer, to_server=to_server)
        )
        try:
            await self._schedule(connection, reader, queue, to_server=to_server)
            await queue.put(None)
            await sender
        finally:
            sender.cancel()

    async def _schedule(
        self,
        connection: _Connection,
        reader: asyncio.StreamReader,
        queue: asyncio.Queue[tuple[float, bytes] | None],
        *,
        to_server: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        # When the link finishes putting the previous chunk on the wire, and
        # when the previous chunk arrives: bandwidth queues chunks behind each
        # other, and arrival order never changes, as with TCP.
        link_free_at = loop.time()
        last_arrival = loop.time()
        hold = None if to_server else EventStreamHold()
        async for received in _read_chunks(reader):
            connection.last_activity = loop.time()
            impairment = self._impairment
            outgoing = (
                hold.feed(received, impairment.event_stream_buffer_bytes)
                if hold is not None
                else [received]
            )
            for chunk in outgoing:
                if not chunk:
                    continue
                now = loop.time()
                link_free_at = max(now, link_free_at)
                if impairment.bandwidth_bytes_per_s:
                    link_free_at += len(chunk) / impairment.bandwidth_bytes_per_s
                propagation = impairment.latency_s
                if impairment.jitter_s:
                    propagation += self._random.uniform(0.0, impairment.jitter_s)
                last_arrival = max(link_free_at + propagation, last_arrival)
                await queue.put((last_arrival, chunk))
        if hold is not None and (remainder := hold.flush()):
            await queue.put((max(loop.time(), last_arrival), remainder))

    async def _send_in_order(
        self,
        connection: _Connection,
        queue: asyncio.Queue[tuple[float, bytes] | None],
        writer: asyncio.StreamWriter,
        *,
        to_server: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        try:
            while (item := await queue.get()) is not None:
                arrival, chunk = item
                await self._deliver(writer, chunk, arrival, to_server)
                connection.last_activity = loop.time()
        except (ConnectionError, OSError):
            # The reader may be waiting on a peer that is still connected;
            # resetting both sides ends it the way a broken path would.
            connection.abort()
            raise
        if writer.can_write_eof() and not writer.transport.is_closing():
            writer.write_eof()

    async def _deliver(
        self,
        writer: asyncio.StreamWriter,
        chunk: bytes,
        deliver_at: float,
        to_server: bool,
    ) -> None:
        if not chunk:
            return
        loop = asyncio.get_running_loop()
        while True:
            wait_until = max(deliver_at, self._blackhole_until)
            remaining = wait_until - loop.time()
            if remaining <= 0:
                break
            self._blackhole_changed.clear()
            # A blackhole started while this chunk waits extends the wait, so
            # wake up when either the deadline passes or the window changes.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._blackhole_changed.wait(), remaining)
        if writer.transport.is_closing():
            raise ConnectionResetError("peer connection already closed")
        writer.write(chunk)
        await writer.drain()
        if to_server:
            self.stats.bytes_to_server += len(chunk)
        else:
            self.stats.bytes_to_client += len(chunk)


class EventStreamHold:
    """Withhold an event-stream response body the way a buffering proxy does.

    The response head goes through at once; the body is released only in
    blocks of ``limit`` bytes, or when the upstream closes. Responses are
    recognised whether or not buffering is on, so turning it on mid-stream
    holds a stream that was already open. A response head split across reads
    is gathered before it is classified.
    """

    def __init__(self) -> None:
        self._event_stream = False
        self._head = bytearray()
        self._held = bytearray()

    def feed(self, received: bytes, limit: int | None) -> list[bytes]:
        if self._event_stream:
            self._held.extend(received)
            return self._release(limit)
        if not self._head and not received.startswith(b"HTTP/"):
            return [received]
        self._head.extend(received)
        end = self._head.find(b"\r\n\r\n")
        if end < 0:
            return []
        head = bytes(self._head[: end + 4])
        body = bytes(self._head[end + 4 :])
        self._head.clear()
        if _EVENT_STREAM_MARKER not in head.lower():
            return [head, body]
        # An event stream never ends while the connection is open, so
        # everything after its head on this connection is its body.
        self._event_stream = True
        self._held.extend(body)
        return [head, *self._release(limit)]

    def flush(self) -> bytes:
        remainder = bytes(self._head) + bytes(self._held)
        self._head.clear()
        self._held.clear()
        return remainder

    def _release(self, limit: int | None) -> list[bytes]:
        if limit is not None and len(self._held) < limit:
            return []
        return [self.flush()]


async def _read_chunks(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    while True:
        chunk = await reader.read(_CHUNK_SIZE)
        if not chunk:
            return
        yield chunk
