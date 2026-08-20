"""Scoped Gmail/Drive tools that act strictly as the turn's acting user.

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
import hashlib
import json
import logging
import mimetypes
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import filetype  # type: ignore[import-untyped]
from markdownify import markdownify

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.services.api_backend import ApiBackendError
from family_assistant.services.google_provider import GOOGLE_PROVIDER, GoogleScope
from family_assistant.services.oauth_credentials import (
    OAuthCredentialError,
    OAuthScopeNotGrantedError,
)
from family_assistant.tools.types import ToolAttachment, ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from family_assistant.services.api_backend import ApiBackend, ApiResponse
    from family_assistant.services.attachment_registry import AttachmentMetadata
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)

# Google REST JSON has an open, endpoint-specific shape; the tools narrow it with
# ``.get(...)`` and explicit ``isinstance`` checks at each use site.
# ast-grep-ignore: no-dict-any - Free-form Google REST JSON with arbitrary keys.
type GoogleJson = dict[str, Any]

_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
# What a sender's mailer declares when it has nothing useful to say about a
# file's type; the attachment registry's allowlist rejects it.
_GENERIC_CONTENT_TYPE = "application/octet-stream"

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
# Write requests are intentionally limited to the APIs' non-resumable upload
# budgets. Gmail attachments expand inside MIME and the API's base64url JSON;
# Drive multipart uploads are the documented small-file path (up to 5 MiB).
_GMAIL_DRAFT_ATTACHMENT_BYTES_LIMIT = 18 * 1024 * 1024
_GMAIL_DRAFT_RAW_MESSAGE_LIMIT = 25 * 1024 * 1024
_DRIVE_MULTIPART_CONTENT_LIMIT = 5 * 1024 * 1024
_GMAIL_DRAFT_ATTACHMENT_CAP = 10

_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_APP_PROPERTY_KEY = "family_assistant"
_APP_FOLDER_PROPERTY_VALUE = "app_folder_v1"
_APP_FILE_PROPERTY_VALUE = "app_file_v1"
_APP_FOLDER_DEFAULT_NAME = "Family Assistant"

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
    "gmail_create_draft": frozenset({GoogleScope.GMAIL_COMPOSE.value}),
    "drive_search": frozenset({
        GoogleScope.DRIVE_READONLY.value,
        GoogleScope.DRIVE_METADATA_READONLY.value,
    }),
    "drive_get_file": frozenset({GoogleScope.DRIVE_READONLY.value}),
    "drive_write_file": frozenset({GoogleScope.DRIVE_FILE.value}),
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
                "(attachment_id, part_id, filename, mime type, size). Use "
                "gmail_get_attachment to download a specific attachment, passing "
                "both its attachment_id and its part_id."
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
                "reply or read back. Provide the message_id, the attachment_id and "
                "the part_id from gmail_get_message. The stored file is private to "
                "the requesting user."
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
                    "part_id": {
                        "type": "string",
                        "description": (
                            "The part id from gmail_get_message's attachments list. "
                            "Always pass this when you have it: Gmail can hand out a "
                            "fresh attachment id every time a message is fetched, "
                            "and the part id is what reliably identifies the file's "
                            "name and type."
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
            "name": "gmail_create_draft",
            "description": (
                "Create an unsent draft in the requesting user's own Gmail account. "
                "This tool can only create a draft; it cannot send mail. Provide one "
                "or more recipient email addresses, a subject, and a plain-text body. "
                "Existing owned attachments can be included by attachment ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Recipient email addresses for the draft.",
                        "minItems": 1,
                    },
                    "subject": {"type": "string", "description": "Draft subject."},
                    "body": {
                        "type": "string",
                        "description": "Plain-text draft body.",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional CC email addresses.",
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional BCC email addresses.",
                    },
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "attachment"},
                        "description": (
                            "Optional IDs of existing attachments owned by the "
                            "requesting user."
                        ),
                        "maxItems": _GMAIL_DRAFT_ATTACHMENT_CAP,
                    },
                },
                "required": ["to", "subject", "body"],
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
    {
        "type": "function",
        "function": {
            "name": "drive_write_file",
            "description": (
                "Create or replace a file only inside the requesting user's "
                "Family Assistant Drive folder. For authored content, provide "
                "name and content; native Google Docs are the default, with plain "
                "text and Markdown also available. To upload an existing owned "
                "attachment instead, provide attachment_id and optionally name. "
                "The tool cannot choose another folder or arbitrary Drive file ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Filename or Google Doc title. Required for authored "
                            "content; optional for attachment uploads."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "UTF-8 content to write. Do not provide with attachment_id."
                        ),
                    },
                    "file_type": {
                        "type": "string",
                        "enum": ["google_doc", "text", "markdown"],
                        "description": "Authored-content format (default google_doc).",
                        "default": "google_doc",
                    },
                    "attachment_id": {
                        "type": "attachment",
                        "description": (
                            "Owned attachment to upload as an ordinary Drive file. "
                            "Do not provide content at the same time."
                        ),
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": (
                            "Replace an existing app-created file of the same name "
                            "inside the app folder. Defaults to false."
                        ),
                        "default": False,
                    },
                },
                "required": [],
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
    content: bytes | None = None,
    content_type: str | None = None,
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
    response = await _backend_request(
        backend,
        method=method,
        url=url,
        access_token=access_token,
        params=params,
        content=content,
        content_type=content_type,
    )
    if response.status_code == 401:
        if exec_context.user_id is not None:
            resolver.evict_cached_token(exec_context.user_id)
        access_token = await resolver.access_token_for(exec_context, scope)
        response = await _backend_request(
            backend,
            method=method,
            url=url,
            access_token=access_token,
            params=params,
            content=content,
            content_type=content_type,
        )

    if 200 <= response.status_code < 300:
        if exec_context.user_id is not None:
            await exec_context.db_context.oauth_connections.update_last_used(
                exec_context.user_id, GOOGLE_PROVIDER.name
            )
        return response
    raise _GoogleToolError(_format_api_error(response))


async def _backend_request(
    backend: ApiBackend,
    *,
    method: str,
    url: str,
    access_token: str,
    params: Mapping[str, str] | None,
    content: bytes | None,
    content_type: str | None,
) -> ApiResponse:
    """Call the shared backend, naming the provider in transport errors.

    The backend is provider-neutral and shared, so its transport/oversize
    messages carry no provider name; these errors deliberately propagate past
    the tool boundary to the generic tool-error renderer, where the user must
    still see which provider failed ("Google API request to ... failed").
    """
    try:
        return await backend.request(
            method=method,
            url=url,
            access_token=access_token,
            params=params,
            content=content,
            content_type=content_type,
        )
    except ApiBackendError as exc:
        raise ApiBackendError(f"{GOOGLE_PROVIDER.display_name} {exc}") from exc


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
            f"part_id: {att.get('part_id')}, "
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
            "part_id": part.get("partId"),
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
    part_id: str | None = None,
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
            exec_context, message_id, attachment_id, part_id
        )
        content_type = await _resolve_content_type(content, part_filename, part_mime)
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
    part_id: str | None,
) -> tuple[str | None, str | None]:
    """Recover an attachment's filename and MIME type from its message's parts.

    Gmail's attachment download endpoint returns only the raw bytes, so the part
    metadata is the source for a real name and content type.

    The part is matched by ``partId`` when the caller supplied one, because
    Gmail hands out a fresh ``attachmentId`` token on each ``messages.get`` for
    the same message: matching on the attachment id alone finds nothing whenever
    the token rotated between the caller's fetch and this one, and the file then
    gets stored as ``application/octet-stream``, losing the type a model needs
    to decide how to read it.
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
    match = _match_attachment_part(attachments, attachment_id, part_id)
    if match is None:
        return (None, None)
    part_filename = match.get("filename")
    part_mime = match.get("mime_type")
    return (
        part_filename if isinstance(part_filename, str) and part_filename else None,
        part_mime if isinstance(part_mime, str) and part_mime else None,
    )


def _match_attachment_part(
    attachments: list[GoogleJson],
    attachment_id: str,
    part_id: str | None,
) -> GoogleJson | None:
    """Find the part an attachment id / part id pair refers to.

    The attachment id wins when it still resolves: it identifies exactly one
    part, whereas a part id is positional and a model that mixed two
    attachments up would name a real but wrong part rather than missing.
    The part id is the fallback for the case it exists to cover — the
    attachment id having rotated out from under the caller.
    """
    for attachment in attachments:
        if attachment.get("attachment_id") == attachment_id:
            return attachment
    if part_id:
        for attachment in attachments:
            if attachment.get("part_id") == part_id:
                return attachment
    return None


async def _resolve_content_type(
    content: bytes,
    part_filename: str | None,
    part_mime: str | None,
) -> str:
    """Decide what type an attachment's bytes should be stored as.

    Gmail echoes the sender's ``Content-Type``, and plenty of mailers label
    every attachment ``application/octet-stream``, so a declared generic type is
    treated as no answer at all rather than a truthy one — the bytes usually say
    what the file really is, and a real type is what tells a model whether it
    can read the file at all.
    Sniffing beats the part filename because it cannot be talked into
    reclassifying content: only formats with no magic number (CSV, plain text)
    fall through to the sender-supplied name.
    """
    if part_mime and part_mime != _GENERIC_CONTENT_TYPE:
        return part_mime
    kind = await asyncio.to_thread(filetype.guess, content)
    if kind is not None:
        return str(kind.mime)
    guessed = mimetypes.guess_type(part_filename)[0] if part_filename else None
    return guessed or _GENERIC_CONTENT_TYPE


def _decode_attachment_payload(payload: GoogleJson) -> bytes | None:
    """Decode the base64url ``data`` field of a Gmail attachment response."""
    data = payload.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        return _decode_base64url(data)
    except (binascii.Error, ValueError):
        return None


def _validated_addresses(addresses: list[str], field_name: str) -> list[str]:
    """Return normalized bare email addresses or raise a user-facing error."""
    if not addresses and field_name == "to":
        raise _GoogleToolError("A Gmail draft requires at least one recipient.")
    normalized: list[str] = []
    for raw_address in addresses:
        candidate = raw_address.strip()
        try:
            address = Address(addr_spec=candidate)
        except (HeaderParseError, TypeError, ValueError) as exc:
            raise _GoogleToolError(
                f"Invalid {field_name} email address: {raw_address!r}."
            ) from exc
        if not address.username or not address.domain:
            raise _GoogleToolError(
                f"Invalid {field_name} email address: {raw_address!r}."
            )
        normalized.append(str(address))
    return normalized


def _google_user_operation_lock(
    exec_context: ToolExecutionContext, operation: str
) -> asyncio.Lock:
    """Return a per-user lock owned by the app-wired Google credential resolver."""
    user_id = exec_context.user_id
    resolver = (exec_context.credential_resolvers or {}).get(GOOGLE_PROVIDER.name)
    if user_id is None or resolver is None:
        raise _GoogleToolError(
            "Google integration is not configured for a specific requesting user."
        )
    return resolver.user_operation_lock(user_id, operation)


def _set_draft_header(message: EmailMessage, name: str, value: str) -> None:
    """Set one MIME header and render header-injection errors safely."""
    try:
        message[name] = value
    except ValueError as exc:
        raise _GoogleToolError(f"Invalid Gmail draft {name} header: {exc}") from exc


def _attachment_filename(metadata: AttachmentMetadata, attachment_id: str) -> str:
    """Choose a safe display filename from attachment-registry metadata."""
    original = metadata.metadata.get("original_filename")
    if isinstance(original, str) and original:
        filename = PurePosixPath(original).name
    elif metadata.storage_path:
        filename = PurePosixPath(metadata.storage_path).name
    else:
        filename = f"attachment-{attachment_id}"
    if "\r" in filename or "\n" in filename:
        raise _GoogleToolError(f"Attachment {attachment_id} has an invalid filename.")
    return filename


def _encode_gmail_draft_payload(message: EmailMessage) -> bytes:
    """Serialize and encode a Gmail draft outside the async event loop."""
    raw_message = message.as_bytes()
    if len(raw_message) > _GMAIL_DRAFT_RAW_MESSAGE_LIMIT:
        raise _GoogleToolError("The encoded Gmail draft is too large to upload.")
    return json.dumps({
        "message": {"raw": base64.urlsafe_b64encode(raw_message).decode("ascii")}
    }).encode("utf-8")


async def _load_owned_attachment(
    exec_context: ToolExecutionContext,
    attachment_id: ScriptAttachment | str,
    *,
    max_bytes: int,
) -> tuple[bytes, str, str]:
    """Load one attachment through the registry's acting-user boundary."""
    attachment_id_str = (
        attachment_id.get_id()
        if isinstance(attachment_id, ScriptAttachment)
        else attachment_id
    )
    registry = exec_context.attachment_registry
    if registry is None:
        raise _GoogleToolError("Attachment registry is not available.")
    metadata = await registry.get_attachment(
        exec_context.db_context,
        attachment_id_str,
        acting_user_id=exec_context.user_id,
    )
    if metadata is None:
        raise _GoogleToolError(
            f"Attachment {attachment_id_str} was not found for the requesting user."
        )
    legacy_user_owned = (
        metadata.owner_user_id is None
        and metadata.source_type == "user"
        and metadata.source_id == exec_context.user_id
    )
    same_conversation_ownerless = (
        metadata.owner_user_id is None
        and metadata.conversation_id is not None
        and metadata.conversation_id == exec_context.conversation_id
    )
    if (
        metadata.owner_user_id != exec_context.user_id
        and not legacy_user_owned
        and not same_conversation_ownerless
    ):
        raise _GoogleToolError(
            f"Attachment {attachment_id_str} is not owned by the requesting user."
        )
    if metadata.size > max_bytes:
        raise _GoogleToolError(
            f"Attachment {attachment_id_str} exceeds the {max_bytes}-byte upload limit."
        )
    content = await registry.get_attachment_content(
        exec_context.db_context,
        attachment_id_str,
        acting_user_id=exec_context.user_id,
    )
    if content is None:
        raise _GoogleToolError(
            f"Attachment {attachment_id_str} has no readable content."
        )
    if len(content) > max_bytes:
        raise _GoogleToolError(
            f"Attachment {attachment_id_str} exceeds the {max_bytes}-byte upload limit."
        )
    return (
        content,
        _attachment_filename(metadata, attachment_id_str),
        metadata.mime_type,
    )


async def gmail_create_draft_tool(
    exec_context: ToolExecutionContext,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachment_ids: list[ScriptAttachment | str] | None = None,
) -> ToolResult:
    """Create an unsent Gmail draft for the acting user."""

    async def _impl() -> ToolResult:
        if len(attachment_ids or []) > _GMAIL_DRAFT_ATTACHMENT_CAP:
            raise _GoogleToolError(
                f"A Gmail draft supports at most {_GMAIL_DRAFT_ATTACHMENT_CAP} "
                "attachments."
            )
        if exec_context.user_id is None:
            raise _GoogleToolError("Google access requires a specific requesting user.")
        connection = await exec_context.db_context.oauth_connections.get_connection(
            exec_context.user_id, GOOGLE_PROVIDER.name
        )
        if connection is None:
            raise _GoogleToolError(
                "No Google account is connected — connect from Settings."
            )

        if len(body.encode("utf-8")) > _GMAIL_DRAFT_ATTACHMENT_BYTES_LIMIT:
            raise _GoogleToolError("The Gmail draft body is too large to upload.")
        message = EmailMessage()
        _set_draft_header(message, "From", connection.provider_account_email)
        _set_draft_header(message, "To", ", ".join(_validated_addresses(to, "to")))
        if cc:
            _set_draft_header(message, "Cc", ", ".join(_validated_addresses(cc, "cc")))
        if bcc:
            _set_draft_header(
                message, "Bcc", ", ".join(_validated_addresses(bcc, "bcc"))
            )
        _set_draft_header(message, "Subject", subject)
        message.set_content(body)

        total_attachment_bytes = 0
        attached_names: list[str] = []
        for attachment_id in attachment_ids or []:
            content, filename, mime_type = await _load_owned_attachment(
                exec_context,
                attachment_id,
                max_bytes=_GMAIL_DRAFT_ATTACHMENT_BYTES_LIMIT,
            )
            total_attachment_bytes += len(content)
            if total_attachment_bytes > _GMAIL_DRAFT_ATTACHMENT_BYTES_LIMIT:
                raise _GoogleToolError(
                    "The draft attachments exceed the combined Google upload limit."
                )
            maintype, separator, subtype = mime_type.partition("/")
            if not separator or not maintype or not subtype:
                maintype, subtype = "application", "octet-stream"
            message.add_attachment(
                content,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )
            attached_names.append(filename)

        payload = await asyncio.to_thread(_encode_gmail_draft_payload, message)
        draft = (
            await _google_request(
                exec_context,
                GoogleScope.GMAIL_COMPOSE,
                method="POST",
                url=f"{_GMAIL_API_BASE}/users/me/drafts",
                content=payload,
                content_type="application/json; charset=UTF-8",
            )
        ).json()
        return ToolResult(
            data={
                "status": "draft_created",
                "draft_id": draft.get("id"),
                "message_id": draft.get("message", {}).get("id"),
                "to": to,
                "cc": cc or [],
                "bcc": bcc or [],
                "subject": subject,
                "attachments": attached_names,
            }
        )

    return await _guard(_impl)


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


def _drive_query_literal(value: str) -> str:
    """Escape one string literal for Drive query syntax."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _validated_drive_name(name: str) -> str:
    """Validate a single file name; paths and control characters are forbidden."""
    normalized = name.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or len(normalized) > 200
        or any(ord(character) < 32 for character in normalized)
    ):
        raise _GoogleToolError(
            "Drive file names must be 1-200 characters and cannot contain paths "
            "or control characters."
        )
    return normalized


def _multipart_related(
    metadata: GoogleJson, media: bytes, media_type: str
) -> tuple[bytes, str]:
    """Build a bounded RFC 2387 multipart/related Drive upload body."""
    metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    seed = metadata_bytes + b"\0" + media
    boundary = f"family_assistant_{hashlib.sha256(seed).hexdigest()}"
    counter = 0
    while boundary.encode("ascii") in media:
        counter += 1
        boundary = (
            "family_assistant_"
            + hashlib.sha256(seed + str(counter).encode("ascii")).hexdigest()
        )
    body = b"\r\n".join([
        f"--{boundary}".encode("ascii"),
        b"Content-Type: application/json; charset=UTF-8",
        b"",
        metadata_bytes,
        f"--{boundary}".encode("ascii"),
        f"Content-Type: {media_type}".encode("ascii"),
        b"",
        media,
        f"--{boundary}--".encode("ascii"),
        b"",
    ])
    return body, f"multipart/related; boundary={boundary}"


async def _find_app_folder(exec_context: ToolExecutionContext) -> GoogleJson | None:
    """Find the one Drive folder marked as this app's dedicated folder."""
    marker = _drive_query_literal(_APP_FOLDER_PROPERTY_VALUE)
    key = _drive_query_literal(_APP_PROPERTY_KEY)
    listing = (
        await _google_request(
            exec_context,
            GoogleScope.DRIVE_FILE,
            url=f"{_DRIVE_API_BASE}/files",
            params={
                "q": (
                    f"appProperties has {{ key='{key}' and value='{marker}' }} "
                    f"and mimeType = '{_DRIVE_FOLDER_MIME}' and trashed = false"
                ),
                "pageSize": "2",
                "fields": "files(id,name,mimeType,webViewLink)",
            },
        )
    ).json()
    folders = listing.get("files") or []
    if len(folders) > 1:
        raise _GoogleToolError(
            "Multiple Family Assistant Drive folders are marked for this account; "
            "resolve the duplicate folders before writing."
        )
    return folders[0] if folders else None


async def _ensure_app_folder(exec_context: ToolExecutionContext) -> GoogleJson:
    """Return the dedicated Drive folder, creating it in My Drive if absent."""
    existing = await _find_app_folder(exec_context)
    if existing is not None:
        return existing
    metadata = {
        "name": _APP_FOLDER_DEFAULT_NAME,
        "mimeType": _DRIVE_FOLDER_MIME,
        "parents": ["root"],
        "appProperties": {_APP_PROPERTY_KEY: _APP_FOLDER_PROPERTY_VALUE},
    }
    return (
        await _google_request(
            exec_context,
            GoogleScope.DRIVE_FILE,
            method="POST",
            url=f"{_DRIVE_API_BASE}/files",
            params={"fields": "id,name,mimeType,webViewLink"},
            content=json.dumps(metadata).encode("utf-8"),
            content_type="application/json; charset=UTF-8",
        )
    ).json()


async def _find_app_file(
    exec_context: ToolExecutionContext, folder_id: str, name: str
) -> GoogleJson | None:
    """Find one app-marked file with ``name`` beneath the app folder."""
    escaped_name = _drive_query_literal(name)
    escaped_parent = _drive_query_literal(folder_id)
    key = _drive_query_literal(_APP_PROPERTY_KEY)
    marker = _drive_query_literal(_APP_FILE_PROPERTY_VALUE)
    listing = (
        await _google_request(
            exec_context,
            GoogleScope.DRIVE_FILE,
            url=f"{_DRIVE_API_BASE}/files",
            params={
                "q": (
                    f"'{escaped_parent}' in parents and name = '{escaped_name}' "
                    f"and appProperties has {{ key='{key}' and value='{marker}' }} "
                    "and trashed = false"
                ),
                "pageSize": "2",
                "fields": "files(id,name,mimeType,webViewLink)",
            },
        )
    ).json()
    files = listing.get("files") or []
    if len(files) > 1:
        raise _GoogleToolError(
            f"Multiple app-created Drive files are named {name!r}; rename the "
            "duplicates before overwriting."
        )
    return files[0] if files else None


async def _drive_write_payload(
    exec_context: ToolExecutionContext,
    *,
    folder_id: str,
    name: str,
    content: bytes,
    mime_type: str,
    overwrite: bool,
) -> tuple[GoogleJson, str]:
    """Create or replace one app-marked file beneath the dedicated folder."""
    existing = await _find_app_file(exec_context, folder_id, name)
    if existing is not None and not overwrite:
        raise _GoogleToolError(
            f"A file named {name!r} already exists in the Family Assistant "
            "folder; set overwrite=true to replace it."
        )
    if existing is not None and existing.get("mimeType") != mime_type:
        raise _GoogleToolError(
            f"Cannot overwrite {name!r} with a different file type; choose a new "
            "name or match the existing type."
        )

    metadata: GoogleJson = {
        "name": name,
        "mimeType": mime_type,
        "appProperties": {_APP_PROPERTY_KEY: _APP_FILE_PROPERTY_VALUE},
    }
    method = "POST"
    url = "https://www.googleapis.com/upload/drive/v3/files"
    status = "created"
    if existing is None:
        metadata["parents"] = [folder_id]
    else:
        method = "PATCH"
        url = f"https://www.googleapis.com/upload/drive/v3/files/{existing['id']}"
        status = "replaced"
    request_body, request_content_type = _multipart_related(
        metadata,
        content,
        "text/plain; charset=UTF-8" if mime_type == _GOOGLE_DOC_MIME else mime_type,
    )
    result = (
        await _google_request(
            exec_context,
            GoogleScope.DRIVE_FILE,
            method=method,
            url=url,
            params={
                "uploadType": "multipart",
                "fields": "id,name,mimeType,webViewLink,parents",
            },
            content=request_body,
            content_type=request_content_type,
        )
    ).json()
    return result, status


async def drive_write_file_tool(
    exec_context: ToolExecutionContext,
    name: str | None = None,
    content: str | None = None,
    file_type: str = "google_doc",
    attachment_id: ScriptAttachment | str | None = None,
    overwrite: bool = False,
) -> ToolResult:
    """Write authored content or an owned attachment inside the app Drive folder."""

    async def _impl() -> ToolResult:
        if (content is None) == (attachment_id is None):
            raise _GoogleToolError(
                "Provide exactly one of content or attachment_id when writing to Drive."
            )
        if attachment_id is not None:
            file_content, attachment_name, mime_type = await _load_owned_attachment(
                exec_context,
                attachment_id,
                max_bytes=_DRIVE_MULTIPART_CONTENT_LIMIT,
            )
            resolved_name = _validated_drive_name(name or attachment_name)
        else:
            assert content is not None
            if name is None:
                raise _GoogleToolError("A name is required for authored Drive content.")
            resolved_name = _validated_drive_name(name)
            file_content = content.encode("utf-8")
            mime_types = {
                "google_doc": _GOOGLE_DOC_MIME,
                "text": "text/plain",
                "markdown": "text/markdown",
            }
            try:
                mime_type = mime_types[file_type]
            except KeyError as exc:
                raise _GoogleToolError(
                    "file_type must be google_doc, text, or markdown."
                ) from exc
        if len(file_content) > _DRIVE_MULTIPART_CONTENT_LIMIT:
            raise _GoogleToolError(
                f"Drive writes are limited to {_DRIVE_MULTIPART_CONTENT_LIMIT} bytes."
            )

        async with _google_user_operation_lock(exec_context, "drive_write"):
            folder = await _ensure_app_folder(exec_context)
            folder_id = folder.get("id")
            if not isinstance(folder_id, str) or not folder_id:
                raise _GoogleToolError("Google Drive did not return an app folder ID.")
            written, status = await _drive_write_payload(
                exec_context,
                folder_id=folder_id,
                name=resolved_name,
                content=file_content,
                mime_type=mime_type,
                overwrite=overwrite,
            )
        return ToolResult(
            data={
                "status": status,
                "file_id": written.get("id"),
                "name": written.get("name", resolved_name),
                "mime_type": written.get("mimeType", mime_type),
                "web_view_link": written.get("webViewLink"),
                "folder_id": folder_id,
                "folder_name": folder.get("name", _APP_FOLDER_DEFAULT_NAME),
            }
        )

    return await _guard(_impl)
