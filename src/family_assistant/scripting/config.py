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
    enable_json_api: bool = True  # Whether json_encode/json_decode are loaded
    enable_time_api: bool = True  # Whether time_* and duration_* helpers are loaded
    enable_llm_api: bool = True  # Whether llm()/llm_json() are loaded
    enable_base64_api: bool = True  # Whether base64_encode/base64_decode are loaded
