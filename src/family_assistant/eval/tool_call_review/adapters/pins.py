"""Upstream-revision pins for gated public corpora.

Any corpus a gate consumes is pinned to an upstream revision or checksum, and a
gate run fails on a mismatch rather than evaluating silently different content —
an after-the-fact content hash can only reveal that two runs differed, not keep
a held-out generation frozen. Unpinned fetch-on-demand is acceptable only for
dev slices, so :func:`verify_pin` is called by the build script on gate corpora
and skipped for dev use.

The pins live in ``PINS.toml`` beside this module. Each corpus records its
canonical upstream, the pinned revision, the SHA-256 checksum of the fetched
corpus file (or directory), and the license as declared at that revision.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Pin",
    "PinMismatchError",
    "PinNotFoundError",
    "corpus_checksum",
    "load_pins",
    "verify_pin",
]

_PINS_PATH = Path(__file__).parent / "PINS.toml"
_CHECKSUM_PREFIX = "sha256:"
_PLACEHOLDER_CHECKSUM = "sha256:PLACEHOLDER"


class PinNotFoundError(Exception):
    """No pin is recorded for the requested corpus."""


class PinMismatchError(Exception):
    """A locally-fetched corpus does not match its recorded pin."""


@dataclass(frozen=True, slots=True)
class Pin:
    """One corpus's recorded upstream pin."""

    corpus_id: str
    upstream: str
    revision: str
    checksum: str
    license: str

    @property
    def is_placeholder(self) -> bool:
        """Whether the checksum is the unfilled placeholder.

        The committed pins ship with a placeholder checksum because the real
        corpus is not vendored: a maintainer preparing a gate fills it in at
        pin time by hashing the fetched corpus with :func:`corpus_checksum`.
        """
        return self.checksum == _PLACEHOLDER_CHECKSUM


def load_pins(pins_path: Path | None = None) -> dict[str, Pin]:
    """Load all recorded pins from ``PINS.toml``."""
    path = pins_path if pins_path is not None else _PINS_PATH
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    pins: dict[str, Pin] = {}
    for corpus_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        pins[corpus_id] = Pin(
            corpus_id=corpus_id,
            upstream=str(entry["upstream"]),
            revision=str(entry["revision"]),
            checksum=str(entry["checksum"]),
            license=str(entry["license"]),
        )
    return pins


def corpus_checksum(local_path: Path) -> str:
    """Return the ``sha256:`` checksum of a corpus file or directory.

    A directory is hashed over its files in sorted relative-path order, each
    contribution prefixed by its path, so the digest is stable across
    filesystems and sensitive to any file being added, removed, or changed.
    """
    digest = hashlib.sha256()
    if local_path.is_dir():
        for file_path in sorted(p for p in local_path.rglob("*") if p.is_file()):
            rel = file_path.relative_to(local_path).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_path.read_bytes())
            digest.update(b"\0")
    else:
        digest.update(local_path.read_bytes())
    return f"{_CHECKSUM_PREFIX}{digest.hexdigest()}"


def verify_pin(
    corpus_id: str,
    local_path: Path,
    *,
    pins: Mapping[str, Pin] | None = None,
    pins_path: Path | None = None,
) -> Pin:
    """Verify a locally-fetched corpus matches its recorded pin, or fail.

    Raises :class:`PinNotFoundError` when no pin exists for ``corpus_id`` and
    :class:`PinMismatchError` when the recorded checksum is still the unfilled
    placeholder or when the fetched content hashes differently. Returns the pin
    on success. Dev slices that accept unpinned fetch-on-demand simply do not
    call this.
    """
    resolved = dict(pins) if pins is not None else load_pins(pins_path)
    pin = resolved.get(corpus_id)
    if pin is None:
        raise PinNotFoundError(
            f"No pin recorded for corpus {corpus_id!r}; a gate cannot consume an "
            "unpinned corpus."
        )
    if pin.is_placeholder:
        raise PinMismatchError(
            f"Corpus {corpus_id!r} has a placeholder checksum in PINS.toml; hash "
            "the fetched corpus with corpus_checksum() and record it before a gate "
            "consumes it."
        )
    actual = corpus_checksum(local_path)
    if actual != pin.checksum:
        raise PinMismatchError(
            f"Corpus {corpus_id!r} at {local_path} does not match its pin: "
            f"recorded {pin.checksum}, computed {actual} (revision {pin.revision})."
        )
    return pin
