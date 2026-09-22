"""Render settled authenticated-site outcomes for inline and background delivery."""

from typing import cast

from family_assistant.storage.delegation_runs import (
    PARKED_AUTHENTICATED_STATUSES,
    AuthenticatedSiteEnvelope,
)
from family_assistant.tools.types import ToolResult


def authenticated_site_result(
    display_name: str,
    envelope: AuthenticatedSiteEnvelope,
    *,
    delegation_id: str | None,
) -> ToolResult:
    """Render a typed outcome for the caller, keeping browser provenance.

    The summary and any evidence in here came off a page, so they stay ordinary
    untrusted tool output: the tool result carries no trust claim beyond the
    status, which orchestration decided.
    """
    status = envelope["status"]
    lines = [f"{display_name}: {status}."]
    summary = envelope.get("summary")
    if summary:
        lines.append(str(summary))
    detail = envelope.get("detail")
    if detail:
        lines.append(str(detail))
    handoff_url = envelope.get("handoff_url")
    if handoff_url:
        lines.append(f"Take over the browser here: {handoff_url}")
    if status in PARKED_AUTHENTICATED_STATUSES and delegation_id:
        lines.append(
            f"When that is done, call this tool again with resume="
            f"{delegation_id!r} to carry on."
        )
    data = cast("dict[str, object]", dict(envelope))
    if delegation_id:
        data["resume"] = delegation_id
    return ToolResult(text="\n".join(lines), data=data)
