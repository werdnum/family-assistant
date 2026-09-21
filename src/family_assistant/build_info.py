"""Build provenance of the running deployment.

The production image excludes ``.git`` (see ``.dockerignore``), so a build
argument is the only record of which revision the running code came from.
Reading it in one place keeps the variable names from being spelled out at
each call site, where a typo would silently report ``unknown`` forever.
"""

import os

UNKNOWN = "unknown"


def get_git_commit() -> str:
    """Return the commit the image was built from, or ``unknown``."""
    return os.getenv("GIT_COMMIT", UNKNOWN)


def get_build_date() -> str:
    """Return the image's build timestamp, or ``unknown``."""
    return os.getenv("BUILD_DATE", UNKNOWN)
