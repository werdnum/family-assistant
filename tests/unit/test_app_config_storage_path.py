"""Regression coverage for ``AppConfig`` storage-path normalization.

A relative ``attachment_storage_path`` in YAML must anchor to the config
file's directory, not the process's cwd — otherwise a restart from a
different working directory silently re-anchors the mailbox root and
every persisted relative email ``storage_path`` points to the wrong
place.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from family_assistant.config_models import AppConfig

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def _cwd(target: Path) -> Iterator[Path]:
    """Temporarily chdir to ``target`` for the duration of the block."""
    original = Path.cwd()
    os.chdir(target)
    try:
        yield target
    finally:
        os.chdir(original)


def _load(yaml_files: list[str]) -> AppConfig:
    """Mirror of the production build path used by ``config_loader``."""
    with AppConfig.yaml_source_context(yaml_files):
        return AppConfig()


def test_relative_attachment_storage_path_anchors_to_config_file_dir(
    tmp_path: Path,
) -> None:
    """A relative ``attachment_storage_path`` resolves to the same absolute
    path regardless of the cwd the process was started from.
    """
    config_dir = tmp_path / "deploy"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "attachment_storage_path: ./mailbox\ndocument_storage_path: ./docs\n",
        encoding="utf-8",
    )

    other_cwd_a = tmp_path / "cwd_a"
    other_cwd_a.mkdir()
    other_cwd_b = tmp_path / "cwd_b"
    other_cwd_b.mkdir()

    with _cwd(other_cwd_a):
        cfg_from_a = _load([str(config_file)])
    with _cwd(other_cwd_b):
        cfg_from_b = _load([str(config_file)])

    expected = str(config_dir / "mailbox")
    assert cfg_from_a.attachment_storage_path == expected
    assert cfg_from_b.attachment_storage_path == expected

    expected_docs = str(config_dir / "docs")
    assert cfg_from_a.document_storage_path == expected_docs
    assert cfg_from_b.document_storage_path == expected_docs


def test_absolute_attachment_storage_path_is_preserved(tmp_path: Path) -> None:
    """Absolute values pass through unchanged — the validator only rewrites
    relative inputs.
    """
    absolute_mailbox = (tmp_path / "absolute-mailbox").resolve()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"attachment_storage_path: {absolute_mailbox}\n",
        encoding="utf-8",
    )

    cfg = _load([str(config_file)])
    assert cfg.attachment_storage_path == str(absolute_mailbox)


def test_default_storage_paths_survive_yaml_load(tmp_path: Path) -> None:
    """When the YAML doesn't override the storage paths, the (absolute)
    defaults still pass through the validator cleanly and are not turned
    into config-file-relative joins.
    """
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("telegram_enabled: false\n", encoding="utf-8")

    cfg = _load([str(config_file)])
    # The model default is absolute ("/mnt/data/mailbox/attachments"); the
    # validator must treat it as-is, not anchor it to the config dir.
    assert Path(cfg.attachment_storage_path).is_absolute()
    assert cfg.attachment_storage_path == "/mnt/data/mailbox/attachments"


def test_relative_path_without_yaml_context_falls_back_to_cwd() -> None:
    """When ``AppConfig`` is constructed directly without
    ``yaml_source_context`` (ad-hoc scripts, tests), a relative value
    falls back to cwd-anchored ``abspath`` — same pre-fix behavior, just
    documented as the no-stable-anchor case.
    """
    with tempfile.TemporaryDirectory() as fallback_cwd, _cwd(Path(fallback_cwd)):
        cfg = AppConfig(attachment_storage_path="./relative-fallback")
        expected = (Path(fallback_cwd) / "relative-fallback").resolve()

    assert Path(cfg.attachment_storage_path) == expected
