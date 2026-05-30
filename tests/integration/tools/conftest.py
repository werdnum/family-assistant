"""Pytest configuration for browser-server integration tests.

Inserts the sibling browser-server repo into sys.path when present so that
``browser_handoff_service`` can be imported from the family-assistant venv.
When the sibling repo is absent the directory is a no-op here; individual
test modules guard themselves with ``pytest.importorskip``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BROWSER_SERVER_ROOT = Path(__file__).parents[4] / "browser-server"

if _BROWSER_SERVER_ROOT.exists() and str(_BROWSER_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_BROWSER_SERVER_ROOT))

