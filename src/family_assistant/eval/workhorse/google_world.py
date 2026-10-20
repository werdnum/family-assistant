"""The Gmail and Drive a workhorse case sees, rebuilt from recorded tool results.

The snapshot holds no mailbox and no Drive, but it does hold every Gmail and
Drive result a historical turn received. Pooled across the whole history they
make a small, real world: the messages and files the household actually looked
at, each one the needle for some case and a distractor for every other. A
case sees the part of it that existed at the moment it was asked.

What the record cannot supply stays visibly absent rather than invented. A
message only ever seen in a search listing has a snippet and no body; a Drive
file only ever listed has no content; attachment bytes were never recorded.
The fake serving this world says which of these a trial ran into (see
:mod:`family_assistant.eval.workhorse.fake_google`), so a trial that depends
on one is scored as a harness gap, not as a model error.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from family_assistant.storage.types import MessageHistoryRow

__all__ = [
    "GoogleWorld",
    "WorldAttachment",
    "WorldFile",
    "WorldMessage",
    "build_google_world",
]

_HEADER_LINE = re.compile(r"^(From|To|Subject|Date): ?(.*)$")
_ATTACHMENT_ENTRY = re.compile(
    r"(?P<filename>[^,(]+?) \(attachment_id: (?P<attachment_id>[^,]*), "
    r"part_id: (?P<part_id>[^,]*), (?P<mime_type>[^,]*), (?P<size>\w+) bytes\)"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorldAttachment(_Model):
    filename: str
    attachment_id: str
    part_id: str
    mime_type: str
    size: int | None


class WorldMessage(_Model):
    id: str
    thread_id: str
    sent_at: datetime | None
    date_header: str | None
    from_: str | None = None
    to: str | None
    subject: str | None
    snippet: str | None
    # None when the message was only ever seen in a search listing.
    body: str | None = None
    attachments: list[WorldAttachment] = []
    first_seen_at: datetime


class WorldFile(_Model):
    id: str
    name: str
    mime_type: str
    owner_name: str | None
    owner_email: str | None
    modified_time: datetime | None
    web_view_link: str | None
    # None when the file was only ever listed, or fetched into an attachment.
    content: str | None = None
    first_seen_at: datetime


class GoogleWorld(_Model):
    messages: list[WorldMessage]
    files: list[WorldFile]

    def as_of(self, moment: datetime) -> GoogleWorld:
        """The part of the world that existed at ``moment``.

        An item is in it when its own date (sent, or last modified) is not
        after ``moment``. An item without a usable date falls back to when a
        turn first saw it.
        """
        return GoogleWorld(
            messages=[
                m for m in self.messages if (m.sent_at or m.first_seen_at) <= moment
            ],
            files=[
                f for f in self.files if (f.modified_time or f.first_seen_at) <= moment
            ],
        )


def _parse_rfc2822(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _arguments(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _json_object(content: str) -> dict[str, object] | None:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _parse_rendered_message(
    content: str,
) -> tuple[dict[str, str], str, list[WorldAttachment]]:
    """Split ``gmail_get_message``'s rendering back into headers, body, attachments."""
    lines = content.split("\n")
    headers: dict[str, str] = {}
    index = 0
    while index < len(lines) and (match := _HEADER_LINE.match(lines[index])):
        headers[match.group(1).lower()] = match.group(2)
        index += 1
    body = "\n".join(lines[index + 1 :])
    attachments: list[WorldAttachment] = []
    marker = body.rfind("\nAttachments: ")
    if marker != -1:
        listing = body[marker + len("\nAttachments: ") :]
        attachments = [
            WorldAttachment(
                filename=match["filename"].strip(),
                attachment_id=match["attachment_id"],
                part_id=match["part_id"],
                mime_type=match["mime_type"],
                size=int(match["size"]) if match["size"].isdigit() else None,
            )
            for match in _ATTACHMENT_ENTRY.finditer(listing)
        ]
        body = body[:marker]
    return headers, body.rstrip("\n"), attachments


class _Builder:
    def __init__(self) -> None:
        self.messages: dict[str, WorldMessage] = {}
        self.files: dict[str, WorldFile] = {}

    def add_search_listing(self, data: Mapping[str, object], seen: datetime) -> None:
        entries = data.get("messages")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            message_id = entry["id"]
            existing = self.messages.get(message_id)
            summary = WorldMessage(
                id=message_id,
                thread_id=_str(entry.get("thread_id")) or message_id,
                sent_at=_parse_rfc2822(entry.get("date")),
                date_header=_str(entry.get("date")),
                from_=_str(entry.get("from")),
                to=_str(entry.get("to")),
                subject=_str(entry.get("subject")),
                snippet=_str(entry.get("snippet")),
                first_seen_at=seen,
            )
            if existing is None:
                self.messages[message_id] = summary
            else:
                self.messages[message_id] = existing.model_copy(
                    update={
                        "snippet": existing.snippet or summary.snippet,
                        "first_seen_at": min(existing.first_seen_at, seen),
                    }
                )

    def add_full_message(self, message_id: str, content: str, seen: datetime) -> None:
        headers, body, attachments = _parse_rendered_message(content)
        existing = self.messages.get(message_id)
        if existing is None:
            self.messages[message_id] = WorldMessage(
                id=message_id,
                thread_id=message_id,
                sent_at=_parse_rfc2822(headers.get("date")),
                date_header=headers.get("date"),
                from_=headers.get("from"),
                to=headers.get("to"),
                subject=headers.get("subject"),
                snippet=" ".join(body.split())[:200],
                body=body,
                attachments=attachments,
                first_seen_at=seen,
            )
            return
        self.messages[message_id] = existing.model_copy(
            update={
                "body": body,
                "attachments": attachments,
                "first_seen_at": min(existing.first_seen_at, seen),
            }
        )

    def add_drive_listing(self, data: Mapping[str, object], seen: datetime) -> None:
        entries = data.get("files")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            owner = entry.get("owner") if isinstance(entry.get("owner"), dict) else {}
            assert isinstance(owner, dict)
            file_id = entry["id"]
            existing = self.files.get(file_id)
            listed = WorldFile(
                id=file_id,
                name=_str(entry.get("name")) or file_id,
                mime_type=_str(entry.get("mime_type")) or "application/octet-stream",
                owner_name=_str(owner.get("display_name")),
                owner_email=_str(owner.get("email")),
                modified_time=_parse_iso(entry.get("modified_time")),
                web_view_link=_str(entry.get("web_view_link")),
                first_seen_at=seen,
            )
            if existing is None:
                self.files[file_id] = listed
            else:
                self.files[file_id] = existing.model_copy(
                    update={"first_seen_at": min(existing.first_seen_at, seen)}
                )

    def add_file_content(self, file_id: str, content: str, seen: datetime) -> None:
        existing = self.files.get(file_id)
        if existing is None:
            # Fetched without ever being listed: keep the content, name it by id.
            self.files[file_id] = WorldFile(
                id=file_id,
                name=file_id,
                mime_type="text/plain",
                owner_name=None,
                owner_email=None,
                modified_time=None,
                web_view_link=None,
                content=content,
                first_seen_at=seen,
            )
            return
        self.files[file_id] = existing.model_copy(update={"content": content})


def _is_tool_failure(content: str) -> bool:
    return content.startswith("Error")


def build_google_world(
    grouped: Mapping[tuple[str, str], Iterable[MessageHistoryRow]],
) -> GoogleWorld:
    """Pool every recorded Gmail and Drive result into one world."""
    builder = _Builder()
    for rows in grouped.values():
        calls: dict[str, tuple[str, dict[str, object]]] = {}
        results: list[MessageHistoryRow] = []
        for row in rows:
            if row["role"] == "assistant":
                for item in row["tool_calls"] or []:
                    calls[item.id] = (
                        item.function.name,
                        _arguments(item.function.arguments),
                    )
            elif row["role"] == "tool" and row["tool_call_id"] and row["content"]:
                results.append(row)
        for row in results:
            name, arguments = calls.get(
                row["tool_call_id"] or "", (row["tool_name"], {})
            )
            content = row["content"] or ""
            seen = row["timestamp"]
            if _is_tool_failure(content):
                continue
            if name == "gmail_search" and (data := _json_object(content)) is not None:
                builder.add_search_listing(data, seen)
            elif name == "gmail_get_message" and isinstance(
                arguments.get("message_id"), str
            ):
                builder.add_full_message(str(arguments["message_id"]), content, seen)
            elif name == "drive_search" and (data := _json_object(content)) is not None:
                builder.add_drive_listing(data, seen)
            elif (
                name == "drive_get_file"
                and isinstance(arguments.get("file_id"), str)
                and not content.startswith("Stored ")
            ):
                builder.add_file_content(str(arguments["file_id"]), content, seen)
    return GoogleWorld(
        messages=sorted(
            builder.messages.values(),
            key=lambda m: (m.sent_at or m.first_seen_at, m.id),
        ),
        files=sorted(builder.files.values(), key=lambda f: f.id),
    )
