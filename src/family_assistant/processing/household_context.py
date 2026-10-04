"""Where the context providers' output goes.

The providers left -- the household's always-loaded notes, its skills catalog,
its known users -- change only when someone writes a note or edits the config,
so their output belongs in the system prompt: it is rebuilt every request but
comes out byte-identical until one of those writes, which keeps it inside the
cached prefix and leaves earlier thinking blocks valid. Anything that changes
from one request to the next is reached through a tool instead. See
docs/design/append-only-prompt.md.

The Live API paths (voice, telephony) build one system instruction per session,
so they render the same section there along with the session's start time.
"""

from __future__ import annotations

HOUSEHOLD_CONTEXT_HEADING = "# Household context"

_HOUSEHOLD_CONTEXT_PREAMBLE = (
    "Loaded from the household's notes and configuration, current as of this "
    "request. It does not include calendar events, weather or home state."
)


def render_household_context_section(aggregated_context: str) -> str:
    """Render the providers' output as a system prompt section, or "" if empty."""
    body = aggregated_context.strip()
    if not body:
        return ""
    return f"{HOUSEHOLD_CONTEXT_HEADING}\n{_HOUSEHOLD_CONTEXT_PREAMBLE}\n\n{body}"


def render_live_session_context(
    *,
    current_time_str: str,
    aggregated_context: str,
) -> str:
    """The context a Live API session's system instruction carries.

    A Live session has no message history to stamp, so the time it started is
    stated here instead.
    """
    sections = [f"Session started at: {current_time_str}"]
    household = render_household_context_section(aggregated_context)
    if household:
        sections.append(household)
    return "\n\n".join(sections)
