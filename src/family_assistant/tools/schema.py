import html
import json  # Add json import
import logging
import os  # For working with file paths
import tempfile  # For creating temporary files
from collections.abc import Callable
from typing import Any

# Remove lru_cache as we will cache based on tool name at the call site

# Attempt to import schema generation tools, handle import error gracefully
try:
    from json_schema_for_humans.generate import (  # type: ignore[import-untyped]
        generate_from_filename as _generate_from_filename,
    )
    from json_schema_for_humans.generation_configuration import (  # type: ignore[import-untyped]
        GenerationConfiguration,
    )

    _SCHEMA_GENERATION_AVAILABLE = True
    _SCHEMA_GEN_CONFIG: Any = GenerationConfiguration(
        template_name="flat", with_footer=False
    )
    generate_from_filename: Callable[..., Any] | None = _generate_from_filename
except ImportError:
    _SCHEMA_GENERATION_AVAILABLE = False
    _SCHEMA_GEN_CONFIG = None
    generate_from_filename = None
    logging.warning(
        "json-schema-for-humans library not found. "
        "Tool schema rendering will fall back to raw JSON."
    )

logger = logging.getLogger(__name__)


def _generate_schema_html(schema_json_str: str) -> str:
    with tempfile.TemporaryDirectory() as temp_dir:
        infile_path = os.path.join(temp_dir, "schema.json")
        outfile_path = os.path.join(temp_dir, "schema.html")
        with open(infile_path, "w", encoding="utf-8") as infile:
            infile.write(schema_json_str)
        with open(outfile_path, "w", encoding="utf-8"):
            pass

        assert generate_from_filename is not None
        assert _SCHEMA_GEN_CONFIG is not None
        generate_from_filename(infile_path, outfile_path, config=_SCHEMA_GEN_CONFIG)
        with open(outfile_path, encoding="utf-8") as outfile:
            return outfile.read()


# Removed @lru_cache decorator
def render_schema_as_html(schema_json_str: str | None) -> str:
    """
    Renders a JSON schema (passed as a JSON string) as HTML using json-schema-for-humans.
    Uses temporary files for input and output.
    The JSON string input makes the function cacheable.
    """
    if not schema_json_str:
        return "<p>No parameters defined.</p>"

    # Attempt to parse the input string back to a dict for validation/use
    try:
        schema_dict = json.loads(schema_json_str)
        if not isinstance(schema_dict, dict) or not schema_dict.get("properties"):
            return "<p>No parameters defined (invalid schema structure).</p>"
    except json.JSONDecodeError:
        return "<p>Error: Invalid JSON schema provided.</p>"

    if (
        not _SCHEMA_GENERATION_AVAILABLE
        or generate_from_filename is None
        or _SCHEMA_GEN_CONFIG is None
    ):
        # Fallback to preformatted JSON if library is unavailable
        return f"<pre>{html.escape(json.dumps(schema_dict, indent=2))}</pre>"

    # Use temporary files
    try:
        return _generate_schema_html(schema_json_str)
    except Exception as e:
        logger.exception(f"Failed to generate HTML schema: {e}")
        return f"<pre>Error generating schema HTML: {html.escape(str(e))}</pre>"
