"""What a finished tool call amounted to, as shown to the user.

Clients render a tool call as succeeded, failed or not run. A tool result is a
string the model reads, and most tools report failure in that string rather
than by raising, so the outcome is read from the result here, once, and handed
to every client on both the live stream and the history API. Clients must not
re-derive it from the text.
"""

from __future__ import annotations

import json
from typing import Literal

ToolOutcome = Literal["succeeded", "failed", "rejected"]

# Results written when a confirmation gate stops a tool before it runs. They
# are built from these prefixes so the classifier below recognises every one.
ACTION_CANCELLED_PREFIX = "Action cancelled:"
ACTION_DECLINED_PREFIX = "OK. Action cancelled by user"

# The tools' own convention for a failure returned as text: "Error: ...",
# "Error executing ...", "Error during ...".
_ERROR_TEXT_PREFIX = "Error"


def classify_tool_outcome(content: object, error_traceback: str | None) -> ToolOutcome:
    """Classify a terminal tool result.

    ``error_traceback`` is set (possibly empty) when execution itself failed.
    A gate that stopped the tool also records one, so a not-run result is
    recognised first.
    """
    text = content.strip() if isinstance(content, str) else ""
    if text.startswith((ACTION_CANCELLED_PREFIX, ACTION_DECLINED_PREFIX)):
        return "rejected"
    if error_traceback is not None:
        return "failed"
    if _is_error_text(text) or _is_error_payload(text):
        return "failed"
    return "succeeded"


def _is_error_text(text: str) -> bool:
    if not text.startswith(_ERROR_TEXT_PREFIX):
        return False
    rest = text[len(_ERROR_TEXT_PREFIX) :]
    return not rest or not rest[0].isalnum()


def _is_error_payload(text: str) -> bool:
    if not text.startswith("{"):
        return False
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    return (
        bool(payload.get("error"))
        or payload.get("success") is False
        or payload.get("status") == "error"
    )
