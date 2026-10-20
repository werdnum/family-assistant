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


def _term_matches(message: WorldMessage, term: str, now: datetime) -> bool:
    """Whether one query term holds for ``message``."""
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
                return True
            return sent >= bound if operator == "after" else sent < bound
        if operator in {"newer_than", "older_than"}:
            match = _RELATIVE_AGE.match(value)
            if match is None:
                return True
            days = int(match[1]) * {"d": 1, "m": 30, "y": 365}[match[2]]
            cutoff = now - timedelta(days=days)
            return sent >= cutoff if operator == "newer_than" else sent < cutoff
        if operator == "has" and value == "attachment":
            return bool(message.attachments)
        if operator == "filename":
            return any(value in a.filename.lower() for a in message.attachments)
        # in:, is:, label:, category: and the rest are not modelled.
        return True
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
        if all(
            not _term_matches(message, term[1:], now)
            if term.startswith("-") and len(term) > 1
            else _term_matches(message, term, now)
            for term in group
        ):
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
            response = self._drive(method, path[len(_DRIVE_PREFIX) :], query)
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
        self, method: str, rest: str, query: Mapping[str, str]
    ) -> ApiResponse | None:
        parts = [part for part in rest.strip("/").split("/") if part]
        if method != "GET":
            return None
        if not parts:
            return self._drive_list(query)
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

    def _drive_upload(
        self,
        method: str,
        path: str,
        content: bytes | None,
        content_type: str | None,
    ) -> ApiResponse:
        file_id = path.rsplit("/", 1)[-1] if path != _UPLOAD_PREFIX else None
        new_id = file_id or f"eval-file-{len(self.writes) + 1}"
        self.writes.append({
            "kind": "drive_upload",
            "method": method,
            "id": new_id,
            "content_type": content_type,
            "bytes": len(content or b""),
        })
        return _json(200, {"id": new_id, "name": new_id, "mimeType": "text/plain"})
