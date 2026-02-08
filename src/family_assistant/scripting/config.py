"""Configuration for the scripting engine."""

from dataclasses import dataclass


@dataclass
class ScriptConfig:
    """Configuration for the scripting engine."""

    max_execution_time: float = (
        600.0  # Maximum execution time in seconds (10 minutes default)
    )
    enable_print: bool = True  # Whether to enable the print() function
    enable_debug: bool = False  # Whether to enable debug output
    allowed_tools: set[str] | None = (
        None  # If specified, only these tools can be executed
    )
    deny_all_tools: bool = False  # If True, no tools can be executed
    disable_apis: bool = False  # If True, no APIs (json, time, etc.) are loaded


StarlarkConfig = ScriptConfig
