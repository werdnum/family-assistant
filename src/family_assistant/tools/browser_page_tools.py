"""Which browser tools report the page they leave the session on.

Kept apart from ``browser_dom`` so the tool-call reviewer can read it without
importing the browser backend.
"""

from __future__ import annotations

# Tools whose result carries a snapshot of the page as they left it. When a
# batch holds more than one, only the last hands back refs -- the earlier pages
# have moved on, so their refs would fail.
SNAPSHOT_RETURNING_TOOLS = frozenset({
    "browser_claim_handback",
    "browser_click",
    "browser_fill",
    "browser_open",
    "browser_select",
    "browser_snapshot",
    "browser_wait",
})

# Runs page script, which can navigate; its result names the URL it left.
BROWSER_EXEC_TOOL = "browser_exec"

# A successful handback's text: this prefix, then the URL the session is at.
HANDBACK_TOOL = "browser_claim_handback"
HANDBACK_SUCCESS_PREFIX = "Session reclaimed"
HANDBACK_URL_PREFIX = "Session reclaimed. Now at: "
