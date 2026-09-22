"""Request Keychute credentials for the current browser page.

The agent names a secret; Keychute authorizes release against the actual
origin checked by browser-server. Plaintext goes directly to the protected
browser, never through Family Assistant. Configured sites remain optional
presets whose account binding cannot be overridden.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from family_assistant.tools.browser_backend import (
    BrowserBackendError,
    RemoteBrowserBackend,
    get_browser_backend,
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
        "No credential was selected. Ask the user for the Keychute secret name, "
        "never the password itself."
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
    """Autofill requires a remote browser with credential protection."""


async def _autofill_backend(
    exec_context: ToolExecutionContext,
) -> tuple[AuthenticatedSessionBinding | None, RemoteBrowserBackend]:
    binding = await resolve_authenticated_binding(exec_context)
    backend = await get_browser_backend(exec_context)
    if not isinstance(backend, RemoteBrowserBackend):
        raise AutofillUnavailableError(
            "Autofill requires browser-server with Keychute configured; "
            "the local browser cannot request credentials."
        )
    if not backend.autofill_enabled:
        raise AutofillUnavailableError(
            "This is an ordinary browser session. Delegate the login task to "
            "credential_browser_profile with the URL and Keychute secret name; "
            "it uses a separate credential-protected browser."
        )
    return binding, backend


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
            "value is not included in this result. Continue using protected snapshots."
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
    secret_name: str | None = None,
    kind: str | None = None,
) -> ToolResult:
    """Request the named Keychute secret for the current login form."""
    if kind is not None and kind not in {"username", "password"}:
        raise ValueError(
            f"kind must be 'username' or 'password', not {kind!r}; omit it to "
            "let the login form decide."
        )
    _validate_refs(field_refs)
    binding, backend = await _autofill_backend(exec_context)
    if binding is None and not secret_name:
        raise ValueError(
            "Name the Keychute secret with secret_name. If you do not know "
            "which secret to use, ask the user for its name, never its value."
        )
    fields: list[JsonDict] | None = None
    if field_refs:
        fields = [
            {"ref": ref, **({"kind": kind} if kind is not None else {})}
            for ref in field_refs
        ]
    elif kind is not None:
        fields = [{"kind": kind}]
    step_keys = binding.step_keys if binding is not None else backend.autofill_step_keys
    signature = (
        f"{secret_name or ''}:{backend.current_url}:"
        f"{kind or 'auto'}:{','.join(sorted(field_refs or ()))}"
    )
    step_key = step_keys.setdefault(signature, f"{kind or 'auto'}-{uuid.uuid4().hex}")
    response = await backend.autofill(
        step_key=step_key,
        secret_name=secret_name,
        fields=fields,
        wait_seconds=_AUTOFILL_WAIT_SECONDS,
        context={
            "site": binding.site_id if binding is not None else secret_name,
            "acting_user": exec_context.user_name,
        },
    )
    status = response.get("status")
    if status != "approval_pending":
        step_keys.pop(signature, None)
    if binding is not None:
        binding.approval_pending_request_id = (
            str(response.get("request_id") or step_key)
            if status == "approval_pending"
            else None
        )
        binding.autofill_refusal = (
            str(response.get("reason") or "refused") if status == "refused" else None
        )
        if response.get("reason") == "bad_password_recorded":
            binding.bad_password_recorded = True
    if status == "filled":
        return _filled_result(response)
    if status == "approval_pending":
        return _approval_pending_result(response)
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
    binding, backend = await _autofill_backend(exec_context)
    if binding is not None:
        binding.bad_password_recorded = True
    await backend.report_autofill_outcome(outcome)
    if binding is None:
        await backend.discard_session()
        return ToolResult(
            text=(
                "Recorded that the stored password was rejected and discarded "
                "this browser session. Stop and ask the user to correct the "
                "stored password. When they ask to try again, browser_open "
                "starts a fresh session in this conversation."
            ),
            data={"status": "bad_password_recorded", "session_discarded": True},
        )
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
                "Request a Keychute secret by name and fill it into the login form in "
                "front of you. It fills; you drive the login — navigate to the "
                "form, call this, then submit the form yourself and check what "
                "happened. A username-first login is two calls: the identifier, "
                "then the password on the next page. You never see the value "
                "in tool results. Keychute approves release for the actual page origin. "
                "No configured site or standing grant is required. If you encounter "
                "a login wall, request the secret the user named, or ask for its "
                "name (never its value). Returns filled, "
                "approval_pending (the household must approve the release — "
                "stop and say so), or refused with a reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "secret_name": {
                        "type": "string",
                        "description": (
                            "Keychute secret name supplied by the user. Required for "
                            "the credential browser profile; optional for a configured site's pinned login. "
                            "This requests access, it does not grant it."
                        ),
                    },
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
