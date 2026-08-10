"""Keychute-brokered HTTP API exposed to Monty scripts."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, TypedDict

from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
)

if TYPE_CHECKING:
    from asyncio.subprocess import Process
    from collections.abc import Mapping

    from family_assistant.config_models import KeychuteConfig
    from family_assistant.tools.types import ToolExecutionContext

_MAX_RESPONSE_HEAD_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_PROCESS_EXIT_SLACK_SECONDS = 30.0


class KeychuteHttpResponse(TypedDict):
    """Script-safe representation of an upstream response."""

    status_code: int
    headers: dict[str, list[str]]
    body: bytes


class KeychuteScriptError(RuntimeError):
    """Raised when a brokered HTTP call cannot be completed."""


async def _read_stream(
    stream: asyncio.StreamReader,
    *,
    limit: int,
    description: str,
    truncate: bool = False,
) -> bytes:
    """Read a subprocess stream without allowing unbounded memory growth."""
    chunks: list[bytes] = []
    total = 0
    was_truncated = False
    while chunk := await stream.read(64 * 1024):
        total += len(chunk)
        if total > limit:
            if truncate:
                retained = min(total - len(chunk), limit)
                if retained < limit:
                    chunks.append(chunk[: limit - retained])
                was_truncated = True
                continue
            raise KeychuteScriptError(f"{description} exceeded the {limit}-byte limit")
        chunks.append(chunk)
    result = b"".join(chunks)
    if was_truncated:
        result += b"\n... [truncated]"
    return result


async def _write_stdin(process: Process, body: bytes) -> None:
    """Write the request body and tolerate the child closing stdin on failure."""
    assert process.stdin is not None
    try:
        process.stdin.write(body)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()


async def _collect_process_output(
    process: Process,
    *,
    body: bytes | None,
    stdout_limit: int,
) -> tuple[int, bytes, bytes]:
    """Drain all subprocess pipes concurrently and enforce output limits."""
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(
        _read_stream(
            process.stdout,
            limit=stdout_limit,
            description="Keychute response",
        )
    )
    stderr_task = asyncio.create_task(
        _read_stream(
            process.stderr,
            limit=_MAX_STDERR_BYTES,
            description="Keychute diagnostics",
            truncate=True,
        )
    )
    stdin_task = (
        asyncio.create_task(_write_stdin(process, body)) if body is not None else None
    )
    try:
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        if stdin_task is not None:
            await stdin_task
        return await process.wait(), stdout, stderr
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        for task in (stdout_task, stderr_task, stdin_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        if stdin_task is not None:
            await asyncio.gather(stdin_task, return_exceptions=True)
        raise


def _parse_response(output: bytes) -> KeychuteHttpResponse:
    """Parse the byte-faithful ``keychute curl --include`` response."""
    head, separator, body = output.partition(b"\r\n\r\n")
    if not separator:
        raise KeychuteScriptError("Keychute returned a malformed HTTP response")

    lines = head.split(b"\r\n")
    status_parts = lines[0].split(b" ", 2)
    if len(status_parts) < 2 or status_parts[0] != b"HTTP/1.1":
        raise KeychuteScriptError("Keychute returned a malformed HTTP status line")
    try:
        status_code = int(status_parts[1])
    except ValueError as exc:
        raise KeychuteScriptError(
            "Keychute returned a malformed HTTP status code"
        ) from exc

    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, delimiter, value = line.partition(b":")
        if not delimiter:
            raise KeychuteScriptError("Keychute returned a malformed HTTP header")
        try:
            normalized_name = name.decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise KeychuteScriptError(
                "Keychute returned a non-ASCII HTTP header name"
            ) from exc
        headers.setdefault(normalized_name, []).append(
            value.lstrip(b" \t").decode("latin-1")
        )

    return KeychuteHttpResponse(
        status_code=status_code,
        headers=headers,
        body=body,
    )


class KeychuteScriptHttpClient:
    """Run Keychute's brokered curl flow for one script execution."""

    def __init__(
        self,
        config: KeychuteConfig,
        script_source: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> None:
        self._config = config
        self._script_source = script_source
        self._execution_context = execution_context

    async def request(
        self,
        secret_name: str,
        url: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: str | bytes | None = None,
        reason: str = "",
        ttl_seconds: int = 300,
        max_uses: int = 1,
        approval_timeout_seconds: int = 300,
        request_timeout_seconds: float = 120.0,
    ) -> KeychuteHttpResponse:
        """Make one credential-injected HTTP call through Keychute."""
        request_body = body.encode() if isinstance(body, str) else body
        args = [
            self._config.executable,
            "curl",
            url,
            "--include",
            f"--secret={secret_name}",
            f"--request={method}",
            f"--reason={reason}",
            f"--ttl={ttl_seconds}",
            f"--max-uses={max_uses}",
            f"--timeout={approval_timeout_seconds}",
            f"--max-time={request_timeout_seconds}",
        ]
        if headers is not None:
            args.extend(f"--header={name}: {value}" for name, value in headers.items())
        if request_body is not None:
            args.append("--data-binary=@-")

        environment = os.environ.copy()
        environment["KEYCHUTE_CONTEXT"] = json.dumps({"script": self._script_source})
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=(
                    asyncio.subprocess.PIPE
                    if request_body is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except OSError as exc:
            raise KeychuteScriptError(
                f"Cannot start Keychute executable {self._config.executable!r}"
            ) from exc

        total_timeout = (
            approval_timeout_seconds
            + max(request_timeout_seconds, 0)
            + _PROCESS_EXIT_SLACK_SECONDS
        )
        try:
            return_code, stdout, stderr = await asyncio.wait_for(
                _collect_process_output(
                    process,
                    body=request_body,
                    stdout_limit=(
                        self._config.max_response_bytes + _MAX_RESPONSE_HEAD_BYTES
                    ),
                ),
                timeout=total_timeout,
            )
        except TimeoutError as exc:
            raise KeychuteScriptError(
                f"Keychute call exceeded its {total_timeout:g}-second process timeout"
            ) from exc

        if return_code != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            message = f"Keychute call failed with exit code {return_code}"
            if detail:
                message += f": {detail}"
            raise KeychuteScriptError(message)

        response = _parse_response(stdout)
        if len(response["body"]) > self._config.max_response_bytes:
            raise KeychuteScriptError(
                "Keychute response body exceeded the "
                f"{self._config.max_response_bytes}-byte limit"
            )
        if (
            self._execution_context is not None
            and self._execution_context.taint_tracker is not None
        ):
            self._execution_context.taint_tracker.add_source(
                TaintSource(
                    source_type=TaintSourceType.TOOL_OUTPUT,
                    source_id="keychute_http_request",
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason="A Keychute-brokered HTTP response is untrusted external content.",
                )
            )
        return response


def add_keychute_http_api(
    # ast-grep-ignore: no-dict-any - arbitrary values form the script namespace
    globals_dict: dict[str, object] | None,
    *,
    config: KeychuteConfig,
    script_source: str,
    execution_context: ToolExecutionContext | None = None,
) -> dict[str, object] | None:
    """Add the brokered HTTP function when the operator enabled Keychute."""
    if not config.enabled:
        return globals_dict
    result = dict(globals_dict or {})
    result["keychute_http_request"] = KeychuteScriptHttpClient(
        config, script_source, execution_context
    ).request
    return result
