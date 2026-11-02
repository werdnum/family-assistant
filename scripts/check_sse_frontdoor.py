#!/usr/bin/env python3
"""Check whether a long-lived response survives the production front door.

Opens an event stream through the public URL and prints when each frame
actually arrives. The server sends a heartbeat at a fixed cadence (30 s by
default), so frames that arrive in bursts, late, or not at all show the front
door buffering or cutting the stream, which is invisible from the server side.

    FA_ACCESS_TOKEN=... scripts/check_sse_frontdoor.py --duration 150

The token is a user access token (the same bearer token the apps send); the
read-only diagnostics token is not accepted by the chat endpoints. See
docs/design/mobile-state-sync.md, "Front door check".
"""

import argparse
import asyncio
import os
import sys
import time

import httpx

DEFAULT_BASE_URL = "https://assistant.andrewgarrett.dev"
DEFAULT_PATH = "/api/v1/chat/activity/stream"


async def _read_frames(
    client: httpx.AsyncClient,
    path: str,
    headers: dict[str, str],
    started: float,
    arrivals: list[float],
) -> bool:
    """Print each frame's arrival; return False if the response was not a stream."""
    async with client.stream("GET", path, headers=headers) as response:
        print(
            f"{time.monotonic() - started:7.2f}s  HTTP {response.status_code} "
            f"content-type={response.headers.get('content-type')}"
        )
        if response.status_code != 200:
            await response.aread()
            print(response.text[:500])
            return False
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                at = time.monotonic() - started
                arrivals.append(at)
                print(f"{at:7.2f}s  {line.strip()}")
    return True


async def probe(base_url: str, path: str, token: str, duration_s: float) -> int:
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    timeout = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)
    started = time.monotonic()
    arrivals: list[float] = []
    closed_reason = "server or front door closed the stream"
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        try:
            async with asyncio.timeout(duration_s):
                if not await _read_frames(client, path, headers, started, arrivals):
                    return 2
        except TimeoutError:
            closed_reason = "duration elapsed"
        except httpx.HTTPError as error:
            closed_reason = f"transport error: {error!r}"

    print(f"\nStream ended after {time.monotonic() - started:.1f}s: {closed_reason}")
    gaps = [
        later - earlier for earlier, later in zip(arrivals, arrivals[1:], strict=False)
    ]
    if gaps:
        print(
            f"{len(arrivals)} frames; gap between frames min {min(gaps):.1f}s, "
            f"max {max(gaps):.1f}s"
        )
    else:
        print(f"{len(arrivals)} frames")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether a long-lived response survives the front door."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--duration", type=float, default=150.0, help="seconds")
    parser.add_argument(
        "--token-env",
        default="FA_ACCESS_TOKEN",
        help="environment variable holding the bearer token",
    )
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        print(f"Set {args.token_env} to a user access token.", file=sys.stderr)
        return 2
    return asyncio.run(probe(args.base_url, args.path, token, args.duration))


if __name__ == "__main__":
    sys.exit(main())
