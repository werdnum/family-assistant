"""Template utilities for the web interface."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache for the manifest file
_manifest_cache: dict | None = None
_manifest_last_read = 0


def get_static_asset(filename: str, entry_name: str = "main") -> str:
    """
    Get the path to a static asset from Vite's manifest.

    In development mode, returns the dev server URL.
    In production mode, reads the manifest.json file to get the hashed filename.

    Args:
        filename: The filename to look up (e.g., "main.js")
        entry_name: The entry point name from vite config (default: "main")

    Returns:
        The URL path to the asset
    """
    if should_use_vite_dev():
        # In development, use relative paths (Vite dev server will serve them)
        return f"/src/{filename}"

    # In production, read from manifest
    global _manifest_cache, _manifest_last_read

    manifest_path = (
        Path(__file__).parent.parent / "static" / "dist" / ".vite" / "manifest.json"
    )

    # Check if we need to reload the manifest
    if manifest_path.exists():
        mtime = manifest_path.stat().st_mtime
        if _manifest_cache is None or mtime > _manifest_last_read:
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    _manifest_cache = json.load(f)
                    _manifest_last_read = mtime
            except Exception as e:
                logger.error(f"Failed to read manifest.json: {e}")
                return f"/static/dist/{filename}"

    if _manifest_cache:
        # Look up the file in the manifest
        entry_key = f"src/{entry_name}.js"
        if entry_key in _manifest_cache:
            entry = _manifest_cache[entry_key]
            if filename == f"{entry_name}.js":
                return f"/static/dist/{entry['file']}"
            # Check CSS files
            if "css" in entry and filename == f"{entry_name}.css":
                css_files = entry.get("css", [])
                if css_files:
                    return f"/static/dist/{css_files[0]}"

    # Fallback to the original filename
    return f"/static/dist/{filename}"


def should_use_vite_dev() -> bool:
    """Check if we should use Vite dev server for assets."""
    # Check if DEV_MODE is explicitly set
    if os.getenv("DEV_MODE", "false").lower() == "true":
        return True

    # Auto-detect if Vite dev server is running
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.1)
        result = sock.connect_ex(("localhost", 5173))
        sock.close()
        return result == 0  # Port is open, Vite is running
    except Exception:
        return False
