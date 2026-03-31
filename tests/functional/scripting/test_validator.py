"""Tests for the script validator (static type checking)."""

import inspect

import pytest

from family_assistant.scripting.apis import time as time_api
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

    def test_callable_globals_accepted_as_external_functions(self) -> None:
        """Callable globals should be passed as extra_external_functions, not input_names."""
        v = ScriptValidator()
        result = v.validate(
            "my_helper(42)",
            extra_external_functions=["my_helper"],
        )
        assert result.is_valid

    def test_callable_global_rejected_without_declaration(self) -> None:
        v = ScriptValidator()
        result = v.validate("my_helper(42)")
        assert not result.is_valid


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

    def test_exclude_tools_api(self) -> None:
        v = ScriptValidator()
        result = v.validate("tools_list()", include_tools_api=False)
        assert not result.is_valid

    def test_include_tools_api_by_default(self) -> None:
        v = ScriptValidator()
        result = v.validate("tools_list()")
        assert result.is_valid

    def test_exclude_attachment_api(self) -> None:
        v = ScriptValidator()
        result = v.validate('attachment_get("id")', include_attachment_api=False)
        assert not result.is_valid

    def test_include_attachment_api_by_default(self) -> None:
        v = ScriptValidator()
        result = v.validate('attachment_get("id")')
        assert result.is_valid

    def test_exclude_both_tools_and_attachment_api(self) -> None:
        v = ScriptValidator()
        result = v.validate(
            "time_now()",
            include_tools_api=False,
            include_attachment_api=False,
        )
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
        assert "MINUTE: float" in code

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

    def test_no_tools_api_when_excluded(self) -> None:
        code = generate_prefix_code(include_tools_api=False)
        assert "def tools_list(" not in code
        assert "def tools_execute(" not in code
        assert "def time_now(" in code

    def test_no_attachment_api_when_excluded(self) -> None:
        code = generate_prefix_code(include_attachment_api=False)
        assert "def attachment_get(" not in code
        assert "def attachment_create(" not in code
        assert "def time_now(" in code

    def test_both_apis_excluded_still_has_core(self) -> None:
        code = generate_prefix_code(
            include_tools_api=False, include_attachment_api=False
        )
        assert "def time_now(" in code
        assert "def llm(" in code
        assert "def tools_list(" not in code
        assert "def attachment_get(" not in code


class TestIdentifierValidation:
    """Test handling of invalid Python identifiers in tool/input names."""

    def test_tool_with_hyphenated_name_is_skipped(self) -> None:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "search-notes",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        code = generate_prefix_code(tool_definitions=tool_defs)
        assert "search-notes" not in code

    def test_tool_with_keyword_param_falls_back_to_kwargs(self) -> None:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "my_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"class": {"type": "string"}},
                        "required": ["class"],
                    },
                },
            },
        ]
        code = generate_prefix_code(tool_definitions=tool_defs)
        assert "def my_tool(**kwargs: Any)" in code

    def test_invalid_input_name_is_skipped(self) -> None:
        code = generate_prefix_code(input_names=["valid_name", "invalid-name", "class"])
        assert "valid_name: Any" in code
        assert "invalid-name" not in code
        assert "class:" not in code

    def test_json_schema_list_type(self) -> None:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "nullable_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "value": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        ]
        code = generate_prefix_code(tool_definitions=tool_defs)
        assert "str | Any" in code

    def test_tool_with_valid_name_and_params_works(self) -> None:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "good_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            },
        ]
        v = ScriptValidator(tool_definitions=tool_defs)
        result = v.validate('good_tool(query="test")')
        assert result.is_valid


class TestStubSignaturesMatchRuntime:
    """Verify that validator stubs match actual runtime API signatures.

    These tests catch signature drift between the validator stubs
    (in validator.py) and the actual runtime implementations. Each
    test calls a function with the real parameter names from the
    runtime API — if the validator stub has different param names,
    the script will fail validation.
    """

    # Mapping of runtime API functions to example call scripts.
    # Each script uses keyword arguments matching the real function signature.
    TIME_API_CALLS: list[tuple[str, str]] = [
        ("time_now", "time_now()"),
        ("time_now_utc", "time_now_utc()"),
        (
            "time_create",
            "time_create(year=2024, month=1, day=1, timezone_name='UTC')",
        ),
        (
            "time_from_timestamp",
            "time_from_timestamp(seconds=1000000.0, nanoseconds=0)",
        ),
        (
            "time_parse",
            "time_parse(time_string='2024-01-01', format_string='', timezone_name='')",
        ),
        (
            "time_in_location",
            "time_in_location(time_dict=time_now(), timezone_name='US/Eastern')",
        ),
        ("time_format", "time_format(time_dict=time_now(), format_string='%Y-%m-%d')"),
        ("time_add", "time_add(time_dict=time_now(), seconds=60.0)"),
        (
            "time_add_duration",
            "time_add_duration(time_dict=time_now(), amount=1.0, unit='hours')",
        ),
        ("time_year", "time_year(time_dict=time_now())"),
        ("time_month", "time_month(time_dict=time_now())"),
        ("time_day", "time_day(time_dict=time_now())"),
        ("time_hour", "time_hour(time_dict=time_now())"),
        ("time_minute", "time_minute(time_dict=time_now())"),
        ("time_second", "time_second(time_dict=time_now())"),
        ("time_weekday", "time_weekday(time_dict=time_now())"),
        ("time_before", "time_before(t1=time_now(), t2=time_now())"),
        ("time_after", "time_after(t1=time_now(), t2=time_now())"),
        ("time_equal", "time_equal(t1=time_now(), t2=time_now())"),
        ("time_diff", "time_diff(t1=time_now(), t2=time_now())"),
        ("duration_parse", "duration_parse(duration_string='1h30m')"),
        ("duration_human", "duration_human(seconds=3600.0)"),
        ("timezone_is_valid", "timezone_is_valid(timezone_name='UTC')"),
        ("timezone_offset", "timezone_offset(timezone_name='UTC')"),
        ("is_between", "is_between(start_hour=9, end_hour=17)"),
        ("is_weekend", "is_weekend()"),
    ]

    @pytest.mark.parametrize(
        ("func_name", "call_script"),
        TIME_API_CALLS,
        ids=[name for name, _ in TIME_API_CALLS],
    )
    def test_time_api_call_validates(self, func_name: str, call_script: str) -> None:
        """Calling a time API function with its real param names should pass validation."""
        v = ScriptValidator()
        result = v.validate(call_script)
        assert result.is_valid, f"{func_name}: {result.error_message}"

    def test_all_time_api_functions_have_stubs(self) -> None:
        """Every function registered in MontyEngine._add_time_api must have a stub."""
        prefix = generate_prefix_code(include_apis=True)
        # Get all public functions from time_api module
        api_functions = [
            name
            for name, obj in inspect.getmembers(time_api, inspect.isfunction)
            if not name.startswith("_") and obj.__module__ == time_api.__name__
        ]
        missing = [fn for fn in api_functions if f"def {fn}(" not in prefix]
        assert not missing, f"Missing stubs for time API functions: {missing}"

    def test_all_time_api_param_names_match(self) -> None:
        """Stub param names must match actual runtime function param names."""
        prefix = generate_prefix_code(include_apis=True)
        api_functions = {
            name: obj
            for name, obj in inspect.getmembers(time_api, inspect.isfunction)
            if not name.startswith("_") and obj.__module__ == time_api.__name__
        }
        mismatches: list[str] = []
        for name, fn in api_functions.items():
            sig = inspect.signature(fn)
            real_params = [p.name for p in sig.parameters.values() if p.name != "self"]
            # Extract the stub line for this function
            stub_line = ""
            for line in prefix.splitlines():
                if f"def {name}(" in line:
                    stub_line = line
                    break
            if not stub_line:
                mismatches.append(f"{name}: no stub found")
                continue
            for param in real_params:
                if param not in stub_line:
                    mismatches.append(
                        f"{name}: param '{param}' not in stub: {stub_line.strip()}"
                    )
        assert not mismatches, "Stub/runtime param mismatches:\n" + "\n".join(
            mismatches
        )

    def test_duration_constants_are_floats(self) -> None:
        """Duration constants in the runtime are floats (seconds), stubs should match."""
        prefix = generate_prefix_code(include_apis=True)
        for name in [
            "NANOSECOND",
            "MICROSECOND",
            "MILLISECOND",
            "SECOND",
            "MINUTE",
            "HOUR",
            "DAY",
            "WEEK",
        ]:
            assert f"{name}: float" in prefix, f"{name} should be typed as float"

    def test_json_api_validates(self) -> None:
        v = ScriptValidator()
        result = v.validate('json_encode(obj={"key": "val"})')
        assert result.is_valid, result.error_message

    def test_llm_api_validates(self) -> None:
        v = ScriptValidator()
        result = v.validate('llm(prompt="hello", system="you are helpful")')
        assert result.is_valid, result.error_message

    def test_llm_json_api_validates(self) -> None:
        v = ScriptValidator()
        result = v.validate('llm_json(prompt="hello")')
        assert result.is_valid, result.error_message

    def test_attachment_api_validates(self) -> None:
        v = ScriptValidator()
        result = v.validate('attachment_get(attachment_id="abc")')
        assert result.is_valid, result.error_message

    def test_tools_meta_api_validates(self) -> None:
        v = ScriptValidator()
        result = v.validate("tools_list()")
        assert result.is_valid, result.error_message
