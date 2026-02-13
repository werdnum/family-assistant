"""Load skill files from directories."""

import logging
from pathlib import Path

from family_assistant.skills.frontmatter import parse_frontmatter
from family_assistant.skills.types import ParsedSkill

logger = logging.getLogger(__name__)


def load_skills_from_directory(directory: Path) -> list[ParsedSkill]:
    """Load markdown files with skill frontmatter from a directory.

    Only files with valid frontmatter containing both ``name`` and
    ``description`` fields are loaded as skills. Other ``.md`` files
    are silently skipped.
    """
    skills: list[ParsedSkill] = []
    if not directory.is_dir():
        return skills

    for md_file in sorted(directory.glob("*.md")):
        try:
            raw_content = md_file.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Failed to read skill file: %s", md_file)
            continue

        frontmatter, body = parse_frontmatter(raw_content)
        if not frontmatter:
            continue

        name = frontmatter.get("name")
        description = frontmatter.get("description")
        if not name or not description:
            continue

        raw_labels = frontmatter.get("visibility_labels", [])
        visibility_labels = (
            frozenset(raw_labels) if isinstance(raw_labels, list) else frozenset()
        )

        skills.append(
            ParsedSkill(
                name=name,
                description=description,
                content=body,
                source_path=md_file,
                visibility_labels=visibility_labels,
            )
        )
        logger.debug("Loaded skill '%s' from %s", name, md_file)

    logger.info("Loaded %d skill(s) from %s", len(skills), directory)
    return skills
