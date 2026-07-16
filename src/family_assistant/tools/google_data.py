"""Read-only Gmail/Drive tools that act strictly as the turn's acting user.

Every tool resolves a per-user Google access token from the execution context
(never from tool arguments), so the LLM cannot address another user's mailbox or
Drive — see ``docs/design/user-scoped-google-data-access.md`` §3. The tools call
Google's REST APIs through an injectable :class:`ApiBackend` seam and taint
their results as ``unknown_external`` via tool metadata tags. Attachments they
register are owned by the acting user so the registry enforces cross-user
isolation on every later access.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import mimetypes
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from markdownify import markdownify

from family_assistant.services.google_provider import GOOGLE_PROVIDER, GoogleScope
from family_assistant.services.oauth_credentials import (
    OAuthCredentialError,
    OAuthScopeNotGrantedError,
)
from family_assistant.tools.types import ToolAttachment, ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from family_assistant.services.api_backend import ApiResponse
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)

# Google REST JSON has an open, endpoint-specific shape; the tools narrow it with
# ``.get(...)`` and explicit ``isinstance`` checks at each use site.
# ast-grep-ignore: no-dict-any - Free-form Google REST JSON with arbitrary keys.
type GoogleJson = dict[str, Any]

_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

# Result-size bounds so a hostile mailbox/Drive cannot blow out the context.
_GMAIL_SEARCH_CAP = 25
_DRIVE_SEARCH_CAP = 25
_MESSAGE_BODY_CHAR_LIMIT = 50_000
# Cap on HTML fed to the HTML->text conversion; the rendered body is truncated
# to _MESSAGE_BODY_CHAR_LIMIT afterwards, so converting more is pure waste.
_HTML_CONVERSION_CHAR_LIMIT = 400_000
# Drive files below this size are pulled inline as text; larger go to attachments.
_DRIVE_INLINE_TEXT_LIMIT = 200 * 1024
# Hard ceiling on a Drive download. When the metadata already reports a larger
# ``size`` we refuse without issuing the ``alt=media`` request, so a multi-gigabyte
# file can never be materialized in memory. This mirrors the backend's own
# response-body cap (``HttpApiBackend.max_response_bytes``), which is the
# defense for Google-native exports that report no size.
_DRIVE_DOWNLOAD_LIMIT = 25 * 1024 * 1024

# Google-native mime types and the export format we fetch them as.
_GOOGLE_DOC_EXPORTS: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# Scopes that must be CONFIGURED (deployment-wide) for each tool to register. A
# later agent uses this for scope-conditional registration gating.
GOOGLE_TOOL_REQUIRED_SCOPES: dict[str, frozenset[str]] = {
    "gmail_search": frozenset({GoogleScope.GMAIL_READONLY.value}),
    "gmail_get_message": frozenset({GoogleScope.GMAIL_READONLY.value}),
    "gmail_get_attachment": frozenset({GoogleScope.GMAIL_READONLY.value}),
    "drive_search": frozenset({
        GoogleScope.DRIVE_READONLY.value,
        GoogleScope.DRIVE_METADATA_READONLY.value,
    }),
    "drive_get_file": frozenset({GoogleScope.DRIVE_READONLY.value}),
}


GOOGLE_DATA_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "gmail_search",
            "description": (
                "Search the requesting user's own Gmail mailbox and return matching "
                "messages (id, thread id, date, sender, recipients, subject, and a "
                "short snippet). You act as the user's own Google account, so you see "
                "only their mail. Use standard Gmail search syntax in `query`, e.g. "
                "`from:school subject:excursion`, `has:attachment newer_than:7d`, "
                "`in:inbox is:unread`. Follow up with gmail_get_message for the full "
                "body of a specific result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Gmail search query using Gmail's search operators."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Maximum number of messages to return "
                            f"(default 10, capped at {_GMAIL_SEARCH_CAP})."
                        ),
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_get_message",
            "description": (
                "Fetch one full Gmail message from the requesting user's own mailbox "
                "by its id (as returned by gmail_search). Returns the parsed headers, "
                "the plain-text body (HTML is converted to text; long bodies are "
                "truncated with a marker), and a list of attachment metadata "
                "(attachment_id, filename, mime type, size). Use gmail_get_attachment "
                "to download a specific attachment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The Gmail message id to fetch.",
                    },
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_get_attachment",
            "description": (
                "Download one attachment from a message in the requesting user's own "
                "mailbox and store it as an attachment you can then attach to your "
                "reply or read back. Provide the message_id and the attachment_id from "
                "gmail_get_message. The stored file is private to the requesting user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": (
                            "The Gmail message id the attachment belongs to."
                        ),
                    },
                    "attachment_id": {
                        "type": "string",
                        "description": (
                            "The attachment id from gmail_get_message's attachments "
                            "list."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "Optional display name for the attachment; the file "
                            "is stored under the name and type Gmail reports."
                        ),
                    },
                },
                "required": ["message_id", "attachment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drive_search",
            "description": (
                "Search the requesting user's own Google Drive and return matching "
                "files (id, name, mime type, owner, last-modified time, web view "
                "link). You act as the user's own Google account. Use Drive query "
                "syntax in `query`, e.g. `name contains 'tax'`, "
                "`mimeType = 'application/pdf'`, `'me' in owners`, "
                "`modifiedTime > '2024-01-01T00:00:00'`. Combine clauses with `and`. "
                "Follow up with drive_get_file to fetch a specific file's content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Drive search query using Drive's query syntax."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Maximum number of files to return "
                            f"(default 10, capped at {_DRIVE_SEARCH_CAP})."
                        ),
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drive_get_file",
            "description": (
                "Fetch a file from the requesting user's own Google Drive by id (from "
                "drive_search). Google Docs/Sheets/Slides are exported to text; small "
                "text files are returned inline (truncated with a marker if long); "
                "binary or large files are stored as a private attachment you can "
                "attach to your reply or read back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "The Drive file id to fetch.",
                    },
                },
                "required": ["file_id"],
            },
        },
    },
]


class _GoogleToolError(Exception):
    """A non-credential failure to be rendered as a tool error message."""


async def _guard(
    impl: Callable[[], Awaitable[ToolResult]],
) -> ToolResult:
    """Run a tool body, rendering credential/API failures as tool errors.

    Credential errors carry actionable messages; ``_GoogleToolError`` carries a
    concise, token-free API failure message.
    """
    try:
        return await impl()
    except (OAuthCredentialError, _GoogleToolError) as exc:
        return ToolResult(text=f"Error: {exc}")


async def _google_request(
    exec_context: ToolExecutionContext,
    scope: GoogleScope,
    *,
    method: str = "GET",
    url: str,
    params: Mapping[str, str] | None = None,
) -> ApiResponse:
    """Issue an authenticated Google REST request for the acting user.

    Resolves the access token from the execution context, calls the injected
    backend, and transparently retries once on a ``401`` after a forced token
    refresh (a revoked-before-expiry token). A second ``401`` propagates as a
    :class:`_GoogleToolError`; if the forced refresh itself fails with
    ``invalid_grant`` the resolver raises ``OAuthReauthRequiredError``, which the
    tool boundary renders directly.
    """
    resolvers = exec_context.credential_resolvers or {}
    resolver = resolvers.get(GOOGLE_PROVIDER.name)
    backend = exec_context.api_backend
    if resolver is None or backend is None:
        raise _GoogleToolError(
            "Google integration is not configured or enabled for this deployment."
        )

    access_token = await resolver.access_token_for(exec_context, scope)
    response = await backend.request(
        method=method, url=url, access_token=access_token, params=params
    )
    if response.status_code == 401:
        if exec_context.user_id is not None:
            resolver.evict_cached_token(exec_context.user_id)
        access_token = await resolver.access_token_for(exec_context, scope)
        response = await backend.request(
            method=method, url=url, access_token=access_token, params=params
        )

    if 200 <= response.status_code < 300:
        if exec_context.user_id is not None:
            await exec_context.db_context.oauth_connections.update_last_used(
                exec_context.user_id, GOOGLE_PROVIDER.name
            )
        return response
    raise _GoogleToolError(_format_api_error(response))


def _format_api_error(response: ApiResponse) -> str:
    """Build a concise, token-free error message from a non-2xx response."""
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                detail = message
        elif isinstance(error, str):
            detail = error
    suffix = f": {detail}" if detail else ""
    return f"Google API request failed (HTTP {response.status_code}){suffix}"


def _decode_base64url(data: str) -> bytes:
    """Decode Gmail/Drive base64url content, tolerating missing padding."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with an explicit marker."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated — {limit} of {len(text)} characters shown]"


async def gmail_search_tool(
    exec_context: ToolExecutionContext,
    query: str,
    max_results: int = 10,
) -> ToolResult:
    """Search the acting user's Gmail mailbox."""

    async def _impl() -> ToolResult:
        capped = max(1, min(max_results, _GMAIL_SEARCH_CAP))
        listing = (
            await _google_request(
                exec_context,
                GoogleScope.GMAIL_READONLY,
                url=f"{_GMAIL_API_BASE}/users/me/messages",
                params={"q": query, "maxResults": str(capped)},
            )
        ).json()
        results = await _fetch_message_summaries(exec_context, listing, capped)
        more_available = bool(listing.get("nextPageToken"))
        # Data-only result: the serialized JSON is what the LLM sees, so the
        # message ids/subjects/snippets must live here, not in a summary text.
        data: GoogleJson = {
            "messages": results,
            "more_results_available": more_available,
        }
        if more_available:
            data["note"] = (
                "More matches exist beyond this page — narrow the query or"
                " raise max_results to see them."
            )
        return ToolResult(data=data)

    return await _guard(_impl)


async def _fetch_message_summaries(
    exec_context: ToolExecutionContext,
    listing: GoogleJson,
    capped: int,
) -> list[GoogleJson]:
    """Fetch metadata-format summaries for each message id in a search listing."""
    message_refs = listing.get("messages") or []
    results: list[GoogleJson] = []
    for ref in message_refs[:capped]:
        message_id = ref.get("id")
        if not isinstance(message_id, str):
            continue
        detail = (
            await _google_request(
                exec_context,
                GoogleScope.GMAIL_READONLY,
                url=f"{_GMAIL_API_BASE}/users/me/messages/{message_id}",
                # metadataHeaders is a repeated query parameter, which the
                # backend's flat params mapping cannot express; format=metadata
                # returns all headers by default and the summarizer picks the
                # four it needs.
                params={"format": "metadata"},
            )
        ).json()
        results.append(_summarize_message(detail))
    return results


def _headers_map(payload: GoogleJson) -> dict[str, str]:
    """Build a case-insensitive header lookup from a Gmail payload."""
    headers = payload.get("payload", {}).get("headers", [])
    result: dict[str, str] = {}
    for header in headers:
        name = header.get("name")
        value = header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name.lower()] = value
    return result


def _summarize_message(message: GoogleJson) -> GoogleJson:
    """Reduce a metadata-format Gmail message to the search summary shape."""
    headers = _headers_map(message)
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "date": headers.get("date"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "subject": headers.get("subject"),
        "snippet": message.get("snippet"),
    }


async def gmail_get_message_tool(
    exec_context: ToolExecutionContext,
    message_id: str,
) -> ToolResult:
    """Fetch and parse one full Gmail message."""

    async def _impl() -> ToolResult:
        message = (
            await _google_request(
                exec_context,
                GoogleScope.GMAIL_READONLY,
                url=f"{_GMAIL_API_BASE}/users/me/messages/{message_id}",
                params={"format": "full"},
            )
        ).json()
        headers = _headers_map(message)
        body_text, attachments = await _walk_message_payload(message.get("payload", {}))
        body = _truncate(body_text, _MESSAGE_BODY_CHAR_LIMIT)
        return ToolResult(
            text=_render_message_text(headers, body, attachments),
            data={
                "id": message.get("id"),
                "thread_id": message.get("threadId"),
                "headers": {
                    "from": headers.get("from"),
                    "to": headers.get("to"),
                    "cc": headers.get("cc"),
                    "subject": headers.get("subject"),
                    "date": headers.get("date"),
                },
                "body": body,
                "attachments": attachments,
            },
        )

    return await _guard(_impl)


def _render_message_text(
    headers: dict[str, str],
    body: str,
    attachments: list[GoogleJson],
) -> str:
    """Human-readable rendering of a fetched message for the LLM."""
    lines = [
        f"From: {headers.get('from', '')}",
        f"To: {headers.get('to', '')}",
        f"Subject: {headers.get('subject', '')}",
        f"Date: {headers.get('date', '')}",
        "",
        body,
    ]
    if attachments:
        # Include the ids the LLM needs to call gmail_get_attachment.
        rendered = ", ".join(
            f"{att.get('filename')} (attachment_id: {att.get('attachment_id')}, "
            f"{att.get('mime_type')}, {att.get('size')} bytes)"
            for att in attachments
        )
        lines.append(f"\nAttachments: {rendered}")
    return "\n".join(lines)


async def _walk_message_payload(payload: GoogleJson) -> tuple[str, list[GoogleJson]]:
    """Extract the best text body and attachment metadata from a MIME payload.

    Prefers a ``text/plain`` part, falling back to converting ``text/html`` to
    text. Parts that carry a filename are collected as attachment metadata.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[GoogleJson] = []
    _collect_parts(payload, plain_parts, html_parts, attachments)

    if plain_parts:
        body = "\n".join(plain_parts).strip()
    elif html_parts:
        # A hostile message can carry megabytes of HTML; cap the conversion
        # input (the rendered body is truncated far smaller anyway) and run the
        # CPU-bound conversion off the event loop.
        joined_html = "\n".join(html_parts)[:_HTML_CONVERSION_CHAR_LIMIT]
        body = (
            await asyncio.to_thread(markdownify, joined_html, heading_style="ATX")
        ).strip()
    else:
        body = ""
    return body, attachments


def _collect_parts(
    part: GoogleJson,
    plain_parts: list[str],
    html_parts: list[str],
    attachments: list[GoogleJson],
) -> None:
    """Recursively gather text bodies and attachment metadata from a part."""
    mime_type = part.get("mimeType", "")
    filename = part.get("filename") or ""
    body = part.get("body", {})

    if filename:
        attachments.append({
            "attachment_id": body.get("attachmentId"),
            "filename": filename,
            "mime_type": mime_type,
            "size": body.get("size"),
        })
    else:
        _accumulate_text_part(mime_type, body, plain_parts, html_parts)

    for child in part.get("parts", []) or []:
        _collect_parts(child, plain_parts, html_parts, attachments)


def _accumulate_text_part(
    mime_type: str,
    body: GoogleJson,
    plain_parts: list[str],
    html_parts: list[str],
) -> None:
    """Decode a text part's body into the plain/html accumulators."""
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return
    try:
        decoded = _decode_base64url(data).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return
    if mime_type == "text/plain":
        plain_parts.append(decoded)
    elif mime_type == "text/html":
        html_parts.append(decoded)


async def gmail_get_attachment_tool(
    exec_context: ToolExecutionContext,
    message_id: str,
    attachment_id: str,
    filename: str | None = None,
) -> ToolResult:
    """Download one Gmail attachment into the attachment registry."""

    async def _impl() -> ToolResult:
        if exec_context.attachment_registry is None:
            return ToolResult(text="Error: Attachment registry not available.")
        payload = (
            await _google_request(
                exec_context,
                GoogleScope.GMAIL_READONLY,
                url=(
                    f"{_GMAIL_API_BASE}/users/me/messages/{message_id}/attachments/"
                    f"{attachment_id}"
                ),
            )
        ).json()
        content = _decode_attachment_payload(payload)
        if content is None:
            return ToolResult(text="Error: Gmail attachment has no usable content.")
        # Both the MIME type and the STORED filename come from the message's
        # part metadata — the caller-supplied filename is untrusted model input;
        # its extension would otherwise reclassify the file (the HTTP attachment
        # route derives Content-Type from the storage path, so a PDF stored as
        # "invoice.txt" would be served as text/plain). The filename argument
        # survives only as display metadata in the description.
        part_filename, part_mime = await _lookup_attachment_part(
            exec_context, message_id, attachment_id
        )
        content_type = (
            part_mime
            or (mimetypes.guess_type(part_filename)[0] if part_filename else None)
            or "application/octet-stream"
        )
        stored_name = part_filename or (
            f"gmail_attachment_{attachment_id}"
            f"{mimetypes.guess_extension(content_type) or ''}"
        )
        display_name = filename or stored_name
        return await _register_attachment(
            exec_context,
            content=content,
            filename=stored_name,
            content_type=content_type,
            tool_name="gmail_get_attachment",
            description=f"Gmail attachment {display_name}",
        )

    return await _guard(_impl)


async def _lookup_attachment_part(
    exec_context: ToolExecutionContext,
    message_id: str,
    attachment_id: str,
) -> tuple[str | None, str | None]:
    """Recover an attachment's filename and MIME type from its message's parts.

    Gmail's attachment download endpoint returns only the raw bytes, so when the
    caller omitted the filename the part metadata is the only source for a real
    name and content type (a bare fallback name would guess
    ``application/octet-stream``, which the attachment allowlist rejects).
    """
    message = (
        await _google_request(
            exec_context,
            GoogleScope.GMAIL_READONLY,
            url=f"{_GMAIL_API_BASE}/users/me/messages/{message_id}",
            params={"format": "full"},
        )
    ).json()
    _, attachments = await _walk_message_payload(message.get("payload", {}))
    for attachment in attachments:
        if attachment.get("attachment_id") != attachment_id:
            continue
        part_filename = attachment.get("filename")
        part_mime = attachment.get("mime_type")
        return (
            part_filename if isinstance(part_filename, str) and part_filename else None,
            part_mime if isinstance(part_mime, str) and part_mime else None,
        )
    return (None, None)


def _decode_attachment_payload(payload: GoogleJson) -> bytes | None:
    """Decode the base64url ``data`` field of a Gmail attachment response."""
    data = payload.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        return _decode_base64url(data)
    except (binascii.Error, ValueError):
        return None


def _mime_consistent_filename(name: str, content_type: str) -> str:
    """Force the stored name's extension to agree with the authoritative MIME type.

    Filenames are sender-controlled (a Gmail part or Drive file can be a PDF
    named ``invoice.txt``), and the attachment HTTP route derives its served
    Content-Type from the storage path — so a disagreeing extension would let
    allowed content be served back as a different type.
    """
    if mimetypes.guess_type(name)[0] == content_type:
        return name
    extension = mimetypes.guess_extension(content_type)
    if extension is None:
        return name
    stem = PurePosixPath(name).stem or name
    return f"{stem}{extension}"


async def _register_attachment(
    exec_context: ToolExecutionContext,
    *,
    content: bytes,
    filename: str,
    content_type: str,
    tool_name: str,
    description: str,
) -> ToolResult:
    """Register owned content and return the standard attachment reference."""
    registry = exec_context.attachment_registry
    assert registry is not None
    filename = _mime_consistent_filename(filename, content_type)
    try:
        metadata = await registry.store_and_register_tool_attachment(
            file_content=content,
            filename=filename,
            content_type=content_type,
            tool_name=tool_name,
            description=description,
            conversation_id=exec_context.conversation_id,
            owner_user_id=exec_context.user_id,
            db_context=exec_context.db_context,
        )
    except ValueError as exc:
        # The registry rejects disallowed mime types / oversized files.
        return ToolResult(text=f"Error: could not store the file — {exc}")
    return ToolResult(
        text=(
            f"Stored {filename} ({len(content)} bytes) as attachment"
            f" {metadata.attachment_id}."
        ),
        attachments=[
            ToolAttachment(
                mime_type=metadata.mime_type,
                attachment_id=metadata.attachment_id,
                description=description,
            )
        ],
        data={
            "attachment_id": metadata.attachment_id,
            "filename": filename,
            "mime_type": metadata.mime_type,
            "size": len(content),
        },
    )


async def drive_search_tool(
    exec_context: ToolExecutionContext,
    query: str,
    max_results: int = 10,
) -> ToolResult:
    """Search the acting user's Google Drive, falling back to metadata scope."""

    async def _impl() -> ToolResult:
        capped = max(1, min(max_results, _DRIVE_SEARCH_CAP))
        params = {
            "q": query,
            "pageSize": str(capped),
            "fields": (
                "nextPageToken,"
                "files(id,name,mimeType,owners(displayName,emailAddress),"
                "modifiedTime,webViewLink)"
            ),
        }
        listing = (await _drive_search_request(exec_context, params)).json()
        files = listing.get("files") or []
        results = [_summarize_drive_file(entry) for entry in files[:capped]]
        more_available = bool(listing.get("nextPageToken"))
        # Data-only result so the file ids/names are LLM-visible (see
        # gmail_search).
        data: GoogleJson = {"files": results, "more_results_available": more_available}
        if more_available:
            data["note"] = (
                "More matches exist beyond this page — narrow the query or"
                " raise max_results to see them."
            )
        return ToolResult(data=data)

    return await _guard(_impl)


async def _drive_search_request(
    exec_context: ToolExecutionContext,
    params: Mapping[str, str],
) -> ApiResponse:
    """Run a Drive search, retrying with metadata scope if full scope is ungranted."""
    try:
        return await _google_request(
            exec_context,
            GoogleScope.DRIVE_READONLY,
            url=f"{_DRIVE_API_BASE}/files",
            params=params,
        )
    except OAuthScopeNotGrantedError:
        return await _google_request(
            exec_context,
            GoogleScope.DRIVE_METADATA_READONLY,
            url=f"{_DRIVE_API_BASE}/files",
            params=params,
        )


def _summarize_drive_file(entry: GoogleJson) -> GoogleJson:
    """Reduce a Drive file resource to the search summary shape."""
    owners = entry.get("owners") or []
    owner = owners[0] if owners else {}
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "mime_type": entry.get("mimeType"),
        "owner": {
            "display_name": owner.get("displayName"),
            "email": owner.get("emailAddress"),
        },
        "modified_time": entry.get("modifiedTime"),
        "web_view_link": entry.get("webViewLink"),
    }


async def drive_get_file_tool(
    exec_context: ToolExecutionContext,
    file_id: str,
) -> ToolResult:
    """Fetch a Drive file: export native docs, inline small text, else attach."""

    async def _impl() -> ToolResult:
        meta = (
            await _google_request(
                exec_context,
                GoogleScope.DRIVE_READONLY,
                url=f"{_DRIVE_API_BASE}/files/{file_id}",
                params={"fields": "id,name,mimeType,size"},
            )
        ).json()
        name = meta.get("name") or file_id
        mime_type = meta.get("mimeType") or "application/octet-stream"
        if mime_type in _GOOGLE_DOC_EXPORTS:
            return await _drive_export_native(exec_context, file_id, name, mime_type)
        size = _coerce_int(meta.get("size"))
        # Pre-check on the ``alt=media`` path: the metadata already reports the
        # byte size for non-native files, so a file over the download limit is
        # refused before any body is fetched (Google-native exports report no
        # size and rely on the backend's response-body cap instead).
        if size is not None and size > _DRIVE_DOWNLOAD_LIMIT:
            raise _GoogleToolError(
                f"Drive file {name} is {size} bytes, which exceeds the "
                f"{_DRIVE_DOWNLOAD_LIMIT}-byte download limit."
            )
        return await _drive_fetch_content(exec_context, file_id, name, mime_type, size)

    return await _guard(_impl)


async def _drive_fetch_content(
    exec_context: ToolExecutionContext,
    file_id: str,
    name: str,
    mime_type: str,
    size: int | None,
) -> ToolResult:
    """Download a non-native Drive file inline (if small text) or into the registry."""
    content = (
        await _google_request(
            exec_context,
            GoogleScope.DRIVE_READONLY,
            url=f"{_DRIVE_API_BASE}/files/{file_id}",
            params={"alt": "media"},
        )
    ).content

    if _is_texty(mime_type) and (size is None or size <= _DRIVE_INLINE_TEXT_LIMIT):
        text = await asyncio.to_thread(content.decode, "utf-8", "replace")
        truncated = _truncate(text, _MESSAGE_BODY_CHAR_LIMIT)
        return ToolResult(
            text=truncated,
            data={
                "id": file_id,
                "name": name,
                "mime_type": mime_type,
                "content": truncated,
            },
        )

    if exec_context.attachment_registry is None:
        return ToolResult(text="Error: Attachment registry not available.")
    return await _register_attachment(
        exec_context,
        content=content,
        filename=name,
        content_type=mime_type,
        tool_name="drive_get_file",
        description=f"Drive file {name}",
    )


async def _drive_export_native(
    exec_context: ToolExecutionContext,
    file_id: str,
    name: str,
    mime_type: str,
) -> ToolResult:
    """Export a Google-native document as text and return it inline."""
    export_mime = _GOOGLE_DOC_EXPORTS[mime_type]
    response = await _google_request(
        exec_context,
        GoogleScope.DRIVE_READONLY,
        url=f"{_DRIVE_API_BASE}/files/{file_id}/export",
        params={"mimeType": export_mime},
    )
    text = await asyncio.to_thread(response.content.decode, "utf-8", "replace")
    truncated = _truncate(text, _MESSAGE_BODY_CHAR_LIMIT)
    return ToolResult(
        text=truncated,
        data={
            "id": file_id,
            "name": name,
            "mime_type": mime_type,
            "exported_as": export_mime,
            "content": truncated,
        },
    )


def _coerce_int(value: object) -> int | None:
    """Best-effort int coercion for the Drive ``size`` field (a JSON string)."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _is_texty(mime_type: str) -> bool:
    """Whether a mime type should be returned inline as text."""
    if mime_type.startswith("text/"):
        return True
    return mime_type in {
        "application/json",
        "application/xml",
        "application/csv",
        "application/x-yaml",
        "application/yaml",
    }
