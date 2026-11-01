"""One log line per step of a chat turn and per event stream.

The conversation stream hub is in memory and the client's reports arrive late,
so after the fact only the backend log can say when a turn reached each step, or
why a stream ended. Each line is a logger tag followed by ``key=value`` pairs,
which is what makes them searchable in the log store by ``turn_id``. No message
content goes here.

The vocabulary is documented in ``src/family_assistant/web/CLAUDE.md``.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_HEARTBEAT_FRAME = "event: heartbeat"
_DROPPED_FRAME = "event: stream_dropped"
_DROP_REASON = re.compile(r'"reason":\s*"(\w+)"')


def _rendered(fields: dict[str, object]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        shown = round(value, 3) if isinstance(value, float) else value
        parts.append(f"{key}={json.dumps(shown, default=str)}")
    return " ".join(parts)


def log_turn_event(
    event: str, *, conversation_id: str, turn_id: str, **fields: object
) -> None:
    """Record one step of a turn."""
    logger.info(
        "chat_turn %s",
        _rendered({
            "event": event,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            **fields,
        }),
    )


@dataclass
class _StreamTally:
    opened_at: float
    last_event_at: float
    events: int = 0
    heartbeats: int = 0
    reason: str = "finished"

    def count(self, frame: str, now: float) -> None:
        if frame.startswith(_HEARTBEAT_FRAME):
            self.heartbeats += 1
            return
        self.events += 1
        self.last_event_at = now
        if frame.startswith(_DROPPED_FRAME):
            dropped = _DROP_REASON.search(frame)
            self.reason = dropped.group(1) if dropped else "dropped"


async def observed_stream(
    frames: AsyncGenerator[str],
    *,
    kind: str,
    conversation_id: str | None = None,
    follow: bool | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncGenerator[str]:
    """Pass an event stream through, recording when it opened and how it ended.

    Every stream a client holds should go through this, so a stream that stalls
    or dies leaves the same record whichever endpoint served it.
    """
    identity = {"kind": kind, "conversation_id": conversation_id, "follow": follow}
    opened_at = clock()
    tally = _StreamTally(opened_at=opened_at, last_event_at=opened_at)
    logger.info("chat_stream %s", _rendered({"event": "opened", **identity}))
    try:
        async for frame in frames:
            tally.count(frame, clock())
            yield frame
    except (asyncio.CancelledError, GeneratorExit):
        tally.reason = "client_gone"
        raise
    except Exception:
        tally.reason = "error"
        raise
    finally:
        try:
            await frames.aclose()
        finally:
            closed_at = clock()
            logger.info(
                "chat_stream %s",
                _rendered({
                    "event": "closed",
                    **identity,
                    "reason": tally.reason,
                    "duration_s": closed_at - tally.opened_at,
                    "events": tally.events,
                    "heartbeats": tally.heartbeats,
                    "quiet_s": closed_at - tally.last_event_at,
                }),
            )
