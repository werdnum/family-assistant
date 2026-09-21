"""Credential autofill inside an authenticated-site browser session.

``browser_autofill`` is a fill primitive, not a login engine: the agent
navigates to the form, asks for the fill, clicks Sign in and looks at what
happened. The credential is chosen by the session's pinned alias, released (or
refused) by Keychute against the origin of the document actually on screen, and
written into the page by browser-server. It never crosses into this process, so
no argument, result, log line or exception here can carry it.

See docs/design/authenticated-site-capabilities.md, "Keychute credential
autofill", and the shared wire contract with browser-server.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING

from family_assistant.tools.browser_backend import (
    BrowserBackendError,
    RemoteBrowserBackend,
    resolve_authenticated_binding,
)
from family_assistant.tools.browser_session import browser_operation
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.browser_backend import (
        AuthenticatedSessionBinding,
        JsonDict,
    )
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

__all__ = [
    "BROWSER_AUTOFILL_TOOLS_DEFINITION",
    "browser_autofill_tool",
    "browser_report_login_outcome_tool",
]

_REF_SYNTAX = re.compile(r"e[0-9]+")

# How long browser-server may long-poll Keychute before answering
# `approval_pending`. Well inside a tool call's budget: a decision that takes
# longer parks the run instead of holding the worker.
_AUTOFILL_WAIT_SECONDS = 25

_REFUSAL_GUIDANCE: dict[str, str] = {
    "no_alias": (
        "This site has no stored credential, so there is nothing to fill. Ask "
        "the household to sign in themselves."
    ),
    "bad_password_recorded": (
        "A wrong password was already recorded for this session, so no further "
        "fill will be attempted. Stop here and report that the stored password "
        "needs correcting."
    ),
    "fill_cap_reached": (
        "This session has already used its fill budget. Stop here rather than retrying."
    ),
    "policy_denied": (
        "The household's credential policy refused this release. Stop here and say so."
    ),
    "request_expired": "The release request expired before it was decided.",
    "no_eligible_field": (
        "No field on this page can take the credential. Check you are on the "
        "login form, and re-snapshot before trying again."
    ),
    "ambiguous_fields": (
        "More than one candidate field is on this page, so nothing was filled. "
        "Name the field explicitly with field_refs."
    ),
    "new_password_field": (
        "That field is for setting a new password, not signing in. This is not "
        "a login form."
    ),
    "in_iframe": (
        "The field is inside an iframe, which cannot be filled. Treat this as a "
        "step the household has to complete themselves."
    ),
    "wrong_origin": (
        "The page you are on is not an origin this credential may be entered "
        "on. Do not enter anything by hand; stop and report it."
    ),
    "target_invalidated": (
        "The page changed while the fill was being prepared. Re-snapshot and "
        "ask again if you are still on the login form."
    ),
    "keychute_unavailable": (
        "The credential service is unreachable, so no fill is possible now. "
        "Stop rather than retrying; nothing here will make it answer."
    ),
    "grant_invalid": (
        "The stored credential does not have the part you asked for. Stop here "
        "and report which part is missing, so it can be corrected."
    ),
}


class AutofillUnavailableError(BrowserBackendError):
    """This turn is not running inside an authenticated-site session.

    Autofill exists only on a session whose alias trusted orchestration pinned
    at creation. A turn with no such binding has nothing to ask for, and must
    not fall back to the conversation's ordinary browser session.
    """


async def _require_authenticated_backend(
    exec_context: ToolExecutionContext,
) -> tuple[AuthenticatedSessionBinding, RemoteBrowserBackend]:
    binding = await resolve_authenticated_binding(exec_context)
    if binding is None or not isinstance(binding.backend, RemoteBrowserBackend):
        raise AutofillUnavailableError(
            "This browser session is not an authenticated-site session, so it "
            "has no credential to fill."
        )
    return binding, binding.backend


def _step_key(
    binding: AuthenticatedSessionBinding,
    *,
    kind: str | None,
    field_refs: list[str] | None,
) -> str:
    """The idempotency key for this fill step.

    One key per distinct step of the login, reused across `approval_pending`
    retries of that step so Keychute replays the same request rather than
    opening a second one. A different step -- the password after the identifier
    -- is a different signature and so a different key.
    """
    signature = f"{kind or 'auto'}:{','.join(sorted(field_refs or ()))}"
    existing = binding.step_keys.get(signature)
    if existing is not None:
        return existing
    step_key = f"{kind or 'auto'}-{uuid.uuid4().hex}"
    binding.step_keys[signature] = step_key
    return step_key


def _validate_refs(field_refs: list[str] | None) -> None:
    for ref in field_refs or ():
        if not _REF_SYNTAX.fullmatch(ref):
            raise ValueError(
                f"Invalid ref {ref!r}. Refs look like 'e12' and come from a "
                "snapshot; pass one exactly as the snapshot listed it."
            )


def _filled_result(response: JsonDict) -> ToolResult:
    filled = response.get("filled")
    # A `ref` may be null for a field browser-server auto-detected and that was
    # never in a snapshot, so the kind is what is named back to the model.
    kinds = (
        ", ".join(
            str(entry.get("kind", "field"))
            for entry in filled
            if isinstance(entry, dict)
        )
        if isinstance(filled, list)
        else ""
    )
    origin = response.get("origin")
    return ToolResult(
        text=(
            f"Filled the stored {kinds or 'credential'} into the form on "
            f"{origin}. Submit the form yourself and check the result. The "
            "value is not shown to you and cannot be read back off the page."
        ),
        data={"status": "filled", "origin": origin, "filled": filled},
    )


def _approval_pending_result(response: JsonDict) -> ToolResult:
    detail = response.get("detail") or ""
    return ToolResult(
        text=(
            "The household has to approve releasing this credential before it "
            f"can be filled. {detail} Stop here and report that approval is "
            "pending; the task can be resumed once they decide."
        ).strip(),
        data={
            "status": "approval_pending",
            "request_id": response.get("request_id"),
            "origin": response.get("origin"),
        },
    )


def _refused_result(response: JsonDict) -> ToolResult:
    reason = str(response.get("reason") or "refused")
    detail = str(response.get("detail") or "")
    guidance = _REFUSAL_GUIDANCE.get(reason, "Do not try to work around this.")
    return ToolResult(
        text=f"Autofill was refused ({reason}). {detail} {guidance}".strip(),
        data={"status": "refused", "reason": reason, "detail": detail},
    )


@browser_operation()
async def browser_autofill_tool(
    exec_context: ToolExecutionContext,
    field_refs: list[str] | None = None,
    kind: str | None = None,
) -> ToolResult:
    """Fill this site's stored credential into the login form on the page."""
    if kind is not None and kind not in {"username", "password"}:
        raise ValueError(
            f"kind must be 'username' or 'password', not {kind!r}; omit it to "
            "let the login form decide."
        )
    _validate_refs(field_refs)
    binding, backend = await _require_authenticated_backend(exec_context)
    fields: list[JsonDict] | None = None
    if field_refs:
        fields = [
            {"ref": ref, **({"kind": kind} if kind is not None else {})}
            for ref in field_refs
        ]
    elif kind is not None:
        fields = [{"kind": kind}]
    step_key = _step_key(binding, kind=kind, field_refs=field_refs)
    logger.info(
        "browser_autofill: site=%s step=%s kind=%s refs=%s",
        binding.site_id,
        step_key,
        kind,
        field_refs,
    )
    response = await backend.autofill(
        step_key=step_key,
        fields=fields,
        wait_seconds=_AUTOFILL_WAIT_SECONDS,
        context={
            "site": binding.site_id,
            "acting_user": exec_context.user_name,
        },
    )
    status = response.get("status")
    if status != "approval_pending":
        binding.approval_pending_request_id = None
        signature = f"{kind or 'auto'}:{','.join(sorted(field_refs or ()))}"
        binding.step_keys.pop(signature, None)
    if status == "filled":
        return _filled_result(response)
    if status == "approval_pending":
        request_id = response.get("request_id")
        binding.approval_pending_request_id = (
            str(request_id) if request_id is not None else step_key
        )
        return _approval_pending_result(response)
    if response.get("reason") == "bad_password_recorded":
        binding.bad_password_recorded = True
    return _refused_result(response)


@browser_operation()
async def browser_report_login_outcome_tool(
    exec_context: ToolExecutionContext,
    outcome: str,
) -> ToolResult:
    """Record that the stored credential did not work, and stop further fills."""
    if outcome != "bad_password":
        raise ValueError(
            f"outcome must be 'bad_password', not {outcome!r}. There is nothing "
            "to report when the login worked."
        )
    binding, backend = await _require_authenticated_backend(exec_context)
    logger.info("browser_report_login_outcome: site=%s", binding.site_id)
    await backend.report_autofill_outcome(outcome)
    binding.bad_password_recorded = True
    return ToolResult(
        text=(
            "Recorded that the stored password was rejected. No further fill "
            "will be attempted in this session. Stop here and report that the "
            "household needs to correct the stored password — nobody can fix "
            "that from inside this session."
        ),
        data={"status": "bad_password_recorded"},
    )


BROWSER_AUTOFILL_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "browser_autofill",
            "description": (
                "Fill this site's stored credential into the login form in "
                "front of you. It fills; you drive the login — navigate to the "
                "form, call this, then submit the form yourself and check what "
                "happened. A username-first login is two calls: the identifier, "
                "then the password on the next page. You never see the value "
                "and cannot read it back off the page. Returns filled, "
                "approval_pending (the household must approve the release — "
                "stop and say so), or refused with a reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional refs of the fields to fill, from a "
                            "snapshot. Omit to let the login form be detected."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["username", "password"],
                        "description": (
                            "Which half of the credential to fill. Omit on a "
                            "single-page form that shows both."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_report_login_outcome",
            "description": (
                "Report that the site rejected the stored password. Call this "
                "once, when the page clearly says the credentials are wrong, "
                "and then stop: no one can correct a stored password from "
                "inside this session. Do not guess at another password."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": ["bad_password"],
                        "description": "The only reportable outcome.",
                    }
                },
                "required": ["outcome"],
            },
        },
    },
]
