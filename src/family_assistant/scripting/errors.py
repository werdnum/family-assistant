"""
Custom exceptions for the Family Assistant scripting system.
"""


class ScriptError(Exception):
    """Base exception for all scripting-related errors."""


class ScriptSyntaxError(ScriptError):
    """Raised when a script has invalid Starlark syntax."""

    def __init__(
        self, message: str, line: int | None = None, column: int | None = None
    ) -> None:
        super().__init__(message)
        self.line = line
        self.column = column


class ScriptExecutionError(ScriptError):
    """Raised when a script encounters an error during execution."""

    def __init__(self, message: str, stack_trace: str | None = None) -> None:
        super().__init__(message)
        self.stack_trace = stack_trace


class ScriptTimeoutError(ScriptError):
    """Raised when a script execution exceeds the allowed time limit."""

    def __init__(self, message: str, timeout_seconds: float) -> None:
        super().__init__(message)
        self.timeout_seconds = timeout_seconds
