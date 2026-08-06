"""Functional tests for the scoped Gmail/Drive tools.

The tools are exercised against a fake :class:`ApiBackend` and a fake
credential resolver (both implementing the real protocols, no monkeypatching),
plus a real :class:`AttachmentRegistry` and database so owner enforcement and
attachment references are validated end to end.
"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    derive_tool_result_taint_source,
    resolve_tool_sink_class,
)
from family_assistant.services.api_backend import ApiBackendError, ApiResponse
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.google_provider import GoogleScope
from family_assistant.services.oauth_credentials import (
    OAuthNoActingUserError,
    OAuthNotConnectedError,
    OAuthScopeNotGrantedError,
)
from family_assistant.storage.database import Database
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.attachment_utils import fetch_attachment_object
from family_assistant.tools.google_data import (
    GOOGLE_TOOL_REQUIRED_SCOPES,
    drive_get_file_tool,
    drive_search_tool,
    drive_write_file_tool,
    gmail_create_draft_tool,
    gmail_get_attachment_tool,
    gmail_get_message_tool,
    gmail_search_tool,
)
from family_assistant.tools.metadata import build_tool_descriptor
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.api_backend import ApiBackend
    from family_assistant.services.oauth_credentials import OAuthCredentialResolver
    from family_assistant.tools.metadata import ToolDescriptor


def _b64url(text: str) -> str:
    """Encode text as base64url without padding (Gmail's body encoding)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class FakeApiBackend:
    """A :class:`ApiBackend` serving canned payloads keyed by access token.

    ``routes`` maps ``access_token -> {(method, url_substring): payload_dict}``.
    A ``scripted`` sequence of ``(status_code, body_bytes)`` responses, when set,
    takes precedence and is consumed one entry per request (used for 401 retry
    tests).
    """

    routes: dict[str, dict[tuple[str, str], object]] = field(default_factory=dict)
    scripted: list[tuple[int, bytes]] = field(default_factory=list)
    requests: list[tuple[str, str, str]] = field(default_factory=list)
    request_params: list[Mapping[str, str] | None] = field(default_factory=list)
    request_bodies: list[tuple[bytes | None, str | None]] = field(default_factory=list)
    transport_error: Exception | None = None

    async def request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> ApiResponse:
        self.requests.append((method, url, access_token))
        self.request_params.append(params)
        self.request_bodies.append((content, content_type))
        if self.transport_error is not None:
            raise self.transport_error
        if self.scripted:
            status, body = self.scripted.pop(0)
            return ApiResponse(status_code=status, content=body)
        # Match on the URL path suffix (ignoring query string) so a list route
        # (`.../messages`) does not also match a detail route
        # (`.../messages/{id}`).
        path = url.split("?", 1)[0]
        token_routes = self.routes.get(access_token, {})
        for (route_method, needle), payload in token_routes.items():
            if route_method == method and path.endswith(needle):
                return ApiResponse(
                    status_code=200,
                    content=json.dumps(payload).encode("utf-8")
                    if not isinstance(payload, bytes)
                    else payload,
                )
        return ApiResponse(status_code=404, content=b'{"error": "not found"}')


@dataclass
class FakeCredentialResolver:
    """A resolver returning a per-user token and tracking scope grants/evictions.

    ``tokens`` maps ``user_id -> access_token``. ``granted_scopes`` maps
    ``user_id -> set(scope values)``; a missing/ungranted scope raises
    :class:`OAuthScopeNotGrantedError`. ``raise_for_user`` maps a user id to an
    exception to raise instead of returning a token (fail-closed tests).
    """

    tokens: dict[str, str] = field(default_factory=dict)
    granted_scopes: dict[str, set[str]] = field(default_factory=dict)
    raise_for_user: dict[str, Exception] = field(default_factory=dict)
    evicted: list[str] = field(default_factory=list)
    operation_locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)

    async def access_token_for(
        self, exec_context: ToolExecutionContext, scope: GoogleScope
    ) -> str:
        user_id = exec_context.user_id
        if user_id is None:
            raise OAuthNoActingUserError("Google")
        if user_id in self.raise_for_user:
            raise self.raise_for_user[user_id]
        granted = self.granted_scopes.get(user_id, set(GoogleScope))
        if scope.value not in granted:
            raise OAuthScopeNotGrantedError("Google", scope.value)
        return self.tokens[user_id]

    def evict_cached_token(self, user_id: str) -> None:
        self.evicted.append(user_id)

    def user_operation_lock(self, user_id: str, operation: str) -> asyncio.Lock:
        return self.operation_locks.setdefault((user_id, operation), asyncio.Lock())


@dataclass
class ConcurrentDriveApiBackend(FakeApiBackend):
    """Pause the first folder lookup so a second Drive write overlaps it."""

    first_folder_search_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_first_folder_search: asyncio.Event = field(default_factory=asyncio.Event)
    folder_created: bool = False
    folder_create_count: int = 0

    async def request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> ApiResponse:
        query = (params or {}).get("q", "")
        is_folder_search = method == "GET" and "google-apps.folder" in query
        is_folder_create = (
            method == "POST"
            and url == "https://www.googleapis.com/drive/v3/files"
            and content is not None
            and b"google-apps.folder" in content
        )
        if is_folder_search:
            if not self.folder_created:
                if not self.first_folder_search_started.is_set():
                    self.first_folder_search_started.set()
                    await self.release_first_folder_search.wait()
                response = b'{"files":[]}'
            else:
                response = b'{"files":[{"id":"folder-1","name":"Family Assistant"}]}'
            self.scripted.insert(0, (200, response))
        elif is_folder_create:
            self.folder_created = True
            self.folder_create_count += 1
            self.scripted.insert(
                0, (200, b'{"id":"folder-1","name":"Family Assistant"}')
            )
        return await super().request(
            method=method,
            url=url,
            access_token=access_token,
            params=params,
            content=content,
            content_type=content_type,
        )


def _make_context(
    db: Database,
    *,
    user_id: str | None,
    resolver: FakeCredentialResolver | None,
    backend: FakeApiBackend | None,
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
        credential_resolvers=(
            {"google": cast("OAuthCredentialResolver", resolver)} if resolver else None
        ),
        api_backend=cast("ApiBackend | None", backend),
        timezone=ZoneInfo("UTC"),
        user_id=user_id,
    )


def _registry(db_engine: AsyncEngine) -> AttachmentRegistry:
    return AttachmentRegistry(
        storage_path=tempfile.mkdtemp(), db_engine=db_engine, config=None
    )


async def _store_google_connection(db: Database, user_id: str = "user-a") -> None:
    """Store the connected-account email needed for RFC-compliant draft headers."""
    await db.oauth_connections.upsert_connection(
        user_id=user_id,
        provider="google",
        provider_account_email=f"{user_id}@example.com",
        scopes=[scope.value for scope in GoogleScope],
        refresh_token_encrypted="ciphertext-not-used-by-fake-resolver",
    )


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_backend_or_resolver_is_actionable_error(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=None, backend=None)
    result = await gmail_search_tool(context, query="hello")
    text = result.get_text()
    assert "not configured" in text.lower()


@pytest.mark.asyncio
async def test_not_connected_message_surfaces(db_engine: AsyncEngine) -> None:
    resolver = FakeCredentialResolver(
        raise_for_user={"user-a": OAuthNotConnectedError("Google")}
    )
    backend = FakeApiBackend()
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await gmail_search_tool(context, query="hello")
    assert "connect from settings" in result.get_text().lower()


@pytest.mark.asyncio
async def test_no_acting_user_fails_closed(db_engine: AsyncEngine) -> None:
    resolver = FakeCredentialResolver()
    backend = FakeApiBackend()
    db = Database(engine=db_engine)
    context = _make_context(db, user_id=None, resolver=resolver, backend=backend)
    result = await gmail_search_tool(context, query="hello")
    assert "acting user" in result.get_text().lower()


@pytest.mark.asyncio
async def test_transport_error_names_the_provider(db_engine: AsyncEngine) -> None:
    """Backend transport errors propagate with the provider named.

    The shared backend's message is provider-neutral; the tool layer must
    re-raise it prefixed with the provider so the generic tool-error renderer
    still tells the user which integration failed.
    """
    resolver = FakeCredentialResolver(tokens={"user-a": "tok-a"})
    backend = FakeApiBackend(
        transport_error=ApiBackendError(
            "API request to https://gmail.googleapis.com/gmail/v1/users/me/messages "
            "failed"
        )
    )
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    with pytest.raises(ApiBackendError, match=r"^Google API request to "):
        await gmail_search_tool(context, query="hello")


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
async def test_gmail_search_marks_paged_results_partial(
    db_engine: AsyncEngine,
) -> None:
    routes = _gmail_search_routes("msg-1", "hello world")
    listing = {"messages": [{"id": "msg-1"}], "nextPageToken": "page-2"}
    routes[("GET", "/users/me/messages")] = listing
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(routes={"token-a": routes})
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await gmail_search_tool(context, query="hello")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["more_results_available"] is True
    # get_text() is what the LLM sees: it must carry both the ids and the note.
    assert "More matches exist" in result.get_text()
    assert "msg-1" in result.get_text()
    assert "hello world" in result.get_text()


@pytest.mark.asyncio
async def test_two_user_isolation_gmail_search(db_engine: AsyncEngine) -> None:
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a", "user-b": "token-b"})
    backend = FakeApiBackend(
        routes={
            "token-a": _gmail_search_routes("msg-a", "Mailbox A only"),
            "token-b": _gmail_search_routes("msg-b", "Mailbox B only"),
        }
    )
    db = Database(engine=db_engine)
    context_a = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    context_b = _make_context(db, user_id="user-b", resolver=resolver, backend=backend)
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
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    listing = json.dumps({"messages": []}).encode("utf-8")
    backend = FakeApiBackend(scripted=[(401, b"{}"), (200, listing)])
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await gmail_search_tool(context, query="anything")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["messages"] == []
    assert resolver.evicted == ["user-a"]
    assert len(backend.requests) == 2


@pytest.mark.asyncio
async def test_401_then_401_is_error(db_engine: AsyncEngine) -> None:
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(scripted=[(401, b"{}"), (401, b"{}")])
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
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
                    "partId": "2",
                    "mimeType": "application/pdf",
                    "filename": "form.pdf",
                    "body": {"attachmentId": "att-1", "size": 1234},
                },
            ],
        },
    }
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(routes={"token-a": {("GET", "/messages/msg-1"): payload}})
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await gmail_get_message_tool(context, message_id="msg-1")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["body"] == "The plain text body wins."
    assert len(data["attachments"]) == 1
    assert data["attachments"][0]["attachment_id"] == "att-1"
    assert data["attachments"][0]["part_id"] == "2"
    assert data["attachments"][0]["filename"] == "form.pdf"
    # The LLM sees get_text(), so the ids it needs for gmail_get_attachment must
    # be rendered there.
    assert "att-1" in result.get_text()
    assert "part_id: 2" in result.get_text()


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
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(routes={"token-a": {("GET", "/messages/msg-2"): payload}})
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
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
    message = {
        "id": "msg-1",
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "body": {},
            "parts": [
                {
                    "mimeType": "application/pdf",
                    "filename": "form.pdf",
                    "body": {"attachmentId": "att-1", "size": len(content)},
                }
            ],
        },
    }
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(
        routes={
            "token-a": {
                ("GET", "/attachments/att-1"): payload,
                ("GET", "/messages/msg-1"): message,
            }
        }
    )
    registry = _registry(db_engine)
    db = Database(engine=db_engine)
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
        await registry.get_attachment_content(db, attachment_id, acting_user_id=None)
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
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(
        routes={
            "token-a": {
                ("GET", "/attachments/att-1"): payload,
                ("GET", "/messages/msg-1"): message,
            }
        }
    )
    registry = _registry(db_engine)
    db = Database(engine=db_engine)
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


@pytest.mark.asyncio
async def test_gmail_get_attachment_part_id_survives_rotated_attachment_id(
    db_engine: AsyncEngine,
) -> None:
    """A rotated attachmentId must not cost us the part's real MIME type.

    Gmail returns a fresh attachmentId token on each messages.get for the same
    message, so the id the caller downloaded with matches nothing when the tool
    re-reads the parts. The stable partId does.
    """
    content = b"%PDF-1.4\nQBE CTP certificate\n"
    payload = {"data": base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")}
    message = {
        "id": "msg-1",
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "body": {},
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "certificate.pdf",
                    "body": {"attachmentId": "att-rotated", "size": len(content)},
                }
            ],
        },
    }
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(
        routes={
            "token-a": {
                ("GET", "/attachments/att-original"): payload,
                ("GET", "/messages/msg-1"): message,
            }
        }
    )
    registry = _registry(db_engine)
    db = Database(engine=db_engine)
    context = _make_context(
        db,
        user_id="user-a",
        resolver=resolver,
        backend=backend,
        attachment_registry=registry,
    )
    result = await gmail_get_attachment_tool(
        context, message_id="msg-1", attachment_id="att-original", part_id="1"
    )
    data = result.get_data()
    assert isinstance(data, dict), f"expected attachment reference, got {data}"
    stored = await registry.get_attachment(
        db, data["attachment_id"], acting_user_id="user-a"
    )
    assert stored is not None
    assert stored.mime_type == "application/pdf"
    assert stored.description == "Gmail attachment certificate.pdf"


@pytest.mark.asyncio
async def test_gmail_get_attachment_sniffs_type_when_part_is_unmatchable(
    db_engine: AsyncEngine,
) -> None:
    """With no part match at all, the bytes decide the type, not octet-stream."""
    content = b"%PDF-1.4\nQBE CTP certificate\n"
    payload = {"data": base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")}
    message = {
        "id": "msg-1",
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "body": {},
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "certificate.pdf",
                    "body": {"attachmentId": "att-rotated", "size": len(content)},
                }
            ],
        },
    }
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(
        routes={
            "token-a": {
                ("GET", "/attachments/att-original"): payload,
                ("GET", "/messages/msg-1"): message,
            }
        }
    )
    registry = _registry(db_engine)
    db = Database(engine=db_engine)
    context = _make_context(
        db,
        user_id="user-a",
        resolver=resolver,
        backend=backend,
        attachment_registry=registry,
    )
    result = await gmail_get_attachment_tool(
        context, message_id="msg-1", attachment_id="att-original"
    )
    data = result.get_data()
    assert isinstance(data, dict), f"expected attachment reference, got {data}"
    stored = await registry.get_attachment(
        db, data["attachment_id"], acting_user_id="user-a"
    )
    stored_path = await registry.resolve_attachment_path(
        data["attachment_id"], db, acting_user_id="user-a"
    )
    assert stored is not None
    assert stored.mime_type == "application/pdf"
    assert stored_path is not None
    assert stored_path.suffix == ".pdf"


@pytest.mark.asyncio
async def test_gmail_get_attachment_filename_cannot_reclassify_content(
    db_engine: AsyncEngine,
) -> None:
    """Neither the model's nor the sender's filename may reclassify content.

    The HTTP attachment route derives Content-Type from the storage path, so a
    PDF stored under "invoice.txt" would be served as text/plain. The caller
    argument survives only in the description, and even Gmail's own
    (sender-controlled) part filename is normalized to an extension matching
    the part's MIME type.
    """
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
                    # Sender-controlled name disagreeing with the part MIME.
                    "filename": "sender-chosen.txt",
                    "body": {"attachmentId": "att-1", "size": len(content)},
                }
            ],
        },
    }
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(
        routes={
            "token-a": {
                ("GET", "/attachments/att-1"): payload,
                ("GET", "/messages/msg-1"): message,
            }
        }
    )
    registry = _registry(db_engine)
    db = Database(engine=db_engine)
    context = _make_context(
        db,
        user_id="user-a",
        resolver=resolver,
        backend=backend,
        attachment_registry=registry,
    )
    result = await gmail_get_attachment_tool(
        context,
        message_id="msg-1",
        attachment_id="att-1",
        filename="invoice.txt",
    )
    data = result.get_data()
    assert isinstance(data, dict), f"expected attachment reference, got {data}"
    stored = await registry.get_attachment(
        db, data["attachment_id"], acting_user_id="user-a"
    )
    stored_path = await registry.resolve_attachment_path(
        data["attachment_id"], db, acting_user_id="user-a"
    )
    assert stored is not None
    assert stored.mime_type == "application/pdf"
    assert stored.description == "Gmail attachment invoice.txt"
    assert stored_path is not None
    assert stored_path.suffix == ".pdf"


# --------------------------------------------------------------------------- #
# drive_search scope fallback
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_drive_search_falls_back_to_metadata_scope(
    db_engine: AsyncEngine,
) -> None:
    resolver = FakeCredentialResolver(
        tokens={"user-a": "token-a"},
        granted_scopes={"user-a": {GoogleScope.DRIVE_METADATA_READONLY.value}},
    )
    backend = FakeApiBackend(
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
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
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
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    backend = FakeApiBackend(
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
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await drive_get_file_tool(context, file_id="doc-1")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["exported_as"] == "text/plain"
    assert "Exported plain text content." in data["content"]


@pytest.mark.asyncio
async def test_drive_get_file_inlines_small_text(db_engine: AsyncEngine) -> None:
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    # drive_get_file issues two GETs to /files/txt-1 (metadata then alt=media);
    # scripting the responses in order keeps them unambiguous.
    backend = FakeApiBackend(
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
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await drive_get_file_tool(context, file_id="txt-1")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["content"] == "hello inline text"


@pytest.mark.asyncio
async def test_drive_get_file_large_binary_goes_to_attachment(
    db_engine: AsyncEngine,
) -> None:
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    binary = b"\x00\x01\x02BINARY" * 100
    backend = FakeApiBackend(
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
    db = Database(engine=db_engine)
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
        await registry.get_attachment_content(db, attachment_id, acting_user_id=None)
        is None
    )


@pytest.mark.asyncio
async def test_drive_get_file_oversized_metadata_errors_without_media_request(
    db_engine: AsyncEngine,
) -> None:
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    oversized = 50 * 1024 * 1024
    backend = FakeApiBackend(
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
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await drive_get_file_tool(context, file_id="huge-1")

    text = result.get_text()
    assert "exceeds" in text.lower()
    assert str(oversized) in text
    # Only the metadata GET was issued — the alt=media download never ran.
    assert len(backend.requests) == 1
    assert backend.requests[0][1].endswith("/files/huge-1")


# --------------------------------------------------------------------------- #
# Scoped writes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gmail_create_draft_uses_only_drafts_create_and_owned_attachment(
    db_engine: AsyncEngine,
) -> None:
    registry = _registry(db_engine)
    backend = FakeApiBackend(
        scripted=[(200, b'{"id":"draft-1","message":{"id":"draft-message-1"}}')]
    )
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    await _store_google_connection(db)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"draft attachment",
        filename="details.txt",
        content_type="text/plain",
        tool_name="test",
        owner_user_id="user-a",
        db_context=db,
    )
    context = _make_context(
        db,
        user_id="user-a",
        resolver=resolver,
        backend=backend,
        attachment_registry=registry,
    )
    attachment_object = await fetch_attachment_object(attachment.attachment_id, context)
    assert attachment_object is not None
    result = await gmail_create_draft_tool(
        context,
        to=["recipient@example.com"],
        cc=["copy@example.com"],
        subject="Draft subject",
        body="Draft body",
        attachment_ids=[attachment_object],
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["draft_id"] == "draft-1"
    assert backend.requests == [
        (
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            "token-a",
        )
    ]
    request_body, content_type = backend.request_bodies[0]
    assert request_body is not None
    assert content_type == "application/json; charset=UTF-8"
    raw = json.loads(request_body)["message"]["raw"]
    parsed = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw)
    )
    assert parsed["From"] == "user-a@example.com"
    assert parsed["To"] == "recipient@example.com"
    assert parsed["Cc"] == "copy@example.com"
    assert parsed["Subject"] == "Draft subject"
    assert [part.get_filename() for part in parsed.iter_attachments()] == [
        "details.txt"
    ]


@pytest.mark.parametrize("invalid_address", ["not-an-address", "a@b@c"])
@pytest.mark.asyncio
async def test_gmail_create_draft_rejects_invalid_address_before_google_request(
    db_engine: AsyncEngine,
    invalid_address: str,
) -> None:
    backend = FakeApiBackend()
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    await _store_google_connection(db)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await gmail_create_draft_tool(
        context,
        to=[invalid_address],
        subject="Nope",
        body="Nope",
    )

    assert "invalid to email address" in result.get_text().lower()
    assert backend.requests == []


@pytest.mark.asyncio
async def test_gmail_create_draft_rejects_another_users_attachment(
    db_engine: AsyncEngine,
) -> None:
    registry = _registry(db_engine)
    backend = FakeApiBackend()
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    await _store_google_connection(db)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"private to user b",
        filename="private.txt",
        content_type="text/plain",
        tool_name="test",
        owner_user_id="user-b",
        db_context=db,
    )
    context = _make_context(
        db,
        user_id="user-a",
        resolver=resolver,
        backend=backend,
        attachment_registry=registry,
    )
    result = await gmail_create_draft_tool(
        context,
        to=["recipient@example.com"],
        subject="No leak",
        body="No leak",
        attachment_ids=[attachment.attachment_id],
    )

    assert "not found for the requesting user" in result.get_text().lower()
    assert backend.requests == []


@pytest.mark.asyncio
async def test_gmail_create_draft_rejects_attachment_filename_with_newline(
    db_engine: AsyncEngine,
) -> None:
    registry = _registry(db_engine)
    backend = FakeApiBackend()
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    await _store_google_connection(db)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"draft attachment",
        filename="unsafe\nname.txt",
        content_type="text/plain",
        tool_name="test",
        owner_user_id="user-a",
        db_context=db,
    )
    context = _make_context(
        db,
        user_id="user-a",
        resolver=resolver,
        backend=backend,
        attachment_registry=registry,
    )
    result = await gmail_create_draft_tool(
        context,
        to=["recipient@example.com"],
        subject="No invalid MIME headers",
        body="No invalid MIME headers",
        attachment_ids=[attachment.attachment_id],
    )

    assert "invalid filename" in result.get_text().lower()
    assert backend.requests == []


@pytest.mark.asyncio
async def test_drive_write_creates_app_folder_and_native_google_doc(
    db_engine: AsyncEngine,
) -> None:
    backend = FakeApiBackend(
        scripted=[
            (200, b'{"files":[]}'),
            (200, b'{"id":"folder-1","name":"Family Assistant"}'),
            (200, b'{"files":[]}'),
            (
                200,
                b'{"id":"doc-1","name":"Plan","mimeType":'
                b'"application/vnd.google-apps.document",'
                b'"webViewLink":"https://docs.google.com/document/d/doc-1"}',
            ),
        ]
    )
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await drive_write_file_tool(context, name="Plan", content="Family plan")

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["status"] == "created"
    assert data["file_id"] == "doc-1"
    assert [request[0] for request in backend.requests] == [
        "GET",
        "POST",
        "GET",
        "POST",
    ]
    assert backend.requests[-1][1] == (
        "https://www.googleapis.com/upload/drive/v3/files"
    )
    request_body, content_type = backend.request_bodies[-1]
    assert request_body is not None
    assert b'"parents":["folder-1"]' in request_body
    assert b'"mimeType":"application/vnd.google-apps.document"' in request_body
    assert b"Family plan" in request_body
    assert content_type is not None and content_type.startswith("multipart/related")


@pytest.mark.asyncio
async def test_parallel_drive_writes_create_only_one_app_folder(
    db_engine: AsyncEngine,
) -> None:
    backend = ConcurrentDriveApiBackend(
        routes={
            "token-a": {
                ("GET", "/files"): {"files": []},
                ("POST", "/files"): {
                    "id": "file-1",
                    "name": "Written file",
                    "mimeType": "application/vnd.google-apps.document",
                },
            }
        }
    )
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    first_write = asyncio.create_task(
        drive_write_file_tool(context, name="First", content="one")
    )
    await backend.first_folder_search_started.wait()
    second_write = asyncio.create_task(
        drive_write_file_tool(context, name="Second", content="two")
    )
    backend.release_first_folder_search.set()
    first_result, second_result = await asyncio.gather(first_write, second_write)

    assert first_result.get_data() is not None
    assert second_result.get_data() is not None
    assert backend.folder_create_count == 1
    folder_create_requests = [
        request
        for request, (body, _) in zip(
            backend.requests, backend.request_bodies, strict=True
        )
        if request[0] == "POST"
        and request[1] == "https://www.googleapis.com/drive/v3/files"
        and body is not None
        and b"google-apps.folder" in body
    ]
    assert len(folder_create_requests) == 1


@pytest.mark.asyncio
async def test_drive_write_uploads_owned_attachment_and_preserves_filename(
    db_engine: AsyncEngine,
) -> None:
    registry = _registry(db_engine)
    backend = FakeApiBackend(
        scripted=[
            (200, b'{"files":[{"id":"folder-1","name":"Renamed Folder"}]}'),
            (200, b'{"files":[]}'),
            (
                200,
                b'{"id":"file-1","name":"report.pdf","mimeType":"application/pdf"}',
            ),
        ]
    )
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"%PDF-owned-content",
        filename="report.pdf",
        content_type="application/pdf",
        tool_name="test",
        owner_user_id="user-a",
        db_context=db,
    )
    context = _make_context(
        db,
        user_id="user-a",
        resolver=resolver,
        backend=backend,
        attachment_registry=registry,
    )
    attachment_object = await fetch_attachment_object(attachment.attachment_id, context)
    assert attachment_object is not None
    result = await drive_write_file_tool(context, attachment_id=attachment_object)

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["name"] == "report.pdf"
    request_body, _ = backend.request_bodies[-1]
    assert request_body is not None
    assert b'"name":"report.pdf"' in request_body
    assert b"%PDF-owned-content" in request_body
    assert b"Content-Type: application/pdf" in request_body


@pytest.mark.asyncio
async def test_drive_write_refuses_existing_file_without_overwrite(
    db_engine: AsyncEngine,
) -> None:
    backend = FakeApiBackend(
        scripted=[
            (200, b'{"files":[{"id":"folder-1","name":"Family Assistant"}]}'),
            (
                200,
                b'{"files":[{"id":"doc-1","name":"Plan",'
                b'"mimeType":"application/vnd.google-apps.document"}]}',
            ),
        ]
    )
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await drive_write_file_tool(context, name="Plan", content="Replacement")

    assert "overwrite=true" in result.get_text()
    assert [request[0] for request in backend.requests] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_drive_write_overwrites_only_discovered_same_type_file(
    db_engine: AsyncEngine,
) -> None:
    backend = FakeApiBackend(
        scripted=[
            (200, b'{"files":[{"id":"folder-1","name":"Family Assistant"}]}'),
            (
                200,
                b'{"files":[{"id":"doc-1","name":"Plan",'
                b'"mimeType":"application/vnd.google-apps.document"}]}',
            ),
            (
                200,
                b'{"id":"doc-1","name":"Plan","mimeType":'
                b'"application/vnd.google-apps.document"}',
            ),
        ]
    )
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await drive_write_file_tool(
        context,
        name="Plan",
        content="Replacement",
        overwrite=True,
    )

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["status"] == "replaced"
    assert backend.requests[-1][0] == "PATCH"
    assert backend.requests[-1][1].endswith("/upload/drive/v3/files/doc-1")
    request_body, _ = backend.request_bodies[-1]
    assert request_body is not None
    assert b'"parents"' not in request_body
    assert b"Replacement" in request_body


@pytest.mark.asyncio
async def test_drive_write_rejects_content_over_multipart_limit(
    db_engine: AsyncEngine,
) -> None:
    backend = FakeApiBackend()
    resolver = FakeCredentialResolver(tokens={"user-a": "token-a"})
    db = Database(engine=db_engine)
    context = _make_context(db, user_id="user-a", resolver=resolver, backend=backend)
    result = await drive_write_file_tool(
        context,
        name="Too large",
        content="x" * (5 * 1024 * 1024 + 1),
    )

    assert "limited to 5242880 bytes" in result.get_text()
    assert backend.requests == []


# --------------------------------------------------------------------------- #
# Taint metadata
# --------------------------------------------------------------------------- #


_GOOGLE_TOOL_NAMES = [
    "gmail_search",
    "gmail_get_message",
    "gmail_get_attachment",
    "drive_search",
    "drive_get_file",
    "gmail_create_draft",
    "drive_write_file",
]

_GOOGLE_READ_TOOL_NAMES = _GOOGLE_TOOL_NAMES[:5]
_GOOGLE_WRITE_TOOL_NAMES = _GOOGLE_TOOL_NAMES[5:]


def _descriptor(name: str) -> ToolDescriptor:
    registration = next(r for r in LOCAL_TOOL_REGISTRATIONS if r.name == name)
    return build_tool_descriptor(
        registration.definition, registration.tags, origin="local"
    )


@pytest.mark.parametrize("tool_name", _GOOGLE_READ_TOOL_NAMES)
def test_google_tools_taint_unknown_external(tool_name: str) -> None:
    source = derive_tool_result_taint_source(
        descriptor=_descriptor(tool_name), call_id=None
    )
    assert source is not None
    assert source.tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.parametrize("tool_name", _GOOGLE_READ_TOOL_NAMES)
def test_google_tools_are_sensitive_read_broadening(tool_name: str) -> None:
    assert (
        resolve_tool_sink_class(_descriptor(tool_name))
        is SinkClass.SENSITIVE_READ_BROADENING
    )


@pytest.mark.parametrize("tool_name", _GOOGLE_WRITE_TOOL_NAMES)
def test_google_write_tools_are_trusted_artifact_writes(tool_name: str) -> None:
    descriptor = _descriptor(tool_name)
    assert derive_tool_result_taint_source(descriptor=descriptor, call_id=None) is None
    assert resolve_tool_sink_class(descriptor) is SinkClass.ARTIFACT_WRITE


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
    assert GOOGLE_TOOL_REQUIRED_SCOPES["gmail_create_draft"] == frozenset({
        GoogleScope.GMAIL_COMPOSE.value
    })
    assert GOOGLE_TOOL_REQUIRED_SCOPES["drive_write_file"] == frozenset({
        GoogleScope.DRIVE_FILE.value
    })
