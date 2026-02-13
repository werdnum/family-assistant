"""Parse YAML frontmatter from markdown content."""

import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DELIMITER = "---"


# ast-grep-ignore: no-dict-any - Frontmatter is genuinely arbitrary YAML
def parse_frontmatter(content: str) -> tuple[dict[str, Any] | None, str]:
    """Parse YAML frontmatter from markdown content.

    Frontmatter is delimited by ``---`` at the start of the content.

    Returns:
        (frontmatter_dict, body) if valid frontmatter found,
        (None, original_content) otherwise.
    """
    stripped = content.lstrip("\n")
    if not stripped.startswith(_DELIMITER):
        return None, content

    after_open = stripped[len(_DELIMITER) :]
    if not after_open.startswith("\n"):
        return None, content

    close_idx = after_open.find(f"\n{_DELIMITER}", 1)
    if close_idx == -1:
        return None, content

    yaml_block = after_open[1:close_idx]
    body_start = close_idx + 1 + len(_DELIMITER)
    body = after_open[body_start:]
    if body.startswith("\n"):
        body = body[1:]

    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        logger.warning("Failed to parse YAML frontmatter")
        return None, content

    if not isinstance(parsed, dict):
        return None, content

    return parsed, body
