"""Centralized path resolution for the Family Assistant project.

All project paths are derived from a single anchor point (this module's location),
eliminating fragile deep ``Path(__file__).parent.parent...`` traversals elsewhere.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single anchor: this file lives at  src/family_assistant/paths.py
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent

# Package root: src/family_assistant/
PACKAGE_ROOT: Path = _THIS_DIR

# Project root: the repository / working-tree root (two levels above the package)
PROJECT_ROOT: Path = _THIS_DIR.parent.parent

# ---------------------------------------------------------------------------
# Derived paths
# ---------------------------------------------------------------------------

# Frontend source (used in dev mode to serve HTML / SW from Vite project)
FRONTEND_DIR: Path = PROJECT_ROOT / "frontend"

# Static assets shipped inside the package
STATIC_DIR: Path = PACKAGE_ROOT / "static"
STATIC_DIST_DIR: Path = STATIC_DIR / "dist"

# Jinja2 templates
TEMPLATES_DIR: Path = PACKAGE_ROOT / "templates"

# Web-layer resources (greeting WAV files, etc.)
WEB_RESOURCES_DIR: Path = PACKAGE_ROOT / "web" / "resources"


def get_docs_user_dir() -> Path:
    """Return the user-facing docs directory, respecting ``DOCS_USER_DIR`` env var.

    Resolution order:
    1. ``DOCS_USER_DIR`` environment variable (for Docker/custom deployments).
    2. ``<PROJECT_ROOT>/docs/user`` (standard development layout).
    3. ``/app/docs/user`` (Docker fallback when project root is unavailable).
    """
    env_val = os.getenv("DOCS_USER_DIR")
    if env_val:
        return Path(env_val).resolve()

    default = PROJECT_ROOT / "docs" / "user"
    if default.is_dir():
        return default

    docker_fallback = Path("/app/docs/user")
    if docker_fallback.is_dir():
        logger.info("Using Docker default docs directory: %s", docker_fallback)
        return docker_fallback

    return default


def validate_paths_at_startup(*, dev_mode: bool) -> None:
    """Log warnings for expected directories that are missing.

    Call once during application startup so operators get early, actionable
    feedback rather than cryptic 404s at request time.
    """
    checks: list[tuple[str, Path]] = [
        ("templates", TEMPLATES_DIR),
        ("static", STATIC_DIR),
        ("docs/user", get_docs_user_dir()),
    ]

    if dev_mode:
        checks.append(("frontend (dev)", FRONTEND_DIR))
    else:
        checks.append(("static/dist (prod)", STATIC_DIST_DIR))

    for label, path in checks:
        if not path.is_dir():
            logger.warning("Expected directory not found: %s → %s", label, path)
