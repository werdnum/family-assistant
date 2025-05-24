import html
import json  # Add json import
import logging
import os  # For working with file paths
import tempfile  # For creating temporary files

# Remove lru_cache as we will cache based on tool name at the call site

# Attempt to import schema generation tools, handle import error gracefully
try:
    # Import generate_from_filename which works with file paths
    from json_schema_for_humans.generate import (  # type: ignore[import-untyped]
        generate_from_filename,
    )
    from json_schema_for_humans.generation_configuration import (  # type: ignore[import-untyped]
        GenerationConfiguration,
    )

    _SCHEMA_GENERATION_AVAILABLE = True
    # Configure once
    _SCHEMA_GEN_CONFIG = GenerationConfiguration(
        template_name="flat", with_footer=False
    )
except ImportError:
    _SCHEMA_GENERATION_AVAILABLE = False
    _SCHEMA_GEN_CONFIG = None  # Type: ignore
    generate_from_filename = None  # Type: ignore
    logging.warning(
        "json-schema-for-humans library not found. "
        "Tool schema rendering will fall back to raw JSON."
    )

logger = logging.getLogger(__name__)


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
        with (
            tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as infile,
            tempfile.NamedTemporaryFile(
                mode="r", suffix=".html", delete=False, encoding="utf-8"
            ) as outfile,
        ):
            infile.write(schema_json_str)  # Write the original JSON string
            infile_path = infile.name
            outfile_path = outfile.name

        # Ensure files are closed before passing paths to the generate function
        generate_from_filename(infile_path, outfile_path, config=_SCHEMA_GEN_CONFIG)
        with open(outfile_path, encoding="utf-8") as f:
            result_html = f.read()
        return result_html
    except Exception as e:
        logger.error(f"Failed to generate HTML schema: {e}", exc_info=True)
        return f"<pre>Error generating schema HTML: {html.escape(str(e))}</pre>"
    finally:
        # Clean up temporary files
        if "infile_path" in locals() and os.path.exists(infile_path):
            os.remove(infile_path)
        if "outfile_path" in locals() and os.path.exists(outfile_path):
            os.remove(outfile_path)
