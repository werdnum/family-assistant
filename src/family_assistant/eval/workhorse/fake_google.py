"""A Gmail and Drive :class:`ApiBackend` serving a :class:`GoogleWorld`.

It answers the REST paths the shipped Gmail and Drive tools call, in the
shapes Google returns, so the tools run unmodified above it. Reads are served
from the world; writes (drafts, uploads) are recorded and answered as Google
would, and never leave the process.

Most messages were only ever seen in a search listing, so their body was
never recorded. Fetching one serves its snippet as the body -- real text, cut
short -- and records it in :attr:`partial`, since a model that reads a
distractor should not void the trial. Anything else the world cannot answer
-- an unknown path, Drive content that was never recorded, attachment bytes
-- returns 404 and is recorded in :attr:`unbacked`, which the runner reads to
mark the trial unscorable rather than charge the model.

Search semantics live in :mod:`family_assistant.eval.workhorse.google_search`,
and are measured against the recorded traffic by replaying every recorded
search with its own evidence withheld.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from email.message import Message
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from family_assistant.eval.workhorse.fake_google_calendar import (
    CALENDAR_PREFIX,
    FakeGoogleCalendar,
)
from family_assistant.eval.workhorse.google_search import (
    drive_known_folders,
    drive_query_matches,
    gmail_query_matches,
)
from family_assistant.eval.workhorse.google_world import WorldFile
from family_assistant.services.api_backend import ApiResponse

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from family_assistant.eval.workhorse.google_world import (
        GoogleWorld,
        WorldMessage,
    )

__all__ = ["FakeGoogleBackend"]

_GMAIL_PREFIX = "/gmail/v1/users/me/"
_DRIVE_PREFIX = "/drive/v3/files"
_UPLOAD_PREFIX = "/upload/drive/v3/files"
_GOOGLE_DOC_TYPES = frozenset({
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
})
_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_APP_QUERY = "appProperties has"
_APP_FILE_NAME = re.compile(r"\bname\s*=\s*'((?:[^'\\]|\\.)*)'")


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _json(status: int, payload: object) -> ApiResponse:
    return ApiResponse(status_code=status, content=json.dumps(payload).encode("utf-8"))


def _not_found(reason: str) -> ApiResponse:
    return _json(404, {"error": {"code": 404, "message": reason}})


def _sent_sort_key(message: WorldMessage) -> datetime:
    return message.sent_at or message.first_seen_at


@dataclass
class _AppItem:
    """A folder or file the Drive write tool created during the trial."""

    id: str
    name: str
    mime_type: str
    content: str | None = None


def _app_link(item: _AppItem) -> str:
    if item.mime_type == _DRIVE_FOLDER_MIME:
        return f"https://drive.google.com/drive/folders/{item.id}"
    return f"https://drive.google.com/file/d/{item.id}/view?usp=drivesdk"


def _google_like_id(seed: str, length: int = 33) -> str:
    """A stable id shaped like Google's, so nothing in a result says "eval"."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    digest = hashlib.sha256(seed.encode()).digest()
    return "1" + "".join(alphabet[b % len(alphabet)] for b in digest)[: length - 1]


def _multipart_parts(content: bytes, content_type: str | None) -> tuple[dict, str]:
    """Split a Drive multipart/related upload into its metadata and media."""
    message = BytesParser(policy=default_policy).parsebytes(
        f"Content-Type: {content_type or 'multipart/related'}\r\n\r\n".encode()
        + content
    )
    parts = list(message.iter_parts()) if message.is_multipart() else []
    if len(parts) < 2:
        return {}, content.decode("utf-8", "replace")
    metadata = json.loads(_part_bytes(parts[0]) or b"{}")
    media = _part_bytes(parts[1]).decode("utf-8", "replace")
    return metadata, media


def _part_bytes(part: object) -> bytes:
    payload = part.get_payload(decode=True) if isinstance(part, Message) else None
    return payload if isinstance(payload, bytes) else b""


@dataclass
class FakeGoogleBackend:
    """Serves ``world`` as Gmail and Drive, at the moment ``now``."""

    world: GoogleWorld
    now: datetime
    # Recorded queries whose own evidence must not answer them: a replay of
    # a recorded search would otherwise be graded against itself.
    withheld: frozenset[str] = frozenset()
    # The user's own addresses, which Gmail's ``me`` stands for.
    user_addresses: tuple[str, ...] = ()
    # Google Calendar, when the trial has a calendar world.
    calendar: FakeGoogleCalendar | None = None
    unbacked: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    writes: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        visible = self.world.as_of(self.now)
        self._messages = {m.id: m for m in visible.messages}
        self._files = {f.id: f for f in visible.files}
        self._app_items: dict[str, _AppItem] = {}
        self._known_folders = drive_known_folders(visible.files, self.withheld)

    async def request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        del access_token, headers
        path = urlparse(url).path
        query = dict(params or {})
        if path.startswith(CALENDAR_PREFIX) and self.calendar is not None:
            response = self.calendar.handle(
                method, path[len(CALENDAR_PREFIX) :], query, content
            )
        elif path.startswith(_GMAIL_PREFIX):
            response = self._gmail(method, path[len(_GMAIL_PREFIX) :], query, content)
        elif path.startswith(_UPLOAD_PREFIX):
            response = self._drive_upload(method, path, content, content_type)
        elif path.startswith(_DRIVE_PREFIX):
            response = self._drive(method, path[len(_DRIVE_PREFIX) :], query, content)
        else:
            response = None
        if response is None:
            self.unbacked.append(f"{method} {path}")
            # Google's own wording, so the model cannot tell it is in a sandbox.
            return _not_found(
                f"File not found: {path.rsplit('/', 1)[-1]}."
                if path.startswith(_DRIVE_PREFIX)
                else "Requested entity was not found."
            )
        return response

    def _gmail(
        self,
        method: str,
        rest: str,
        query: Mapping[str, str],
        content: bytes | None,
    ) -> ApiResponse | None:
        parts = rest.strip("/").split("/")
        if method == "GET" and parts == ["messages"]:
            return self._gmail_list(query)
        if method == "GET" and len(parts) == 2 and parts[0] == "messages":
            return self._gmail_get(parts[1], query.get("format", "full"))
        if method == "POST" and parts == ["drafts"]:
            draft_id = (
                "r"
                + str(
                    int(
                        hashlib.sha256(
                            f"draft-{len(self.writes)}".encode()
                        ).hexdigest(),
                        16,
                    )
                )[:19]
            )
            self.writes.append({
                "kind": "gmail_draft",
                "id": draft_id,
                "body": (content or b"").decode("utf-8", "replace"),
            })
            return _json(
                200,
                {"id": draft_id, "message": {"id": f"{draft_id}-message"}},
            )
        return None

    def _gmail_list(self, query: Mapping[str, str]) -> ApiResponse:
        limit = int(query.get("maxResults", "10"))
        q = query.get("q", "")
        unmodelled: set[str] = set()
        matches = sorted(
            (
                m
                for m in self._messages.values()
                if gmail_query_matches(
                    m,
                    q,
                    self.now,
                    self.withheld,
                    me=self.user_addresses,
                    unmodelled=unmodelled,
                )
            ),
            key=_sent_sort_key,
            reverse=True,
        )
        self.partial.extend(
            f"unmodelled search term {term!r}" for term in sorted(unmodelled)
        )
        payload: dict[str, object] = {
            "messages": [
                {"id": m.id, "threadId": m.thread_id} for m in matches[:limit]
            ],
            "resultSizeEstimate": len(matches),
        }
        if len(matches) > limit:
            payload["nextPageToken"] = "eval-more"
        return _json(200, payload)

    def _gmail_get(self, message_id: str, fmt: str) -> ApiResponse | None:
        message = self._messages.get(message_id)
        if message is None:
            return None
        headers = [
            {"name": name, "value": value}
            for name, value in (
                ("From", message.from_),
                ("To", message.to),
                ("Subject", message.subject),
                ("Date", message.date_header),
            )
            if value is not None
        ]
        payload: dict[str, object] = {"mimeType": "multipart/mixed", "headers": headers}
        if fmt == "full":
            body = message.body
            if body is None:
                self.partial.append(f"gmail message {message.id}: snippet only")
                # Snippets end mid-sentence; the ellipsis says so, as a
                # truncated preview would.
                body = f"{message.snippet or ''} …"
            payload["parts"] = [
                {
                    "partId": "0",
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"data": _b64url(body)},
                },
                *(
                    {
                        "partId": a.part_id,
                        "mimeType": a.mime_type,
                        "filename": a.filename,
                        "body": {"attachmentId": a.attachment_id, "size": a.size},
                    }
                    for a in message.attachments
                ),
            ]
        return _json(
            200,
            {
                "id": message.id,
                "threadId": message.thread_id,
                "snippet": message.snippet or "",
                "payload": payload,
            },
        )

    def _drive(
        self,
        method: str,
        rest: str,
        query: Mapping[str, str],
        content: bytes | None,
    ) -> ApiResponse | None:
        parts = [part for part in rest.strip("/").split("/") if part]
        if method == "POST" and not parts:
            # Metadata-only create: the write tool making its app folder.
            metadata = json.loads(content or b"{}")
            item = self._create_app_item(
                str(metadata.get("name", "untitled")),
                str(metadata.get("mimeType", _DRIVE_FOLDER_MIME)),
                None,
            )
            return _json(200, self._app_resource(item))
        if method != "GET":
            return None
        if not parts:
            if _APP_QUERY in query.get("q", ""):
                return self._drive_app_list(query.get("q", ""))
            return self._drive_list(query)
        app_item = self._app_items.get(parts[0])
        if app_item is not None:
            return self._drive_app_get(app_item, parts, query)
        file = self._files.get(parts[0])
        if file is None:
            return None
        if len(parts) == 2 and parts[1] == "export":
            if file.content is None:
                return None
            return ApiResponse(status_code=200, content=file.content.encode("utf-8"))
        if len(parts) != 1:
            return None
        if query.get("alt") == "media":
            if file.content is None:
                return None
            return ApiResponse(status_code=200, content=file.content.encode("utf-8"))
        meta: dict[str, object] = {
            "id": file.id,
            "name": file.name,
            "mimeType": file.mime_type,
        }
        if file.mime_type not in _GOOGLE_DOC_TYPES and file.content is not None:
            meta["size"] = str(len(file.content.encode("utf-8")))
        return _json(200, meta)

    def _drive_list(self, query: Mapping[str, str]) -> ApiResponse:
        limit = int(query.get("pageSize", "10"))
        q = query.get("q", "")
        unmodelled: set[str] = set()
        # Files the trial created are searchable like any other.
        created = [
            WorldFile(
                id=item.id,
                name=item.name,
                mime_type=item.mime_type,
                owner_name=None,
                owner_email=None,
                modified_time=self.now,
                web_view_link=_app_link(item),
                content=item.content,
                first_seen_at=self.now,
            )
            for item in self._app_items.values()
            if item.mime_type != _DRIVE_FOLDER_MIME
        ]
        matches = sorted(
            (
                f
                for f in [*self._files.values(), *created]
                if drive_query_matches(
                    f,
                    q,
                    self.withheld,
                    known_folders=self._known_folders,
                    unmodelled=unmodelled,
                )
            ),
            key=lambda f: f.modified_time or f.first_seen_at,
            reverse=True,
        )
        self.partial.extend(
            f"unmodelled search term {term!r}" for term in sorted(unmodelled)
        )
        payload: dict[str, object] = {
            "files": [
                {
                    "id": f.id,
                    "name": f.name,
                    "mimeType": f.mime_type,
                    "owners": [
                        {"displayName": f.owner_name, "emailAddress": f.owner_email}
                    ],
                    "modifiedTime": f.modified_time.isoformat()
                    if f.modified_time
                    else None,
                    "webViewLink": f.web_view_link,
                }
                for f in matches[:limit]
            ]
        }
        if len(matches) > limit:
            payload["nextPageToken"] = "eval-more"
        return _json(200, payload)

    def _create_app_item(
        self, name: str, mime_type: str, content: str | None
    ) -> _AppItem:
        item = _AppItem(
            id=_google_like_id(f"{name}-{len(self._app_items)}"),
            name=name,
            mime_type=mime_type,
            content=content,
        )
        self._app_items[item.id] = item
        self.writes.append({
            "kind": "drive_create",
            "id": item.id,
            "name": name,
            "mime_type": mime_type,
            "content": content,
        })
        return item

    @staticmethod
    def _app_resource(item: _AppItem) -> dict[str, object]:
        return {
            "id": item.id,
            "name": item.name,
            "mimeType": item.mime_type,
            "webViewLink": _app_link(item),
        }

    def _drive_app_list(self, q: str) -> ApiResponse:
        """The write tool's lookups: its folder, or a named file inside it."""
        if _DRIVE_FOLDER_MIME in q:
            items = [
                i for i in self._app_items.values() if i.mime_type == _DRIVE_FOLDER_MIME
            ]
        else:
            match = _APP_FILE_NAME.search(q)
            name = match[1].replace("\\'", "'") if match else None
            items = [
                i
                for i in self._app_items.values()
                if i.mime_type != _DRIVE_FOLDER_MIME and i.name == name
            ]
        return _json(200, {"files": [self._app_resource(i) for i in items]})

    def _drive_app_get(
        self, item: _AppItem, parts: list[str], query: Mapping[str, str]
    ) -> ApiResponse | None:
        if len(parts) == 2 and parts[1] == "export" or query.get("alt") == "media":
            if item.content is None:
                return None
            return ApiResponse(status_code=200, content=item.content.encode("utf-8"))
        if len(parts) != 1:
            return None
        return _json(200, self._app_resource(item))

    def _drive_upload(
        self,
        method: str,
        path: str,
        content: bytes | None,
        content_type: str | None,
    ) -> ApiResponse | None:
        metadata, media = _multipart_parts(content or b"", content_type)
        if method == "POST" and path.rstrip("/") == _UPLOAD_PREFIX:
            item = self._create_app_item(
                str(metadata.get("name", "untitled")),
                str(metadata.get("mimeType", "text/plain")),
                media,
            )
            return _json(200, self._app_resource(item))
        if method == "PATCH":
            item = self._app_items.get(path.rsplit("/", 1)[-1])
            if item is None:
                return None
            item.content = media
            self.writes.append({
                "kind": "drive_replace",
                "id": item.id,
                "content": media,
            })
            return _json(200, self._app_resource(item))
        return None
