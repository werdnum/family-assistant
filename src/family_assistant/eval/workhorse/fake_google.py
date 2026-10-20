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

Search is deliberately lenient. Gmail and Drive query languages are only
approximated: every free term must appear somewhere in the item, the common
operators narrow the match, and operators the fake does not model are
ignored. A model is never refused for writing a query the real service would
accept; the cost is that some queries match more than they would at Google.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from email.message import Message
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from family_assistant.services.api_backend import ApiResponse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.eval.workhorse.google_world import (
        GoogleWorld,
        WorldFile,
        WorldMessage,
    )

__all__ = ["FakeGoogleBackend", "drive_query_matches", "gmail_query_matches"]

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
_RELATIVE_AGE = re.compile(r"^(\d+)([dmy])$")
_DRIVE_CONTAINS = re.compile(
    r"\b(name|fullText)\s+contains\s+'((?:[^'\\]|\\.)*)'", re.IGNORECASE
)
_DRIVE_MIME = re.compile(r"\bmimeType\s*(=|!=)\s*'((?:[^'\\]|\\.)*)'", re.IGNORECASE)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _json(status: int, payload: object) -> ApiResponse:
    return ApiResponse(status_code=status, content=json.dumps(payload).encode("utf-8"))


def _not_found(reason: str) -> ApiResponse:
    return _json(404, {"error": {"code": 404, "message": reason}})


def _message_text(message: WorldMessage) -> str:
    return " ".join(
        part or ""
        for part in (
            message.subject,
            message.from_,
            message.to,
            message.snippet,
            message.body,
        )
    ).lower()


def _gmail_date(value: str, tz: tzinfo | None) -> datetime | None:
    """Parse Gmail's ``YYYY/MM/DD`` (or dashed) date as midnight in ``tz``."""
    pieces = re.split(r"[/-]", value)
    if len(pieces) != 3 or not all(piece.isdigit() for piece in pieces):
        return None
    year, month, day = (int(piece) for piece in pieces)
    try:
        return datetime(year, month, day, tzinfo=tz or UTC)
    except ValueError:
        return None


def _term_matches(message: WorldMessage, term: str, now: datetime) -> bool | None:
    """Whether one query term holds for ``message``; ``None`` when not modelled."""
    if ":" in term:
        operator, _, value = term.partition(":")
        operator = operator.lower()
        value = value.strip('"').lower()
        sent = message.sent_at or message.first_seen_at
        if operator == "from":
            return value in (message.from_ or "").lower()
        if operator in {"to", "cc", "deliveredto"}:
            return value in (message.to or "").lower()
        if operator == "subject":
            return value in (message.subject or "").lower()
        if operator in {"after", "before"}:
            bound = _gmail_date(value, sent.tzinfo)
            if bound is None:
                return None
            return sent >= bound if operator == "after" else sent < bound
        if operator in {"newer_than", "older_than"}:
            match = _RELATIVE_AGE.match(value)
            if match is None:
                return None
            days = int(match[1]) * {"d": 1, "m": 30, "y": 365}[match[2]]
            cutoff = now - timedelta(days=days)
            return sent >= cutoff if operator == "newer_than" else sent < cutoff
        if operator == "has" and value == "attachment":
            return bool(message.attachments)
        if operator == "filename":
            return any(value in a.filename.lower() for a in message.attachments)
        # in:, is:, label:, category: and the rest are not modelled, and are
        # skipped whether or not they are negated.
        return None
    return term.strip('"').lower() in _message_text(message)


def gmail_query_matches(message: WorldMessage, query: str, now: datetime) -> bool:
    """Approximate Gmail search: OR-separated groups of ANDed terms."""
    try:
        tokens = shlex.split(query.replace("(", " ").replace(")", " "), posix=True)
    except ValueError:
        tokens = query.split()
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token == "OR":
            groups.append([])
        elif token and token not in {"AND", "{", "}"}:
            groups[-1].append(token.strip("{}"))
    for group in groups:
        holds = True
        for term in group:
            negated = term.startswith("-") and len(term) > 1
            result = _term_matches(message, term[1:] if negated else term, now)
            if result is not None and result == negated:
                holds = False
                break
        if holds:
            return True
    return False


def drive_query_matches(file: WorldFile, query: str) -> bool:
    """Approximate Drive search over ``name``/``fullText contains`` and ``mimeType``."""
    name = file.name.lower()
    full_text = f"{name} {(file.content or '').lower()}"
    checks: list[bool] = []
    for field_name, raw in _DRIVE_CONTAINS.findall(query):
        value = raw.replace("\\'", "'").lower()
        checks.append(value in (name if field_name.lower() == "name" else full_text))
    for operator, raw in _DRIVE_MIME.findall(query):
        same = file.mime_type == raw.replace("\\'", "'")
        checks.append(same if operator == "=" else not same)
    if not checks:
        return True
    if re.search(r"\bor\b", query, re.IGNORECASE):
        return any(checks)
    return all(checks)


def _sent_sort_key(message: WorldMessage) -> datetime:
    return message.sent_at or message.first_seen_at


@dataclass
class _AppItem:
    """A folder or file the Drive write tool created during the trial."""

    id: str
    name: str
    mime_type: str
    content: str | None = None


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
    unbacked: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    writes: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        visible = self.world.as_of(self.now)
        self._messages = {m.id: m for m in visible.messages}
        self._files = {f.id: f for f in visible.files}
        self._app_items: dict[str, _AppItem] = {}

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
        del access_token
        path = urlparse(url).path
        query = dict(params or {})
        if path.startswith(_GMAIL_PREFIX):
            response = self._gmail(method, path[len(_GMAIL_PREFIX) :], query, content)
        elif path.startswith(_UPLOAD_PREFIX):
            response = self._drive_upload(method, path, content, content_type)
        elif path.startswith(_DRIVE_PREFIX):
            response = self._drive(method, path[len(_DRIVE_PREFIX) :], query, content)
        else:
            response = None
        if response is None:
            self.unbacked.append(f"{method} {path}")
            return _not_found(f"{path} is not served by the eval's Google world.")
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
            draft_id = f"eval-draft-{len(self.writes) + 1}"
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
        matches = sorted(
            (m for m in self._messages.values() if gmail_query_matches(m, q, self.now)),
            key=_sent_sort_key,
            reverse=True,
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
                body = message.snippet or ""
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
        matches = sorted(
            (f for f in self._files.values() if drive_query_matches(f, q)),
            key=lambda f: f.modified_time or f.first_seen_at,
            reverse=True,
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
            id=f"eval-drive-{len(self._app_items) + 1}",
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
            "webViewLink": f"https://drive.google.com/eval/{item.id}",
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
