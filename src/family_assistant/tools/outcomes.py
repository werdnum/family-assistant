"""What a finished tool call amounted to, as shown to the user.

Clients render a tool call as succeeded, failed or not run. A tool result is a
string the model reads, and most tools report failure in that string rather
than by raising, so the outcome is read from the result here, once, and handed
to every client on both the live stream and the history API. Clients must not
re-derive it from the text.

Recognising failure is best effort: a tool that reports failure in prose this
module does not know, with no structured error and no exception, still reads
as succeeded. Tools should signal failure with an "Error:" text, a structured
``error``, or by raising.
"""

from __future__ import annotations

import json
from typing import Literal

# "rejected" means the tool did not run: a confirmation was declined, cancelled
# or timed out, or the call was deferred to an approval outside the turn.
ToolOutcome = Literal["succeeded", "failed", "rejected"]

# Results written when a confirmation gate stops a tool before it runs. They
# are built from these prefixes so the classifier below recognises every one.
ACTION_CANCELLED_PREFIX = "Action cancelled:"
ACTION_DECLINED_PREFIX = "OK. Action cancelled by user"
# Said by a result that handed the call to a durable confirmation instead of
# running it: the turn ends with the call not run, waiting outside the turn.
NOT_RUN_YET_NOTE = "It hasn't run yet"

# The tools' own convention for a failure returned as text: "Error: ...",
# "Error executing ...", "Error during ...".
_ERROR_TEXT_PREFIX = "Error"


def classify_tool_outcome(content: object, error_traceback: str | None) -> ToolOutcome:
    """Classify a terminal tool result.

    ``error_traceback`` is set (possibly empty) when execution failed, or when
    a tool's structured data reported a failure (see ``is_error_data``). A gate
    that stopped the tool also records one, so a not-run result is recognised
    first.
    """
    text = content.strip() if isinstance(content, str) else ""
    if text.startswith((ACTION_CANCELLED_PREFIX, ACTION_DECLINED_PREFIX)):
        return "rejected"
    if NOT_RUN_YET_NOTE in text and error_traceback is None:
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
    return is_error_data(payload)


def is_error_data(data: object) -> bool:
    """Whether a tool's structured result reports a failure.

    Tools that pair readable text ("Download failed: ...") with structured
    data carry the failure in the data, which the text classifier never sees.
    """
    if not isinstance(data, dict):
        return False
    status = data.get("status")
    return (
        bool(data.get("error"))
        or data.get("success") is False
        or (isinstance(status, str) and status in {"error", "failed"})
    )
