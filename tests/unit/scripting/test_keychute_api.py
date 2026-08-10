"""Tests for the Keychute-backed Monty HTTP API."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from family_assistant.config_models import KeychuteConfig
from family_assistant.scripting.apis.keychute import (
    KeychuteScriptError,
    KeychuteScriptHttpClient,
    add_keychute_http_api,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
)
from family_assistant.tools.types import ToolExecutionContext


def _write_fake_keychute(path: Path, *, exit_code: int = 0) -> None:
    source = f"""#!/usr/bin/env python3
import json
import os
import sys

request_body = sys.stdin.buffer.read()
if {exit_code}:
    sys.stderr.write("request denied")
    raise SystemExit({exit_code})
payload = json.dumps({{
    "args": sys.argv[1:],
    "body": request_body.decode(),
    "context": json.loads(os.environ["KEYCHUTE_CONTEXT"]),
}}).encode()
sys.stdout.buffer.write(
    b"HTTP/1.1 201 Created\\r\\nX-Test: first\\r\\nX-Test: second\\r\\n\\r\\n"
    + payload
)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.asyncio
async def test_request_invokes_cli_and_preserves_response(tmp_path: Path) -> None:
    executable = tmp_path / "keychute"
    _write_fake_keychute(executable)
    script_source = (
        'result = keychute_http_request("weather", "https://example.test/x")'
    )
    tracker = InMemoryTurnTaintTracker()
    execution_context = Mock(spec=ToolExecutionContext)
    execution_context.taint_tracker = tracker
    client = KeychuteScriptHttpClient(
        KeychuteConfig(enabled=True, executable=str(executable)),
        script_source,
        execution_context,
    )

    response = await client.request(
        "weather",
        "https://example.test/x?q=1",
        method="POST",
        headers={"Content-Type": "application/json"},
        body='{"city":"Melbourne"}',
        reason="forecast",
        ttl_seconds=60,
        max_uses=2,
        approval_timeout_seconds=10,
        request_timeout_seconds=5,
    )

    assert response["status_code"] == 201
    assert response["headers"]["x-test"] == ["first", "second"]
    payload = json.loads(response["body"])
    assert payload["body"] == '{"city":"Melbourne"}'
    assert payload["context"] == {"script": script_source}
    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert payload["args"] == [
        "curl",
        "https://example.test/x?q=1",
        "--include",
        "--secret=weather",
        "--request=POST",
        "--reason=forecast",
        "--ttl=60",
        "--max-uses=2",
        "--timeout=10",
        "--max-time=5",
        "--header=Content-Type: application/json",
        "--data-binary=@-",
    ]


@pytest.mark.asyncio
async def test_request_surfaces_keychute_failure(tmp_path: Path) -> None:
    executable = tmp_path / "keychute"
    _write_fake_keychute(executable, exit_code=3)
    client = KeychuteScriptHttpClient(
        KeychuteConfig(enabled=True, executable=str(executable)),
        "keychute_http_request(...)",
    )

    with pytest.raises(KeychuteScriptError, match="exit code 3: request denied"):
        await client.request("weather", "https://example.test/x")


@pytest.mark.asyncio
async def test_request_enforces_response_body_limit(tmp_path: Path) -> None:
    executable = tmp_path / "keychute"
    _write_fake_keychute(executable)
    client = KeychuteScriptHttpClient(
        KeychuteConfig(
            enabled=True,
            executable=str(executable),
            max_response_bytes=3,
        ),
        "keychute_http_request(...)",
    )

    with pytest.raises(KeychuteScriptError, match="response body exceeded"):
        await client.request("weather", "https://example.test/x")


def test_api_is_added_only_when_enabled() -> None:
    original: dict[str, object] = {"value": 1}

    assert (
        add_keychute_http_api(
            original,
            config=KeychuteConfig(enabled=False),
            script_source="value",
        )
        is original
    )
    enabled = add_keychute_http_api(
        original,
        config=KeychuteConfig(enabled=True),
        script_source="value",
    )
    assert enabled is not None
    assert callable(enabled["keychute_http_request"])
