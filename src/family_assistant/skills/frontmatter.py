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
    body = body.removeprefix("\n")

    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        logger.warning("Failed to parse YAML frontmatter")
        return None, content

    if not isinstance(parsed, dict):
        return None, content

    return parsed, body


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def skill_activations(
    # ast-grep-ignore: no-dict-any - Frontmatter is genuinely arbitrary YAML
    frontmatter: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the tools and MCP servers a skill's frontmatter says it brings."""
    return (
        _string_list(frontmatter.get("activate_tools")),
        _string_list(frontmatter.get("activate_mcp_servers")),
    )
