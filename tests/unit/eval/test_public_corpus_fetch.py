"""Tests for the private destination guard in the public-corpus fetcher."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from family_assistant.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from pathlib import Path


def test_fetch_rejects_a_symlinked_private_eval_root(tmp_path: Path) -> None:
    """The fetcher must not place raw corpora through a linked marker directory."""
    fake_repository = tmp_path / "repository"
    script_directory = fake_repository / "scripts"
    script_directory.mkdir(parents=True)
    fetcher = script_directory / "fetch_review_eval_corpora.sh"
    shutil.copy2(PROJECT_ROOT / "scripts/fetch_review_eval_corpora.sh", fetcher)
    fetcher.chmod(0o755)

    tracked_destination = fake_repository / "tracked-destination"
    tracked_destination.mkdir()
    (fake_repository / ".review-eval-local").symlink_to(
        tracked_destination, target_is_directory=True
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "lfs" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'if [ "$1" = "-C" ] && [ "$3" = "rev-parse" ]; then\n'
        '  printf "%s\\n" "$FAKE_REPOSITORY"\n'
        "  exit 0\n"
        "fi\n"
        'printf "unexpected git invocation\\n" >&2\n'
        "exit 99\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    result = subprocess.run(
        [
            str(fetcher),
            "--deepset-revision",
            "0" * 40,
            "--injecagent-revision",
            "1" * 40,
        ],
        cwd=fake_repository,
        env={
            **os.environ,
            "FAKE_REPOSITORY": str(fake_repository),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing symlinked private eval root" in result.stderr
    assert list(tracked_destination.iterdir()) == []


def test_fetch_uses_pinned_defaults_without_caller_input(tmp_path: Path) -> None:
    """Bare invocation uses the script pins before any network acquisition."""
    fake_repository = tmp_path / "repository"
    script_directory = fake_repository / "scripts"
    script_directory.mkdir(parents=True)
    fetcher = script_directory / "fetch_review_eval_corpora.sh"
    shutil.copy2(PROJECT_ROOT / "scripts/fetch_review_eval_corpora.sh", fetcher)
    fetcher.chmod(0o755)

    tracked_destination = fake_repository / "tracked-destination"
    tracked_destination.mkdir()
    (fake_repository / ".review-eval-local").symlink_to(
        tracked_destination, target_is_directory=True
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "lfs" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'if [ "$1" = "-C" ] && [ "$3" = "rev-parse" ]; then\n'
        '  printf "%s\\n" "$FAKE_REPOSITORY"\n'
        "  exit 0\n"
        "fi\n"
        'printf "unexpected git invocation\\n" >&2\n'
        "exit 99\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    result = subprocess.run(
        [str(fetcher)],
        cwd=fake_repository,
        env={
            **os.environ,
            "FAKE_REPOSITORY": str(fake_repository),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing symlinked private eval root" in result.stderr
