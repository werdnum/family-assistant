"""The core memory note's topic index, which is derived rather than authored.

See docs/design/conversation-memory.md, "Memory lives in notes": the core note
carries the household's standing facts *and* a short index of the topic notes,
and the index half is regenerated from the topic notes that exist on every
apply to any memory note. A pointer therefore never outlives its topic or its
title, and no writer has to remember to update it.

The section is delimited by an HTML comment pair. Everything outside the
markers belongs to whoever wrote it -- the curator, the foreground assistant, a
person editing in the notes UI. Whatever the submitted markdown says *inside*
the markers is discarded and replaced, which is what makes "a hand edit can
change the core note's entries but not its index" true rather than advisory.

With no topic notes, no section is emitted at all, so a household that has only
ever written standing facts keeps a core note free of scaffolding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

INDEX_START_MARKER = "<!-- memory:index -->"
INDEX_END_MARKER = "<!-- /memory:index -->"

_INDEX_HEADING = "## Memory topics"


@dataclass(frozen=True)
class TopicIndexEntry:
    """One topic memory note, as the index names it."""

    title: str
    last_changed: datetime


def strip_topic_index(core_markdown: str) -> str:
    """Return the author's part of the core note: everything outside the markers.

    Tolerates a missing end marker (an interrupted hand edit) by treating the
    rest of the note as the derived section, since the alternative is leaving a
    half-open marker that swallows the next regeneration.
    """
    start = core_markdown.find(INDEX_START_MARKER)
    if start == -1:
        return core_markdown
    end = core_markdown.find(INDEX_END_MARKER, start)
    after = "" if end == -1 else core_markdown[end + len(INDEX_END_MARKER) :]
    return (core_markdown[:start].rstrip() + "\n" + after.lstrip("\n")).strip()


def regenerate_topic_index(
    core_markdown: str,
    topics: Sequence[TopicIndexEntry],
    *,
    max_chars: int,
) -> str:
    """Replace the core note's derived index section with a current one.

    The index names topic notes most recently changed first, truncated so the
    whole section stays within ``max_chars``; a topic that drops off it is
    still reachable by title and by search.

    Args:
        core_markdown: The core note's content as submitted or stored.
        topics: Every topic memory note that currently exists.
        max_chars: The index's share of the core note's cap.

    Returns:
        The core note's content with the derived section regenerated.
    """
    authored = strip_topic_index(core_markdown)
    section = _render_section(topics, max_chars=max_chars)
    if not section:
        return authored
    if not authored:
        return section
    return f"{authored}\n\n{section}"


def _render_section(topics: Sequence[TopicIndexEntry], *, max_chars: int) -> str:
    """Render as many topics as fit, or "" when none do."""
    ordered = sorted(
        topics, key=lambda topic: (-topic.last_changed.timestamp(), topic.title)
    )
    lines = [
        f"- {topic.title} (changed {topic.last_changed:%Y-%m-%d})" for topic in ordered
    ]

    rendered = ""
    for count in range(1, len(lines) + 1):
        candidate = "\n".join([
            INDEX_START_MARKER,
            "",
            _INDEX_HEADING,
            "",
            *lines[:count],
            "",
            INDEX_END_MARKER,
        ])
        if len(candidate) > max_chars:
            break
        rendered = candidate
    return rendered
