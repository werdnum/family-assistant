"""A real Tuit, seeded with a small world, answering the assistant's MCP calls.

Needs Node.js, PostgreSQL server binaries and a Tuit checkout with its npm
dependencies installed, named by ``$FA_EVAL_TUIT_DIR``; skipped otherwise.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent
from pydantic import BaseModel, JsonValue

from family_assistant.eval.workhorse.tuit_sandbox import TUIT_DIR_ENV, tuit_server
from family_assistant.eval.workhorse.tuit_world import (
    ActivityEntry,
    PinObservation,
    TaskObservation,
    TaskSnapshot,
    TuitWorld,
    WorldTask,
)

if TYPE_CHECKING:
    from mcp.types import CallToolResult

pytestmark = [
    pytest.mark.no_db,
    pytest.mark.skipif(shutil.which("node") is None, reason="Tuit needs Node.js"),
    pytest.mark.skipif(
        not os.environ.get(TUIT_DIR_ENV)
        or not (Path(os.environ[TUIT_DIR_ENV]) / "node_modules").is_dir(),
        reason=f"Tuit needs a checkout with dependencies in ${TUIT_DIR_ENV}",
    ),
    pytest.mark.skipif(
        shutil.which("initdb") is None
        and not Path("/opt/homebrew/opt/postgresql@18/bin/initdb").exists()
        and not os.environ.get("FA_EVAL_PG_BIN"),
        reason="Tuit needs PostgreSQL server binaries",
    ),
]

_MOMENT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
_CREATED = _MOMENT - timedelta(days=3)
_FA = {"user": "alex", "agent": "family-assistant"}


def _snapshot(
    task_id: str, title: str, created: datetime, **fields: object
) -> TaskSnapshot:
    return TaskSnapshot.model_validate({
        "id": task_id,
        "title": title,
        "owner": "alex",
        "next_actor": {"kind": "user", "user": "alex"},
        "created_at": created,
        "updated_at": created,
        "created_by": _FA,
        **fields,
    })


def _world() -> TuitWorld:
    vet = WorldTask(
        id="vetvet22",
        observations=[
            TaskObservation(
                seen_at=_MOMENT - timedelta(days=1),
                task=_snapshot(
                    "vetvet22",
                    "Book Milo's dental check",
                    _CREATED,
                    brief="Two clinics nearby.",
                    next_action="Ring the closer clinic",
                    area="home",
                    deadline={"date": "2026-10-20"},
                    updated_at=_CREATED + timedelta(hours=2),
                ),
            )
        ],
        activity=[
            ActivityEntry(
                id=3,
                kind="research",
                body="Found two clinics within 5 km.",
                happened_at=_CREATED + timedelta(hours=2),
                recorded_at=_CREATED + timedelta(hours=2),
                author={"user": "alex", "agent": "chatgpt"},
            )
        ],
        pins=[PinObservation(seen_at=_MOMENT, user="alex", pinned=True)],
    )
    waiting_since = _CREATED + timedelta(days=1)
    plumber = WorldTask(
        id="plumb333",
        observations=[
            TaskObservation(
                seen_at=_MOMENT,
                task=_snapshot(
                    "plumb333",
                    "Fix the leaking tap",
                    _CREATED,
                    state="waiting",
                    waiting={
                        "kind": "reply",
                        "for": "the plumber's quote",
                        "since": waiting_since.isoformat(),
                        "follow_up": {"date": "2026-10-05"},
                    },
                    updated_at=waiting_since,
                ),
            )
        ],
    )
    later = WorldTask(
        id="later444",
        observations=[
            TaskObservation(
                seen_at=_MOMENT + timedelta(days=2),
                task=_snapshot(
                    "later444", "Not yet asked for", _MOMENT + timedelta(days=1)
                ),
            )
        ],
    )
    return TuitWorld(users=["alex"], tasks=[vet, plumber, later])


class _Found(BaseModel):
    tasks: list[TaskSnapshot]


class _View(BaseModel):
    task: TaskSnapshot
    status: dict[str, JsonValue] = {}
    activity: list[ActivityEntry] = []


def _text(result: CallToolResult) -> str:
    assert not result.isError, result.content
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def test_seeded_tuit_serves_the_world_over_mcp(tmp_path: Path) -> None:
    with tuit_server(
        _world(),
        _MOMENT,
        tmp_path,
        fa_users={"alex@example.com": "alex"},
        household=("alex", "sam"),
    ) as server:
        token = server.tokens["alex"]
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            response = await client.get(
                "/api/me", headers={"Authorization": f"Bearer {token}"}
            )
        me = response.json()
        assert (me["user"]["id"], me["agent"], me["can_write"]) == (
            "alex",
            "family-assistant",
            True,
        )
        assert server.environment == {"TUIT_ALEX_TOKEN": token}
        async with contextlib.AsyncExitStack() as stack:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(
                    server.mcp_url, headers={"Authorization": f"Bearer {token}"}
                )
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            found = _Found.model_validate_json(
                _text(await session.call_tool("find_tasks", {}))
            )
            tasks = {t.id: t for t in found.tasks}
            assert set(tasks) == {"vetvet22", "plumb333"}
            assert tasks["plumb333"].state == "waiting"
            assert tasks["vetvet22"].deadline == {"date": "2026-10-20"}

            before = _View.model_validate_json(
                _text(await session.call_tool("get_task", {"task_id": "vetvet22"}))
            )
            assert before.status["pinned"] is True
            assert [(a.kind, a.author.get("agent")) for a in before.activity] == [
                ("research", "chatgpt")
            ]

            await session.call_tool(
                "checkpoint",
                {
                    "task_id": "vetvet22",
                    "note": "Booked for Tuesday.",
                    "next_action": "Take Milo in on Tuesday",
                    "next_actor": "me",
                },
            )
            after = _View.model_validate_json(
                _text(await session.call_tool("get_task", {"task_id": "vetvet22"}))
            )

    assert after.task.next_action == "Take Milo in on Tuesday"
    assert after.activity[-1].body == "Booked for Tuesday."
    assert after.activity[-1].author == _FA
