"""
Family Assistant Scripting System.

Provides a Starlark-based scripting engine for executing user-defined scripts
with access to family assistant tools and state.
"""

from .engine import StarlarkEngine
from .errors import (
    ScriptError,
    ScriptExecutionError,
    ScriptSyntaxError,
    ScriptTimeoutError,
)

__all__ = [
    "StarlarkEngine",
    "ScriptError",
    "ScriptExecutionError",
    "ScriptSyntaxError",
    "ScriptTimeoutError",
]
