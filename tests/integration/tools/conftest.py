"""Pytest configuration for browser-server integration tests.

Inserts the sibling browser-server repo into sys.path so that
``browser_handoff_service`` can be imported from the family-assistant venv.
If the sibling repo is absent the entire tools integration suite is skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BROWSER_SERVER_ROOT = Path(__file__).parents[4] / "browser-server"

if not _BROWSER_SERVER_ROOT.exists():
    pytest.skip(
        "browser-server sibling repo not found; skipping browser integration tests",
        allow_module_level=True,
    )

if str(_BROWSER_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_BROWSER_SERVER_ROOT))
