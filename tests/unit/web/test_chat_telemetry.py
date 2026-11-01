"""The backend log says how far a turn got and how each stream ended."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime

import pytest

from family_assistant.web.chat_telemetry import log_turn_event, observed_stream
from family_assistant.web.conversation_stream_hub import ConversationStreamHub

LOGGER = "family_assistant.web.chat_telemetry"


def _lines(caplog: pytest.LogCaptureFixture, tag: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(tag)]


async def _frames(frames: Sequence[str]) -> AsyncGenerator[str]:
    for frame in frames:
        yield frame


@pytest.mark.no_db
def test_a_turn_line_is_searchable_and_skips_empty_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        log_turn_event(
            "ended",
            conversation_id="conv",
            turn_id="turn",
            duration_s=1.23456,
            error=None,
        )

    assert _lines(caplog, "chat_turn") == [
        'chat_turn event="ended" conversation_id="conv" turn_id="turn" duration_s=1.235'
    ]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_a_stream_that_runs_out_records_what_it_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    frames = ["event: heartbeat\ndata: {}\n\n", "event: text\ndata: {}\n\n"]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        stream = observed_stream(_frames(frames), kind="activity")
        assert [frame async for frame in stream] == frames

    closed = _lines(caplog, "chat_stream")[-1]
    assert 'event="closed"' in closed
    assert 'kind="activity"' in closed
    assert 'reason="finished"' in closed
    assert "events=1" in closed
    assert "heartbeats=1" in closed


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_a_dropped_stream_records_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dropped = 'event: stream_dropped\ndata: {"reason": "queue_overflow"}\n\n'
    with caplog.at_level(logging.INFO, logger=LOGGER):
        stream = observed_stream(_frames([dropped]), kind="conversation")
        assert [frame async for frame in stream] == [dropped]

    assert 'reason="queue_overflow"' in _lines(caplog, "chat_stream")[-1]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_closing_a_stream_records_the_client_left_and_closes_the_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_closed = asyncio.Event()

    async def source() -> AsyncGenerator[str]:
        try:
            yield "event: text\ndata: {}\n\n"
            yield "event: text\ndata: {}\n\n"
        finally:
            source_closed.set()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        stream = observed_stream(source(), kind="sync")
        await anext(stream)
        await stream.aclose()

    assert source_closed.is_set()
    assert 'reason="client_gone"' in _lines(caplog, "chat_stream")[-1]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_a_stream_records_closing_even_if_its_cleanup_is_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def source() -> AsyncGenerator[str]:
        try:
            yield "event: text\ndata: {}\n\n"
        finally:
            raise asyncio.CancelledError

    with (
        caplog.at_level(logging.INFO, logger=LOGGER),
        pytest.raises(asyncio.CancelledError),
    ):
        stream = observed_stream(source(), kind="sync")
        await anext(stream)
        await stream.aclose()

    assert 'event="closed"' in _lines(caplog, "chat_stream")[-1]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_a_stream_that_raises_records_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def source() -> AsyncGenerator[str]:
        yield "event: text\ndata: {}\n\n"
        raise RuntimeError("boom")

    with (
        caplog.at_level(logging.INFO, logger=LOGGER),
        pytest.raises(RuntimeError, match="boom"),
    ):
        async for _ in observed_stream(source(), kind="sync"):
            pass

    assert 'reason="error"' in _lines(caplog, "chat_stream")[-1]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_a_turn_records_each_step_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = ConversationStreamHub()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await hub.start_turn(
            "conv", turn_id="turn", user_id="user", started_at=datetime.now(UTC)
        )
        await hub.publish("conv", "text", turn_id="turn", payload={"content": "a"})
        await hub.publish("conv", "text", turn_id="turn", payload={"content": "b"})
        ended = await hub.end_turn("conv", turn_id="turn", status="complete")
        await hub.end_turn("conv", turn_id="turn", status="failed")
        await hub.ack_conversation("conv", ended.seq)
        await hub.ack_conversation("conv", ended.seq)

    events = [line.split()[1] for line in _lines(caplog, "chat_turn")]
    assert events == [
        'event="started"',
        'event="first_output"',
        'event="ended"',
        'event="acked"',
    ]
