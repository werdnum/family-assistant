import html
import logging
import json # Add json import
from typing import Dict, Any, Optional
import io # Import io for StringIO
from functools import lru_cache

# Attempt to import schema generation tools, handle import error gracefully
try:
    from json_schema_for_humans.generation_configuration import GenerationConfiguration
    # Import generate_from_file_object instead of generate_from_schema
    from json_schema_for_humans.generate import generate_from_file_object

    _SCHEMA_GENERATION_AVAILABLE = True
    # Configure once
    _SCHEMA_GEN_CONFIG = GenerationConfiguration(template_name="flat")
except ImportError:
    _SCHEMA_GENERATION_AVAILABLE = False
    _SCHEMA_GEN_CONFIG = None # Type: ignore
    generate_from_file_object = None # Type: ignore
    logging.warning(
        "json-schema-for-humans library not found. "
        "Tool schema rendering will fall back to raw JSON."
    )

logger = logging.getLogger(__name__)


@lru_cache(maxsize=128)
def render_schema_as_html(schema_dict: Optional[Dict[str, Any]]) -> str:
    """Renders a JSON schema dictionary as HTML using json-schema-for-humans."""
    if not schema_dict or not isinstance(schema_dict, dict) or not schema_dict.get("properties"):
        return "<p>No parameters defined.</p>"

    if not _SCHEMA_GENERATION_AVAILABLE or generate_from_file_object is None or _SCHEMA_GEN_CONFIG is None:
        # Fallback to preformatted JSON if library is unavailable
        # Ensure json is imported if this path is taken
        import json
        return f"<pre>{html.escape(json.dumps(schema_dict, indent=2))}</pre>"

    try:
        # Serialize schema to JSON string
        schema_json_str = json.dumps(schema_dict, indent=2)
        # Create in-memory file objects
        input_file = io.StringIO(schema_json_str)
        output_file = io.StringIO()
        # Use generate_from_file_object
        generate_from_file_object(input_file, output_file, config=_SCHEMA_GEN_CONFIG)
        output_file.seek(0) # Rewind to read the content
        return output_file.read()
    except Exception as e:
        logger.error(f"Failed to generate HTML schema: {e}", exc_info=True)
        return f"<pre>Error generating schema HTML: {html.escape(str(e))}</pre>"
