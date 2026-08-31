"""Adapter contract and lineage records for public-corpus adaptation.

An :class:`Adapter` turns rows of a locally-fetched upstream corpus into
schema-valid :class:`~family_assistant.eval.tool_call_review.schema.EvalCase`
objects, each carrying an :class:`AdaptedLineage` record naming the upstream
row and the family it belongs to, so whole families can be held out as units
before any dev/gate split. Lineage is load-bearing: the large corpora
incorporate one another, so the same family appearing on both sides of a split
would flatter the judge.

Adapters do not decide what counts as the same input. Two rows that assemble the
same reviewer prompt are one attack input, and that is settled at load time over
whatever corpus is being evaluated, by
:func:`~family_assistant.eval.tool_call_review.loader.attack_input_key`.
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
    "evaluation_split_for_group",
    "normalized_text_key",
]


def normalized_text_key(text: str) -> str:
    """Return a whitespace/case/Unicode-folded digest of a text, for grouping.

    A corpus that ships no author or challenge label has no family to group by,
    so its adapter groups by the text itself; NFKC folding keeps the trivial
    whitespace and compatibility variants of one template in one family.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    collapsed = " ".join(folded.split())
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def evaluation_split_for_group(group: str) -> str:
    """Assign a family to a deterministic dev/gate split before expansion.

    Five-way bucketing reserves one bucket for held-out gate evidence and four
    for prompt iteration. The input is the already-normalized family key, so
    every visibility/control derivative of a family receives the same split.
    """
    digest = hashlib.sha256(group.encode("utf-8")).digest()
    return "gate" if digest[0] % 5 == 0 else "dev"


@dataclass(frozen=True, slots=True)
class AdaptedLineage:
    """Provenance for one adapted case, preserved before any dev/gate split.

    ``group`` clusters cases that share an author, challenge, or template
    family — the unit BIPIA-style combinatorial corpora and human adversarial
    pools must be held out by, since a random row-level split would leave the
    same family on both sides. ``paired_upstream_id`` records the source row
    paired into a matched control, without copying its content. ``source_split``
    preserves the upstream split or source slice, while ``evaluation_split``
    records the deterministic family-level dev/gate assignment. It is a
    split-assignment label and nothing else; whether two cases are the same
    input is not a question lineage answers.
    """

    corpus_id: str
    upstream_id: str
    group: str
    license: str
    upstream_revision: str | None = None
    paired_upstream_id: str | None = None
    adapter_version: str = "unspecified"
    source_split: str | None = None
    evaluation_split: str = "dev"


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
    string on every emitted case is a ``public:<corpus_id>`` slice (possibly
    variant-qualified), matching the design doc's convention; the richer
    lineage travels in the paired :class:`AdaptedLineage` and the sidecar the
    build script writes.
    """

    corpus_id: ClassVar[str]
    license: ClassVar[str]
    upstream: ClassVar[str]
    adapter_version: ClassVar[str] = "browser-ablation-v2"

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
