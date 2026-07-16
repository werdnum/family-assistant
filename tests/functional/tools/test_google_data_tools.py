"""Functional tests for the read-only Gmail/Drive tools.

The tools are exercised against a fake :class:`GoogleApiBackend` and a fake
credential resolver (both implementing the real protocols, no monkeypatching),
plus a real :class:`AttachmentRegistry` and database so owner enforcement and
attachment references are validated end to end.
"""

from __future__ import annotations

import base64
import json
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    derive_tool_result_taint_source,
    resolve_tool_sink_class,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.google_api import GoogleApiResponse
from family_assistant.services.google_credentials import (
    GoogleNoActingUserError,
    GoogleNotConnectedError,
    GoogleScope,
    GoogleScopeNotGrantedError,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.google_data import (
    GOOGLE_TOOL_REQUIRED_SCOPES,
    drive_get_file_tool,
    drive_search_tool,
    gmail_get_attachment_tool,
    gmail_get_message_tool,
    gmail_search_tool,
)
from family_assistant.tools.metadata import build_tool_descriptor
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.google_api import GoogleApiBackend
    from family_assistant.services.google_credentials import GoogleCredentialResolver
    from family_assistant.tools.metadata import ToolDescriptor


def _b64url(text: str) -> str:
    """Encode text as base64url without padding (Gmail's body encoding)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class FakeGoogleApiBackend:
    """A :class:`GoogleApiBackend` serving canned payloads keyed by access token.

    ``routes`` maps ``access_token -> {(method, url_substring): payload_dict}``.
    A ``scripted`` sequence of ``(status_code, body_bytes)`` responses, when set,
    takes precedence and is consumed one entry per request (used for 401 retry
    tests).
    """

    routes: dict[str, dict[tuple[str, str], object]] = field(default_factory=dict)
    scripted: list[tuple[int, bytes]] = field(default_factory=list)
    requests: list[tuple[str, str, str]] = field(default_factory=list)

    async def request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None = None,
    ) -> GoogleApiResponse:
        self.requests.append((method, url, access_token))
        if self.scripted:
            status, body = self.scripted.pop(0)
            return GoogleApiResponse(status_code=status, content=body)
        # Match on the URL path suffix (ignoring query string) so a list route
        # (`.../messages`) does not also match a detail route
        # (`.../messages/{id}`).
        path = url.split("?", 1)[0]
        token_routes = self.routes.get(access_token, {})
        for (route_method, needle), payload in token_routes.items():
            if route_method == method and path.endswith(needle):
                return GoogleApiResponse(
                    status_code=200,
                    content=json.dumps(payload).encode("utf-8")
                    if not isinstance(payload, bytes)
                    else payload,
                )
        return GoogleApiResponse(status_code=404, content=b'{"error": "not found"}')


@dataclass
class FakeGoogleCredentialResolver:
    """A resolver returning a per-user token and tracking scope grants/evictions.

    ``tokens`` maps ``user_id -> access_token``. ``granted_scopes`` maps
    ``user_id -> set(scope values)``; a missing/ungranted scope raises
    :class:`GoogleScopeNotGrantedError`. ``raise_for_user`` maps a user id to an
    exception to raise instead of returning a token (fail-closed tests).
    """

    tokens: dict[str, str] = field(default_factory=dict)
    granted_scopes: dict[str, set[str]] = field(default_factory=dict)
    raise_for_user: dict[str, Exception] = field(default_factory=dict)
    evicted: list[str] = field(default_factory=list)

    async def access_token_for(
        self, exec_context: ToolExecutionContext, scope: GoogleScope
    ) -> str:
        user_id = exec_context.user_id
        if user_id is None:
            raise GoogleNoActingUserError()
        if user_id in self.raise_for_user:
            raise self.raise_for_user[user_id]
        granted = self.granted_scopes.get(user_id, set(GoogleScope))
        if scope.value not in granted:
            raise GoogleScopeNotGrantedError(scope)
        return self.tokens[user_id]

    def evict_cached_token(self, user_id: str) -> None:
        self.evicted.append(user_id)


def _make_context(
    db: DatabaseContext,
    *,
    user_id: str | None,
    resolver: FakeGoogleCredentialResolver | None,
    backend: FakeGoogleApiBackend | None,
    attachment_registry: AttachmentRegistry | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-google",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=attachment_registry,
        camera_backend=None,
        google_credentials=cast("GoogleCredentialResolver | None", resolver),
        google_api_backend=cast("GoogleApiBackend | None", backend),
        timezone=ZoneInfo("UTC"),
        user_id=user_id,
    )


def _registry(db_engine: AsyncEngine) -> AttachmentRegistry:
    return AttachmentRegistry(
        storage_path=tempfile.mkdtemp(), db_engine=db_engine, config=None
    )


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_backend_or_resolver_is_actionable_error(
    db_engine: AsyncEngine,
) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(db, user_id="user-a", resolver=None, backend=None)
        result = await gmail_search_tool(context, query="hello")
    text = result.get_text()
    assert "not configured" in text.lower()


@pytest.mark.asyncio
async def test_not_connected_message_surfaces(db_engine: AsyncEngine) -> None:
    resolver = FakeGoogleCredentialResolver(
        raise_for_user={"user-a": GoogleNotConnectedError()}
    )
    backend = FakeGoogleApiBackend()
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await gmail_search_tool(context, query="hello")
    assert "connect from settings" in result.get_text().lower()


@pytest.mark.asyncio
async def test_no_acting_user_fails_closed(db_engine: AsyncEngine) -> None:
    resolver = FakeGoogleCredentialResolver()
    backend = FakeGoogleApiBackend()
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(db, user_id=None, resolver=resolver, backend=backend)
        result = await gmail_search_tool(context, query="hello")
    assert "acting user" in result.get_text().lower()


# --------------------------------------------------------------------------- #
# Cross-user isolation
# --------------------------------------------------------------------------- #


def _gmail_search_routes(
    message_id: str, subject: str
) -> dict[tuple[str, str], object]:
    return {
        ("GET", "/users/me/messages"): {"messages": [{"id": message_id}]},
        ("GET", f"/messages/{message_id}"): {
            "id": message_id,
            "threadId": f"thread-{message_id}",
            "snippet": f"snippet for {subject}",
            "payload": {
                "headers": [
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Subject", "value": subject},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                ]
            },
        },
    }


@pytest.mark.asyncio
async def test_two_user_isolation_gmail_search(db_engine: AsyncEngine) -> None:
    resolver = FakeGoogleCredentialResolver(
        tokens={"user-a": "token-a", "user-b": "token-b"}
    )
    backend = FakeGoogleApiBackend(
        routes={
            "token-a": _gmail_search_routes("msg-a", "Mailbox A only"),
            "token-b": _gmail_search_routes("msg-b", "Mailbox B only"),
        }
    )
    async with DatabaseContext(engine=db_engine) as db:
        context_a = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        context_b = _make_context(
            db, user_id="user-b", resolver=resolver, backend=backend
        )
        result_a = await gmail_search_tool(context_a, query="anything")
        result_b = await gmail_search_tool(context_b, query="anything")

    data_a = result_a.get_data()
    data_b = result_b.get_data()
    assert isinstance(data_a, dict)
    assert isinstance(data_b, dict)
    subjects_a = [m["subject"] for m in data_a["messages"]]
    subjects_b = [m["subject"] for m in data_b["messages"]]
    assert subjects_a == ["Mailbox A only"]
    assert subjects_b == ["Mailbox B only"]


# --------------------------------------------------------------------------- #
# 401 retry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_401_then_200_retries_once_and_evicts(db_engine: AsyncEngine) -> None:
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    listing = json.dumps({"messages": []}).encode("utf-8")
    backend = FakeGoogleApiBackend(scripted=[(401, b"{}"), (200, listing)])
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await gmail_search_tool(context, query="anything")

    assert "Found 0 message(s)" in result.get_text()
    assert resolver.evicted == ["user-a"]
    assert len(backend.requests) == 2


@pytest.mark.asyncio
async def test_401_then_401_is_error(db_engine: AsyncEngine) -> None:
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeGoogleApiBackend(scripted=[(401, b"{}"), (401, b"{}")])
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await gmail_search_tool(context, query="anything")

    assert "Error" in result.get_text()
    assert "401" in result.get_text()


# --------------------------------------------------------------------------- #
# gmail_get_message: MIME walk + truncation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gmail_get_message_prefers_plain_and_lists_attachments(
    db_engine: AsyncEngine,
) -> None:
    payload = {
        "id": "msg-1",
        "threadId": "thread-1",
        "payload": {
            "headers": [
                {"name": "From", "value": "a@example.com"},
                {"name": "Subject", "value": "Permission form"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"data": _b64url("The plain text body wins.")},
                },
                {
                    "mimeType": "text/html",
                    "filename": "",
                    "body": {"data": _b64url("<p>ignored html</p>")},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "form.pdf",
                    "body": {"attachmentId": "att-1", "size": 1234},
                },
            ],
        },
    }
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeGoogleApiBackend(
        routes={"token-a": {("GET", "/messages/msg-1"): payload}}
    )
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await gmail_get_message_tool(context, message_id="msg-1")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["body"] == "The plain text body wins."
    assert len(data["attachments"]) == 1
    assert data["attachments"][0]["attachment_id"] == "att-1"
    assert data["attachments"][0]["filename"] == "form.pdf"


@pytest.mark.asyncio
async def test_gmail_get_message_html_fallback_and_truncation(
    db_engine: AsyncEngine,
) -> None:
    big = "word " * 20000  # > 50000 chars
    payload = {
        "id": "msg-2",
        "payload": {
            "headers": [{"name": "Subject", "value": "Big"}],
            "mimeType": "text/html",
            "filename": "",
            "body": {"data": _b64url(f"<p>{big}</p>")},
        },
    }
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeGoogleApiBackend(
        routes={"token-a": {("GET", "/messages/msg-2"): payload}}
    )
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await gmail_get_message_tool(context, message_id="msg-2")

    data = result.get_data()
    assert isinstance(data, dict)
    assert "truncated" in data["body"]
    assert "word" in data["body"]


# --------------------------------------------------------------------------- #
# gmail_get_attachment: owner enforcement
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gmail_get_attachment_registers_with_owner(
    db_engine: AsyncEngine,
) -> None:
    content = b"PDF-BYTES-HERE"
    payload = {"data": base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")}
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeGoogleApiBackend(
        routes={"token-a": {("GET", "/attachments/att-1"): payload}}
    )
    registry = _registry(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db,
            user_id="user-a",
            resolver=resolver,
            backend=backend,
            attachment_registry=registry,
        )
        result = await gmail_get_attachment_tool(
            context, message_id="msg-1", attachment_id="att-1", filename="form.pdf"
        )
        data = result.get_data()
        assert isinstance(data, dict)
        attachment_id = data["attachment_id"]
        assert result.attachments is not None
        assert result.attachments[0].attachment_id == attachment_id

        owned = await registry.get_attachment_content(
            db, attachment_id, acting_user_id="user-a"
        )
        assert owned == content
        # Another actor (or no actor) cannot read the owned attachment.
        assert (
            await registry.get_attachment_content(
                db, attachment_id, acting_user_id="user-b"
            )
            is None
        )
        assert (
            await registry.get_attachment_content(
                db, attachment_id, acting_user_id=None
            )
            is None
        )


@pytest.mark.asyncio
async def test_gmail_get_attachment_without_filename_uses_part_metadata(
    db_engine: AsyncEngine,
) -> None:
    content = b"PDF-BYTES-HERE"
    payload = {"data": base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")}
    message = {
        "id": "msg-1",
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "body": {},
            "parts": [
                {
                    "mimeType": "application/pdf",
                    "filename": "invite.pdf",
                    "body": {"attachmentId": "att-1", "size": len(content)},
                }
            ],
        },
    }
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeGoogleApiBackend(
        routes={
            "token-a": {
                ("GET", "/attachments/att-1"): payload,
                ("GET", "/messages/msg-1"): message,
            }
        }
    )
    registry = _registry(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db,
            user_id="user-a",
            resolver=resolver,
            backend=backend,
            attachment_registry=registry,
        )
        result = await gmail_get_attachment_tool(
            context, message_id="msg-1", attachment_id="att-1"
        )
        data = result.get_data()
        assert isinstance(data, dict), f"expected attachment reference, got {data}"
        stored = await registry.get_attachment(
            db, data["attachment_id"], acting_user_id="user-a"
        )
    assert stored is not None
    assert stored.mime_type == "application/pdf"
    assert stored.description == "Gmail attachment invite.pdf"


# --------------------------------------------------------------------------- #
# drive_search scope fallback
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_drive_search_falls_back_to_metadata_scope(
    db_engine: AsyncEngine,
) -> None:
    resolver = FakeGoogleCredentialResolver(
        tokens={"user-a": "token-a"},
        granted_scopes={"user-a": {GoogleScope.DRIVE_METADATA_READONLY.value}},
    )
    backend = FakeGoogleApiBackend(
        routes={
            "token-a": {
                ("GET", "/files"): {
                    "files": [
                        {
                            "id": "file-1",
                            "name": "tax.pdf",
                            "mimeType": "application/pdf",
                            "owners": [
                                {
                                    "displayName": "Me",
                                    "emailAddress": "me@example.com",
                                }
                            ],
                            "modifiedTime": "2024-01-01T00:00:00Z",
                            "webViewLink": "https://drive.example/file-1",
                        }
                    ]
                }
            }
        }
    )
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await drive_search_tool(context, query="name contains 'tax'")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["files"][0]["name"] == "tax.pdf"
    assert data["files"][0]["owner"]["email"] == "me@example.com"


# --------------------------------------------------------------------------- #
# drive_get_file: export / inline / attachment paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_drive_get_file_exports_google_doc(db_engine: AsyncEngine) -> None:
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeGoogleApiBackend(
        routes={
            "token-a": {
                ("GET", "/files/doc-1"): {
                    "id": "doc-1",
                    "name": "Notes",
                    "mimeType": "application/vnd.google-apps.document",
                },
                ("GET", "/files/doc-1/export"): b"Exported plain text content.",
            }
        }
    )
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await drive_get_file_tool(context, file_id="doc-1")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["exported_as"] == "text/plain"
    assert "Exported plain text content." in data["content"]


@pytest.mark.asyncio
async def test_drive_get_file_inlines_small_text(db_engine: AsyncEngine) -> None:
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    # drive_get_file issues two GETs to /files/txt-1 (metadata then alt=media);
    # scripting the responses in order keeps them unambiguous.
    backend = FakeGoogleApiBackend(
        scripted=[
            (
                200,
                json.dumps({
                    "id": "txt-1",
                    "name": "note.txt",
                    "mimeType": "text/plain",
                    "size": "17",
                }).encode("utf-8"),
            ),
            (200, b"hello inline text"),
        ]
    )
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await drive_get_file_tool(context, file_id="txt-1")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["content"] == "hello inline text"


@pytest.mark.asyncio
async def test_drive_get_file_large_binary_goes_to_attachment(
    db_engine: AsyncEngine,
) -> None:
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    binary = b"\x00\x01\x02BINARY" * 100
    backend = FakeGoogleApiBackend(
        scripted=[
            (
                200,
                json.dumps({
                    "id": "bin-1",
                    "name": "photo.png",
                    "mimeType": "image/png",
                    "size": str(len(binary)),
                }).encode("utf-8"),
            ),
            (200, binary),
        ]
    )
    registry = _registry(db_engine)
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db,
            user_id="user-a",
            resolver=resolver,
            backend=backend,
            attachment_registry=registry,
        )
        result = await drive_get_file_tool(context, file_id="bin-1")
        data = result.get_data()
        assert isinstance(data, dict)
        attachment_id = data["attachment_id"]
        owned = await registry.get_attachment_content(
            db, attachment_id, acting_user_id="user-a"
        )
        assert owned == binary
        assert (
            await registry.get_attachment_content(
                db, attachment_id, acting_user_id=None
            )
            is None
        )


@pytest.mark.asyncio
async def test_drive_get_file_oversized_metadata_errors_without_media_request(
    db_engine: AsyncEngine,
) -> None:
    resolver = FakeGoogleCredentialResolver(tokens={"user-a": "token-a"})
    oversized = 50 * 1024 * 1024
    backend = FakeGoogleApiBackend(
        routes={
            "token-a": {
                ("GET", "/files/huge-1"): {
                    "id": "huge-1",
                    "name": "movie.mp4",
                    "mimeType": "video/mp4",
                    "size": str(oversized),
                }
            }
        }
    )
    async with DatabaseContext(engine=db_engine) as db:
        context = _make_context(
            db, user_id="user-a", resolver=resolver, backend=backend
        )
        result = await drive_get_file_tool(context, file_id="huge-1")

    text = result.get_text()
    assert "exceeds" in text.lower()
    assert str(oversized) in text
    # Only the metadata GET was issued — the alt=media download never ran.
    assert len(backend.requests) == 1
    assert backend.requests[0][1].endswith("/files/huge-1")


# --------------------------------------------------------------------------- #
# Taint metadata
# --------------------------------------------------------------------------- #


_GOOGLE_TOOL_NAMES = [
    "gmail_search",
    "gmail_get_message",
    "gmail_get_attachment",
    "drive_search",
    "drive_get_file",
]


def _descriptor(name: str) -> ToolDescriptor:
    registration = next(r for r in LOCAL_TOOL_REGISTRATIONS if r.name == name)
    return build_tool_descriptor(
        registration.definition, registration.tags, origin="local"
    )


@pytest.mark.parametrize("tool_name", _GOOGLE_TOOL_NAMES)
def test_google_tools_taint_unknown_external(tool_name: str) -> None:
    source = derive_tool_result_taint_source(
        descriptor=_descriptor(tool_name), call_id=None
    )
    assert source is not None
    assert source.tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.parametrize("tool_name", _GOOGLE_TOOL_NAMES)
def test_google_tools_are_sensitive_read_broadening(tool_name: str) -> None:
    assert (
        resolve_tool_sink_class(_descriptor(tool_name))
        is SinkClass.SENSITIVE_READ_BROADENING
    )


def test_attach_to_response_is_user_local() -> None:
    assert (
        resolve_tool_sink_class(_descriptor("attach_to_response"))
        is SinkClass.USER_LOCAL
    )


def test_required_scopes_map_matches_tools() -> None:
    assert set(GOOGLE_TOOL_REQUIRED_SCOPES) == set(_GOOGLE_TOOL_NAMES)
    assert GOOGLE_TOOL_REQUIRED_SCOPES["drive_search"] == frozenset({
        GoogleScope.DRIVE_READONLY.value,
        GoogleScope.DRIVE_METADATA_READONLY.value,
    })
    assert GOOGLE_TOOL_REQUIRED_SCOPES["drive_get_file"] == frozenset({
        GoogleScope.DRIVE_READONLY.value
    })
