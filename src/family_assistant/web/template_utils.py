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
        Path(__file__).parent.parent.resolve()
        / "static"
        / "dist"
        / ".vite"
        / "manifest.json"
    )

    if not manifest_path.exists():
        manifest_path = Path(
            "./src/family_assistant/static/dist/.vite/manifest.json"
        ).resolve()
    if not manifest_path.exists():
        manifest_path = Path(
            "/app/src/family_assistant/static/dist/.vite/manifest.json"
        ).resolve()

    # Check if we need to reload the manifest
    if manifest_path.exists():
        mtime = manifest_path.stat().st_mtime
        if _manifest_cache is None or mtime > _manifest_last_read:
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    _manifest_cache = json.load(f)
                    _manifest_last_read = mtime
                    logger.info(
                        f"Loaded manifest.json with {len(_manifest_cache) if _manifest_cache else 0} entries"
                    )
            except Exception as e:
                logger.error(f"Failed to read manifest.json: {e}")
                return f"/static/dist/{filename}"
    else:
        logger.error(f"Manifest file not found at: {manifest_path}")
        return f"/static/dist/{filename}"

    if _manifest_cache:
        # Look up the file in the manifest
        entry_key = f"src/{filename}"
        if entry_key in _manifest_cache:
            return f"/static/dist/{_manifest_cache[entry_key]['file']}"

        # Fallback for CSS or other assets tied to an entry point
        entry_point_key = f"src/{entry_name}.js"
        if entry_point_key in _manifest_cache:
            entry = _manifest_cache[entry_point_key]
            # Check for CSS files
            if "css" in entry and filename.endswith(".css") and entry.get("css"):
                return f"/static/dist/{entry['css'][0]}"
            # Check for other assets if needed
            if "assets" in entry:
                for asset in entry.get("assets", []):
                    if filename in asset:
                        return f"/static/dist/{asset}"

    # Fallback to the original filename
    logger.warning(
        "Asset not found in manifest, using original filename: %s (entry_key=%s, manifest has %d entries)",
        filename,
        f"src/{filename}",
        len(_manifest_cache) if _manifest_cache else 0,
    )
    if _manifest_cache and logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Manifest keys: {list(_manifest_cache.keys())}")
    return f"/static/dist/{filename}"


def should_use_vite_dev() -> bool:
    """Check if we should use Vite dev server for assets."""
    return os.getenv("DEV_MODE", "false").lower() == "true"
