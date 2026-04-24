"""Regression coverage for ``select_email_attachment_base`` fallback semantics."""

from __future__ import annotations

from family_assistant.config_models import AppConfig
from family_assistant.web.routers.webhooks import (
    DEFAULT_ATTACHMENT_STORAGE_PATH,
    select_email_attachment_base,
)


def test_with_app_config_uses_relative_persist_path() -> None:
    """When the app config supplies a base, persist the relative path.

    The runtime registry knows the same base and rejoins it at read time,
    so portable relative paths are safe.
    """
    config = AppConfig(attachment_storage_path="/deploy/mailbox/attachments")

    base, persist_absolute = select_email_attachment_base(config)

    assert base == "/deploy/mailbox/attachments"
    assert persist_absolute is False


def test_without_app_config_falls_back_to_default_and_persists_absolute() -> None:
    """When no app config is attached to the request, persist absolute paths.

    The registry that later reads the attachment may be configured with a
    different ``email_attachment_base_path`` than the fallback we wrote
    to — if we persisted relative, the read-time join would land in the
    wrong directory and 404.
    """
    base, persist_absolute = select_email_attachment_base(None)

    assert base == DEFAULT_ATTACHMENT_STORAGE_PATH
    assert persist_absolute is True


def test_app_config_with_empty_storage_path_falls_back() -> None:
    """Empty string in ``attachment_storage_path`` is treated as missing."""
    config = AppConfig(attachment_storage_path="")

    base, persist_absolute = select_email_attachment_base(config)

    assert base == DEFAULT_ATTACHMENT_STORAGE_PATH
    assert persist_absolute is True
