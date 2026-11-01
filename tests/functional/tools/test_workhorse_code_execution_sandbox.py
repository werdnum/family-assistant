"""The workhorse sandbox's confined code-execution server launcher.

The deployment's server source is not in this repository, so a stand-in
``main.py`` with the same entry point exercises the launcher and, above all,
the confinement: the process may write its own working directory and nothing
of the user's.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import httpx
import pytest

from family_assistant.eval.workhorse.code_execution_sandbox import (
    code_execution_server,
)

pytestmark = [
    pytest.mark.no_db,
    pytest.mark.skipif(
        sys.platform != "darwin" or shutil.which("uv") is None,
        reason="the sandbox confines the server with macOS sandbox-exec and runs it with uv",
    ),
]

_STAND_IN = """
import os
import pathlib

from fastapi import FastAPI

app = FastAPI()


@app.get("/mcp/sse")
def probe(path: str) -> dict[str, str]:
    try:
        pathlib.Path(path).write_text("probe")
    except OSError as error:
        return {"result": f"denied: {error.strerror}"}
    return {"result": "written"}


@app.get("/workspace")
def workspace() -> dict[str, str]:
    return {"base": os.environ["WORKSPACE_BASE"]}
"""


def test_the_server_writes_its_workspace_and_nothing_of_the_users(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text(_STAND_IN, encoding="utf-8")
    (source / "mcp_server.py").write_text("", encoding="utf-8")
    outside = Path.home() / ".workhorse-sandbox-confinement-probe"

    with code_execution_server(
        source, tmp_path / "work", cache_dir=tmp_path / "cache"
    ) as server:
        base = server.sse_url.removesuffix("/mcp/sse")
        inside = server.workspace_base / "probe.txt"
        allowed = httpx.get(server.sse_url, params={"path": str(inside)}).json()
        refused = httpx.get(server.sse_url, params={"path": str(outside)}).json()
        workspace = httpx.get(f"{base}/workspace").json()

    assert allowed == {"result": "written"}
    assert refused["result"].startswith("denied")
    assert not outside.exists()
    assert workspace == {"base": str(server.workspace_base)}
