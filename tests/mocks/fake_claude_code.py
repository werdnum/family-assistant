"""A fake Claude Code CLI, for driving the Claude Agent SDK without a subscription.

``FakeClaudeCode`` is an Agent SDK ``Transport``: the SDK's own ``Query``
machinery talks to it exactly as it talks to the CLI subprocess, over the
stream-json control protocol -- initialize, the in-process MCP handshake,
``PreToolUse`` hook callbacks, MCP ``tools/call`` requests, interrupts. The
frame shapes are those of a recorded real session
(``tests/data/claude_agent_sdk/recorded_session.jsonl``), so what the loop
parses is what Claude Code actually sends.

A test scripts the model: each ``FakeResponse`` is one API response, a list of
text and tool-use blocks. Tool calls go through the session's real MCP server
and hook, and their results are recorded on the fake.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import Transport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

SESSION_ID = "00000000-0000-4000-8000-000000000001"

# ast-grep-ignore: no-dict-any - a stream-json wire frame is arbitrary JSON from the CLI protocol
type Frame = dict[str, Any]


@dataclass(frozen=True)
class FakeText:
    text: str


@dataclass(frozen=True)
class FakeToolUse:
    """A call to one of the session's MCP tools, or a built-in by full name."""

    name: str
    input: Frame
    id: str = field(default_factory=lambda: f"toolu_{uuid.uuid4().hex[:24]}")


@dataclass(frozen=True)
class FakeResponse:
    blocks: Sequence[FakeText | FakeToolUse]
    model: str = "claude-opus-5-5"
    error: str | None = None


@dataclass
class FakeClaudeCode(Transport):
    """Plays scripted responses through the Agent SDK's control protocol."""

    responses: Sequence[FakeResponse]
    mcp_server_name: str = "family_assistant"
    api_key_source: str = "none"
    result_subtype: str = "success"
    tool_results: dict[str, Frame] = field(default_factory=dict)
    offered_tools: list[Frame] = field(default_factory=list)
    prompts: list[Frame] = field(default_factory=list)
    interrupted: bool = False
    # Sent before the first response, as Claude Code does when a window's
    # state changes, e.g. {"status": "allowed_warning", "rateLimitType":
    # "five_hour", "utilization": 0.9, "resetsAt": 1794730800}.
    rate_limit_info: Frame | None = None
    # Where the session's transcript would be on disk. When set, the session
    # mirrors a transcript of its prompts and responses there before its result,
    # as Claude Code does for an SDK session store.
    transcript_path: str | None = None
    transcript: list[Frame] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._out: asyncio.Queue[Frame | None] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[Frame]] = {}
        self._ready = False
        self._script: asyncio.Task[None] | None = None
        self._mcp_id = 0

    # -- Transport ----------------------------------------------------------

    async def connect(self) -> None:
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        pass

    async def close(self) -> None:
        self._ready = False
        if self._script is not None:
            self._script.cancel()
            await asyncio.gather(self._script, return_exceptions=True)
        await self._out.put(None)

    def read_messages(self) -> AsyncIterator[Frame]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[Frame]:
        while True:
            frame = await self._out.get()
            if frame is None:
                return
            yield frame

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                await self._receive(json.loads(line))

    # -- protocol -----------------------------------------------------------

    async def _receive(self, frame: Frame) -> None:
        kind = frame.get("type")
        if kind == "control_request":
            request = frame["request"]
            if request["subtype"] == "initialize":
                await self._respond(frame["request_id"], {"commands": [], "models": []})
            elif request["subtype"] == "interrupt":
                self.interrupted = True
                if self._script is not None:
                    self._script.cancel()
                await self._respond(frame["request_id"], {})
                await self._emit_result(subtype="error_during_execution", aborted=True)
            else:
                await self._respond(frame["request_id"], {})
        elif kind == "control_response":
            response = frame["response"]
            future = self._pending.pop(response["request_id"], None)
            if future is not None and not future.done():
                future.set_result(response.get("response") or {})
        elif kind == "user":
            self.prompts.append(frame["message"])
            self._script = asyncio.create_task(self._run())

    async def _respond(self, request_id: str, response: Frame) -> None:
        await self._out.put({
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        })

    async def _request(self, request: Frame) -> Frame:
        request_id = str(uuid.uuid4())
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._out.put({
            "type": "control_request",
            "request_id": request_id,
            "request": request,
        })
        return await future

    async def _mcp(self, method: str, params: Frame) -> Frame:
        self._mcp_id += 1
        response = await self._request({
            "subtype": "mcp_message",
            "server_name": self.mcp_server_name,
            "message": {
                "jsonrpc": "2.0",
                "id": self._mcp_id,
                "method": method,
                "params": params,
            },
        })
        return response["mcp_response"]

    # -- the scripted session -----------------------------------------------

    async def _run(self) -> None:
        await self._mcp(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "2.1.325"},
            },
        )
        listed = await self._mcp("tools/list", {})
        self.offered_tools = listed["result"]["tools"]
        await self._out.put({
            "type": "system",
            "subtype": "init",
            "cwd": "/tmp/claude-agent-cwd",
            "session_id": SESSION_ID,
            "tools": ["ToolSearch"]
            + [f"mcp__{self.mcp_server_name}__{t['name']}" for t in self.offered_tools],
            "mcp_servers": [
                {"name": self.mcp_server_name, "status": "connected", "source": "sdk"}
            ],
            "model": "claude-opus-5-5",
            "permissionMode": "dontAsk",
            "slash_commands": [],
            "apiKeySource": self.api_key_source,
            "claude_code_version": "2.1.325",
            "output_style": "default",
            "agents": [],
            "skills": [],
            "plugins": [],
            "uuid": str(uuid.uuid4()),
        })
        if self.rate_limit_info is not None:
            await self._out.put({
                "type": "rate_limit_event",
                "rate_limit_info": self.rate_limit_info,
                "uuid": str(uuid.uuid4()),
                "session_id": SESSION_ID,
            })
        for response in self.responses:
            await self._play(response)
        if self.transcript_path is not None:
            entries = [
                {"type": "user", "uuid": str(uuid.uuid4()), "message": prompt}
                for prompt in self.prompts[-1:]
            ] + [
                {"type": "assistant", "uuid": str(uuid.uuid4()), "text": text}
                for response in self.responses
                for text in (b.text for b in response.blocks if isinstance(b, FakeText))
            ]
            self.transcript.extend(entries)
            await self._out.put({
                "type": "transcript_mirror",
                "filePath": self.transcript_path,
                "entries": entries,
            })
        await self._emit_result(subtype=self.result_subtype)

    async def _play(self, response: FakeResponse) -> None:
        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        usage = {
            "input_tokens": 2,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 100,
            },
            "output_tokens": 20,
        }
        await self._stream({
            "type": "message_start",
            "message": {"model": response.model, "id": message_id, "content": []},
        })
        tool_uses: list[FakeToolUse] = []
        for index, block in enumerate(response.blocks):
            if isinstance(block, FakeText):
                await self._stream({
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": block.text},
                })
                content = {"type": "text", "text": block.text}
            else:
                content = {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            await self._out.put({
                "type": "assistant",
                "message": {
                    "model": response.model,
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [content],
                    "usage": usage,
                },
                "parent_tool_use_id": None,
                "session_id": SESSION_ID,
                "uuid": str(uuid.uuid4()),
                **({"error": response.error} if response.error else {}),
            })
            if isinstance(block, FakeToolUse):
                tool_uses.append(block)
                await self._request({
                    "subtype": "hook_callback",
                    "callback_id": "hook_0",
                    "input": {
                        "session_id": SESSION_ID,
                        "hook_event_name": "PreToolUse",
                        "tool_name": block.name,
                        "tool_input": block.input,
                        "tool_use_id": block.id,
                    },
                    "tool_use_id": block.id,
                })
        await self._stream({
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool_uses else "end_turn"},
            "usage": usage,
        })
        await self._stream({"type": "message_stop"})

        prefix = f"mcp__{self.mcp_server_name}__"
        results = await asyncio.gather(
            *(
                self._mcp(
                    "tools/call",
                    {"name": use.name.removeprefix(prefix), "arguments": use.input},
                )
                for use in tool_uses
                if use.name.startswith(prefix)
            )
        )
        for use, result in zip(
            (use for use in tool_uses if use.name.startswith(prefix)),
            results,
            strict=True,
        ):
            self.tool_results[use.id] = result["result"]
            await self._out.put({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": use.id,
                            "content": result["result"]["content"],
                        }
                    ],
                },
                "parent_tool_use_id": None,
                "session_id": SESSION_ID,
                "uuid": str(uuid.uuid4()),
            })

    async def _stream(self, event: Frame) -> None:
        await self._out.put({
            "type": "stream_event",
            "event": event,
            "session_id": SESSION_ID,
            "parent_tool_use_id": None,
            "uuid": str(uuid.uuid4()),
        })

    async def _emit_result(self, *, subtype: str, aborted: bool = False) -> None:
        await self._out.put({
            "type": "result",
            "subtype": subtype,
            "duration_ms": 10,
            "duration_api_ms": 5,
            "is_error": subtype not in {"success"} and not aborted,
            "num_turns": len(self.responses),
            "session_id": SESSION_ID,
            "stop_reason": "end_turn",
            "total_cost_usd": 0.0,
            "usage": {},
            "result": "",
            "terminal_reason": "aborted_tools" if aborted else "completed",
            "uuid": str(uuid.uuid4()),
        })
        await self._out.put({
            "type": "system",
            "subtype": "session_state_changed",
            "state": "idle",
            "sdk_host_only": True,
            "session_id": SESSION_ID,
            "uuid": str(uuid.uuid4()),
        })


RECORDED_SESSION = (
    Path(__file__).parent.parent
    / "data"
    / "claude_agent_sdk"
    / "recorded_session.jsonl"
)
_RECORDED_SERVER_NAME = "fa"


@dataclass
class ReplayClaudeCode(Transport):
    """Replays a recorded real session's CLI frames verbatim.

    Frames the CLI sent are played in order; at each frame the SDK sent, the
    replay waits for the SDK to send one. Only what cannot be identical is
    rewritten: the SDK's fresh request id on the answer to its own request,
    and the MCP server's name.
    """

    mcp_server_name: str = "family_assistant"

    def __post_init__(self) -> None:
        self._out: asyncio.Queue[Frame | None] = asyncio.Queue()
        self._written: asyncio.Queue[Frame] = asyncio.Queue()
        self._ready = False
        self._replay: asyncio.Task[None] | None = None
        recorded = RECORDED_SESSION.read_text().replace(
            f"mcp__{_RECORDED_SERVER_NAME}__", f"mcp__{self.mcp_server_name}__"
        )
        self._frames = [json.loads(line) for line in recorded.splitlines() if line]

    async def connect(self) -> None:
        self._ready = True
        self._replay = asyncio.create_task(self._run())

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        pass

    async def close(self) -> None:
        self._ready = False
        if self._replay is not None:
            self._replay.cancel()
            await asyncio.gather(self._replay, return_exceptions=True)
        await self._out.put(None)

    def read_messages(self) -> AsyncIterator[Frame]:
        return self._read()

    async def _read(self) -> AsyncIterator[Frame]:
        while True:
            frame = await self._out.get()
            if frame is None:
                return
            yield frame

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                await self._written.put(json.loads(line))

    async def _run(self) -> None:
        sdk_request_id: str | None = None
        for entry in self._frames:
            frame = entry["frame"]
            if entry["dir"] == "out":
                sent = await self._written.get()
                if sent.get("type") == "control_request":
                    sdk_request_id = sent["request_id"]
                continue
            request = frame.get("request") or {}
            if request.get("server_name") == _RECORDED_SERVER_NAME:
                request["server_name"] = self.mcp_server_name
            if frame.get("type") == "control_response" and sdk_request_id:
                frame["response"]["request_id"] = sdk_request_id
            await self._out.put(frame)
