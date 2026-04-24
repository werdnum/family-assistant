"""Unit coverage for ``format_email_attachments_text``.

Drives the two rendering branches — ``attachment_id`` present vs legacy
email needing reindex — so the LLM-visible wording in
``get_full_document_content`` stays stable.
"""

from __future__ import annotations

from family_assistant.tools.documents import (
    EmailAttachmentSummary,
    format_email_attachments_text,
)


def _summary(
    *,
    filename: str = "report.pdf",
    mime_type: str = "application/pdf",
    size: int | None = 1024,
    attachment_id: str | None = None,
) -> EmailAttachmentSummary:
    return {
        "attachment_id": attachment_id,
        "filename": filename,
        "mime_type": mime_type,
        "size": size,
    }


def test_attachment_with_id_surfaces_attachment_id() -> None:
    """Happy path: the model gets the id so it can chain into read_text_attachment."""
    text = format_email_attachments_text([_summary(attachment_id="att-123")])

    assert "attachment_id: att-123" in text
    assert "reindex_email" not in text


def test_attachment_without_id_emits_tool_agnostic_reindex_guidance() -> None:
    """Legacy email: no id yet → describe the action without prescribing
    a tool that some profiles don't have.
    """
    text = format_email_attachments_text([_summary(attachment_id=None)])

    assert "attachment_id not yet assigned" in text
    # The text mentions reindex_email by name but explicitly as "if it is
    # available in this profile" — not a hard prescription.
    assert "if it is available in this profile" in text


def test_multiple_attachments_render_distinct_lines() -> None:
    """Each attachment gets its own line so the caller can read them individually."""
    text = format_email_attachments_text([
        _summary(filename="first.pdf", attachment_id="att-1"),
        _summary(filename="second.jpg", mime_type="image/jpeg", attachment_id=None),
    ])

    lines = text.splitlines()
    assert len(lines) == 2
    assert "first.pdf" in lines[0]
    assert "att-1" in lines[0]
    assert "second.jpg" in lines[1]
    assert "attachment_id not yet assigned" in lines[1]


def test_unknown_size_renders_as_unknown_size() -> None:
    """``size=None`` on legacy rows renders as ``unknown size``, not ``None bytes``."""
    text = format_email_attachments_text([_summary(size=None, attachment_id="att-9")])

    assert "unknown size" in text
    assert "None bytes" not in text
