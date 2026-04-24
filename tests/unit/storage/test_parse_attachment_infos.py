"""Unit coverage for per-entry ``parse_attachment_infos`` tolerance."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from family_assistant.storage.email import (
    AttachmentData,
    parse_attachment_infos,
)

if TYPE_CHECKING:
    import pytest


def test_parse_attachment_infos_skips_malformed_entries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed entries are logged and skipped; valid entries flow through."""
    raw_entries = [
        {
            "filename": "good-first.pdf",
            "content_type": "application/pdf",
            "size": 10,
            "storage_path": "batch-1/good-first.pdf",
        },
        # Missing ``storage_path`` — should be skipped, not raise.
        {
            "filename": "no-path.png",
            "content_type": "image/png",
            "size": 5,
        },
        # ``content_type`` is None — schema requires str, skip.
        {
            "filename": "bad-mime.txt",
            "content_type": None,
            "size": 3,
            "storage_path": "batch-1/bad-mime.txt",
        },
        # Wrong outer type entirely.
        "not-a-dict",
        {
            "filename": "good-last.jpg",
            "content_type": "image/jpeg",
            "size": 20,
            "storage_path": "batch-1/good-last.jpg",
        },
    ]

    with caplog.at_level(logging.WARNING, logger="family_assistant.storage.email"):
        result = parse_attachment_infos(raw_entries, context="test-email")

    assert [att.filename for att in result] == ["good-first.pdf", "good-last.jpg"]
    assert all(isinstance(att, AttachmentData) for att in result)

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 3
    assert all("test-email" in msg for msg in warnings)


def test_parse_attachment_infos_handles_empty_and_none() -> None:
    """Empty list and ``None`` return empty results without raising."""
    assert parse_attachment_infos([], context="ctx") == []
    assert parse_attachment_infos(None, context="ctx") == []
