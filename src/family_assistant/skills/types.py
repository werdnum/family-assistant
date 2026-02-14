"""Skill types."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParsedSkill:
    """A file-based skill with parsed metadata."""

    name: str
    description: str
    content: str
    source_path: Path
    visibility_labels: frozenset[str] = frozenset()
