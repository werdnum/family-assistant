"""Tests for the script validator (static type checking)."""

import pydantic_monty
import pytest

from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.validator import (
    ScriptValidator,
    ValidationDiagnostic,
    ValidationResult,
    generate_prefix_code,
)


class TestScriptValidatorSyntax:
    """Test syntax error detection."""

    def test_valid_simple_expression(self) -> None:
        v = ScriptValidator()
        result = v.validate("1 + 2")
        assert result.is_valid

    def test_syntax_error_incomplete_def(self) -> None:
        v = ScriptValidator()
        result = v.validate("def foo(")
        assert not result.is_valid
        assert result.errors

    def test_syntax_error_bad_indent(self) -> None:
        v = ScriptValidator()
        result = v.validate("if True:\nx = 1")
        assert not result.is_valid


class TestScriptValidatorTypeChecking:
    """Test static type checking."""

    def test_string_plus_int_is_type_error(self) -> None:
        v = ScriptValidator()
        result = v.validate('"hello" + 1')
        assert not result.is_valid
        assert any("+" in d.message or "operator" in d.message for d in result.errors)

    def test_valid_string_concatenation(self) -> None:
        v = ScriptValidator()
        result = v.validate('"hello" + " world"')
        assert result.is_valid

    def test_valid_arithmetic(self) -> None:
        v = ScriptValidator()
        result = v.validate("x = 2 + 3\nx * 10")
        assert result.is_valid

    def test_unknown_function_is_error(self) -> None:
        v = ScriptValidator()
        result = v.validate("nonexistent_function()")
        assert not result.is_valid

    def test_builtin_api_functions_are_known(self) -> None:
        v = ScriptValidator()
        result = v.validate("t = time_now()\ntime_year(t)")
        assert result.is_valid

    def test_json_api_is_known(self) -> None:
        v = ScriptValidator()
        result = v.validate('json_encode({"key": "value"})')
        assert result.is_valid

    def test_llm_api_is_known(self) -> None:
        v = ScriptValidator()
        result = v.validate('llm("summarize this")')
        assert result.is_valid

    def test_wake_llm_is_known(self) -> None:
        v = ScriptValidator()
        result = v.validate('wake_llm("hello")')
        assert result.is_valid

    def test_attachment_api_is_known(self) -> None:
        v = ScriptValidator()
        result = v.validate('attachment_get("some-id")')
        assert result.is_valid

    def test_duration_constants_are_known(self) -> None:
        v = ScriptValidator()
        result = v.validate("t = time_now()\ntime_add(t, 5 * MINUTE)")
        assert result.is_valid

    def test_print_is_known(self) -> None:
        v = ScriptValidator()
        result = v.validate('print("hello")')
        assert result.is_valid


class TestScriptValidatorWithGlobals:
    """Test validation with injected globals."""

    def test_global_variable_is_known(self) -> None:
        v = ScriptValidator()
        result = v.validate("user_name", input_names=["user_name"])
        assert result.is_valid

    def test_global_variable_not_declared_is_error(self) -> None:
        v = ScriptValidator()
        result = v.validate("user_name")
        assert not result.is_valid

    def test_multiple_globals(self) -> None:
        v = ScriptValidator()
        result = v.validate(
            "str(count) + name",
            input_names=["name", "count"],
        )
        assert result.is_valid


class TestScriptValidatorWithTools:
    """Test validation with tool definitions."""

    @pytest.fixture
    def sample_tool_definitions(self) -> list:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_notes",
                    "description": "Search notes",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_or_update_note",
                    "description": "Add or update a note",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Note title"},
                            "content": {
                                "type": "string",
                                "description": "Note content",
                            },
                        },
                        "required": ["title", "content"],
                    },
                },
            },
        ]

    def test_tool_call_is_valid(self, sample_tool_definitions: list) -> None:
        v = ScriptValidator(tool_definitions=sample_tool_definitions)
        result = v.validate('search_notes(query="TODO")')
        assert result.is_valid

    def test_tool_prefixed_call_is_valid(self, sample_tool_definitions: list) -> None:
        v = ScriptValidator(tool_definitions=sample_tool_definitions)
        result = v.validate('tool_search_notes(query="TODO")')
        assert result.is_valid

    def test_multi_tool_script(self, sample_tool_definitions: list) -> None:
        v = ScriptValidator(tool_definitions=sample_tool_definitions)
        script = """
notes = search_notes(query="project")
add_or_update_note(title="Summary", content="Found notes")
"""
        result = v.validate(script)
        assert result.is_valid

    def test_tools_meta_api_is_known(self, sample_tool_definitions: list) -> None:
        v = ScriptValidator(tool_definitions=sample_tool_definitions)
        result = v.validate("tools_list()")
        assert result.is_valid


class TestScriptValidatorConfig:
    """Test configuration options."""

    def test_disable_apis(self) -> None:
        config = ScriptConfig(disable_apis=True)
        v = ScriptValidator(config=config)
        result = v.validate("time_now()")
        assert not result.is_valid

    def test_disable_apis_still_has_wake_llm(self) -> None:
        config = ScriptConfig(disable_apis=True)
        v = ScriptValidator(config=config)
        result = v.validate('wake_llm("hello")')
        assert result.is_valid


class TestValidationResult:
    """Test ValidationResult structure."""

    def test_valid_result_has_no_errors(self) -> None:
        r = ValidationResult(is_valid=True, diagnostics=[])
        assert r.is_valid
        assert r.error_message is None
        assert r.errors == []

    def test_error_message_joins_errors(self) -> None:
        r = ValidationResult(
            is_valid=False,
            diagnostics=[
                ValidationDiagnostic(message="error one", line=1, severity="error"),
                ValidationDiagnostic(message="error two", line=3, severity="error"),
            ],
        )
        assert not r.is_valid
        msg = r.error_message
        assert msg is not None
        assert "error one" in msg
        assert "error two" in msg


class TestGeneratePrefixCode:
    """Test stub generation."""

    def test_includes_api_stubs(self) -> None:
        code = generate_prefix_code(include_apis=True)
        assert "def time_now()" in code
        assert "def json_encode(" in code
        assert "def llm(" in code
        assert "def wake_llm(" in code
        assert "MINUTE: int" in code

    def test_includes_tool_stubs(self) -> None:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "my_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "arg1": {"type": "string"},
                        },
                        "required": ["arg1"],
                    },
                },
            },
        ]
        code = generate_prefix_code(tool_definitions=tool_defs)
        assert "def my_tool(" in code
        assert "def tool_my_tool(" in code
        assert "arg1: str" in code

    def test_includes_input_variables(self) -> None:
        code = generate_prefix_code(input_names=["x", "y"])
        assert "x: Any" in code
        assert "y: Any" in code

    def test_no_apis_when_disabled(self) -> None:
        code = generate_prefix_code(include_apis=False)
        assert "time_now" not in code


class TestValidatorFailsClosed:
    """Test that infrastructure errors propagate rather than being swallowed."""

    def test_runtime_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RuntimeError during type_check should propagate, not be caught."""
        monkeypatch.setattr(
            pydantic_monty.Monty,
            "type_check",
            lambda self, **kw: (_ for _ in ()).throw(RuntimeError("checker crashed")),
        )
        v = ScriptValidator()
        with pytest.raises(RuntimeError, match="checker crashed"):
            v.validate("1 + 2")
