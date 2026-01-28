"""Project-wide constants."""

import pathlib

# Path to the directory containing this file (src/family_assistant)
PACKAGE_ROOT = pathlib.Path(__file__).parent.resolve()

# Path to the project root directory (root of the repo)
# src/family_assistant -> src -> root
PROJECT_ROOT = PACKAGE_ROOT.parent.parent.resolve()
