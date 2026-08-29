"""Adapter contract and lineage records for public-corpus adaptation.

An :class:`Adapter` turns rows of a locally-fetched upstream corpus into
schema-valid :class:`~family_assistant.eval.tool_call_review.schema.EvalCase`
objects, each carrying an :class:`AdaptedLineage` record so a near-duplicate
that recurs across corpora can be clustered before any dev/gate split. Lineage
is load-bearing: the large corpora incorporate one another, so the same attack
appearing on both sides of a split would flatter the judge.
"""

from __future__ import annotations

import hashlib
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from family_assistant.eval.tool_call_review.schema import EvalCase

__all__ = [
    "AdaptedCase",
    "AdaptedLineage",
    "Adapter",
    "lineage_aware_dedup",
    "normalized_text_key",
]


def normalized_text_key(text: str) -> str:
    """Return a whitespace/case/Unicode-folded digest of an injection text.

    Human adversarial pools are duplicate-heavy and the same template recurs
    verbatim across corpora, so lineage-aware dedup keys on a normalized digest
    rather than the raw bytes: NFKC folding collapses the compatibility and
    zero-width tricks that otherwise present one attack as many.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    collapsed = " ".join(folded.split())
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AdaptedLineage:
    """Provenance for one adapted case, preserved before any dev/gate split.

    ``group`` clusters cases that share an author, challenge, or template
    family — the unit BIPIA-style combinatorial corpora and human adversarial
    pools must be held out by, since a random row-level split would leave the
    same family on both sides. ``dedup_key`` combines the group with a
    normalized digest of the injection text so a template that recurs across
    corpora deduplicates to one case.
    """

    corpus_id: str
    upstream_id: str
    group: str
    license: str
    upstream_revision: str | None = None
    text_key: str = ""

    @property
    def dedup_key(self) -> tuple[str, str]:
        """Return the (group, normalized-text) key for lineage-aware dedup."""
        return (self.group, self.text_key)

    def to_source_metadata(self) -> dict[str, object]:
        """Render the sidecar record that travels beside the committed case."""
        return {
            "corpus_id": self.corpus_id,
            "upstream_id": self.upstream_id,
            "group": self.group,
            "license": self.license,
            "upstream_revision": self.upstream_revision,
            "text_key": self.text_key,
        }


@dataclass(frozen=True, slots=True)
class AdaptedCase:
    """One adapted case paired with its lineage record."""

    case: EvalCase
    lineage: AdaptedLineage


@dataclass
class Adapter(ABC):
    """Maps one upstream corpus's rows into paired cases and lineage.

    Concrete adapters declare their ``corpus_id``, ``license``, and ``upstream``
    identifier as class attributes, load rows via :meth:`from_path` (or
    :meth:`from_sample`), and implement :meth:`iter_adapted`. The ``source``
    string on every emitted case is ``public:<corpus_id>``, matching the design
    doc's slice convention; the richer lineage travels in the paired
    :class:`AdaptedLineage` and the sidecar the build script writes.
    """

    corpus_id: ClassVar[str]
    license: ClassVar[str]
    upstream: ClassVar[str]

    rows: Sequence[object]
    upstream_revision: str | None = None
    _id_seen: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def source(self) -> str:
        """Return the ``public:<corpus_id>`` source tag for this corpus."""
        return f"public:{self.corpus_id}"

    @classmethod
    @abstractmethod
    def parse_rows(cls, path: Path) -> list[object]:
        """Parse a locally-fetched corpus file into raw rows for this adapter."""

    @classmethod
    def from_path(cls, path: Path, *, upstream_revision: str | None = None) -> Adapter:
        """Build an adapter from a locally-fetched corpus file or directory."""
        return cls(cls.parse_rows(path), upstream_revision=upstream_revision)

    @classmethod
    def sample_dir(cls) -> Path:
        """Return the bundled tiny-sample directory for this corpus."""
        return Path(__file__).parent / "samples" / cls.corpus_id

    @classmethod
    def from_sample(cls) -> Adapter:
        """Build an adapter from the committed synthetic sample."""
        sample_dir = cls.sample_dir()
        files = sorted(p for p in sample_dir.iterdir() if p.is_file())
        rows: list[object] = []
        for file_path in files:
            rows.extend(cls.parse_rows(file_path))
        return cls(rows, upstream_revision="sample")

    @abstractmethod
    def iter_adapted(self) -> Iterable[AdaptedCase]:
        """Yield each mapped case paired with its lineage."""

    def iter_cases(self) -> Iterator[EvalCase]:
        """Yield mapped :class:`EvalCase` objects (dropping lineage)."""
        for adapted in self.iter_adapted():
            yield adapted.case

    def _unique_id(self, candidate: str) -> str:
        """Return a case id unique within this adapter run."""
        if candidate not in self._id_seen:
            self._id_seen.add(candidate)
            return candidate
        suffix = 2
        while f"{candidate}-{suffix}" in self._id_seen:
            suffix += 1
        unique = f"{candidate}-{suffix}"
        self._id_seen.add(unique)
        return unique


def lineage_aware_dedup(adapted: Iterable[AdaptedCase]) -> list[AdaptedCase]:
    """Drop later cases that share a lineage dedup key with an earlier one.

    Clustering by ``(group, normalized-text)`` keeps a template that recurs
    across corpora — or a duplicate row within one — from landing on both sides
    of a dev/gate split. The first occurrence wins; order is otherwise
    preserved.
    """
    seen: set[tuple[str, str]] = set()
    kept: list[AdaptedCase] = []
    for item in adapted:
        key = item.lineage.dedup_key
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept
