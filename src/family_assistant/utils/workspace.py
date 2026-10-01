"""Paths in the shared workspace.

Validates and resolves workspace-relative paths for the workspace file tools
and the AI worker plugin.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


def get_workspace_root(exec_context: ToolExecutionContext) -> Path:
    """Get the shared workspace root from configuration.

    Args:
        exec_context: The tool execution context

    Returns:
        Path to the workspace root

    Raises:
        ValueError: If processing_service or app_config is not available
    """
    if exec_context.processing_service is None:
        raise ValueError("processing_service not available in exec_context")

    app_config = exec_context.processing_service.app_config
    return Path(app_config.shared_workspace_path)


def validate_workspace_path(relative_path: str, workspace_root: Path) -> Path:
    """Validate and resolve a workspace-relative path.

    Args:
        relative_path: Path relative to workspace root
        workspace_root: The workspace root directory

    Returns:
        Resolved absolute path within workspace

    Raises:
        ValueError: If path attempts to escape workspace
    """
    # Normalize the path and resolve any .. components
    normalized = Path(relative_path)
    if normalized.is_absolute():
        raise ValueError(f"Path must be relative, not absolute: {relative_path}")

    # Resolve against workspace root
    full_path = (workspace_root / normalized).resolve()

    # Ensure the resolved path is still within workspace
    try:
        full_path.relative_to(workspace_root.resolve())
    except ValueError as e:
        raise ValueError(f"Path escapes workspace directory: {relative_path}") from e

    return full_path
