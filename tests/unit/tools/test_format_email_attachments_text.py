"""Unit coverage for ``format_email_attachments_text`` branches.

The formatter's wording determines what the LLM is told to do about
email attachments surfaced via ``get_full_document_content``. The
branches have different operator/caller implications — getting them
wrong either sends the model chasing unavailable tools or has it
advertise ids that can't be dereferenced.
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


def test_registry_available_with_id_surfaces_attachment_id() -> None:
    """Happy path: attachment is registered, model gets the id."""
    text = format_email_attachments_text(
        [_summary(attachment_id="att-123")], registry_available=True
    )
    assert "attachment_id: att-123" in text
    assert "reindex_email" not in text
    assert "AttachmentRegistry" not in text


def test_registry_available_without_id_asks_for_reindex() -> None:
    """Legacy email: registry exists but row not yet registered → suggest reindex."""
    text = format_email_attachments_text(
        [_summary(attachment_id=None)], registry_available=True
    )
    assert "reindex_email" in text
    assert "attachment_id not yet assigned" in text


def test_registry_unavailable_never_surfaces_id() -> None:
    """Without a registry, ``read_text_attachment`` / ``get_attachment_info``
    can't dereference the id, so the formatter must not advertise it even
    when ``attachment_id`` is present on the row.
    """
    text = format_email_attachments_text(
        [_summary(attachment_id="att-456")], registry_available=False
    )
    assert "att-456" not in text
    assert "AttachmentRegistry" in text
    assert "operator" in text.lower()


def test_registry_unavailable_without_id_surfaces_operator_guidance() -> None:
    """Same operator-action guidance applies when there's no id either."""
    text = format_email_attachments_text(
        [_summary(attachment_id=None)], registry_available=False
    )
    assert "AttachmentRegistry" in text
    assert "operator" in text.lower()
    assert "reindex_email" not in text
