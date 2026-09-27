"""Tests for the script validator (static type checking)."""

import ast
import inspect
from collections.abc import Callable

import pytest

from family_assistant.scripting.apis import time as time_api
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.validator import (
    ScriptValidator,
    ValidationDiagnostic,
    ValidationResult,
    generate_prefix_code,
)

DURATION_CONSTANTS = [
    "NANOSECOND",
    "MICROSECOND",
    "MILLISECOND",
    "SECOND",
    "MINUTE",
    "HOUR",
    "DAY",
    "WEEK",
]

TOOLS_API_SCRIPTS = ["tools_list()", 'tools_execute("search_notes")']
ATTACHMENT_API_SCRIPTS = [
    'attachment_get("id")',
    'attachment_create("content", "notes.txt")',
]


def _stub_functions(prefix_code: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(prefix_code)
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _stub_parameters(func: ast.FunctionDef) -> list[tuple[str, str, bool]]:
    """Return (name, kind, has_default) for each stub parameter, in order."""
    args = func.args
    positional = [*args.posonlyargs, *args.args]
    first_with_default = len(positional) - len(args.defaults)
    params = [
        (
            arg.arg,
            inspect.Parameter.POSITIONAL_ONLY.name
            if index < len(args.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD.name,
            index >= first_with_default,
        )
        for index, arg in enumerate(positional)
    ]
    if args.vararg:
        params.append((args.vararg.arg, inspect.Parameter.VAR_POSITIONAL.name, False))
    params.extend(
        (arg.arg, inspect.Parameter.KEYWORD_ONLY.name, default is not None)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    if args.kwarg:
        params.append((args.kwarg.arg, inspect.Parameter.VAR_KEYWORD.name, False))
    return params


def _runtime_parameters(func: Callable[..., object]) -> list[tuple[str, str, bool]]:
    """Return (name, kind, has_default) for each script-visible runtime parameter.

    Underscore-prefixed parameters (e.g. ``_default_tz``) are runtime-internal
    wiring: MontyEngine binds them before scripts see the function, so they are
    intentionally absent from the stub.
    """
    return [
        (param.name, param.kind.name, param.default is not inspect.Parameter.empty)
        for param in inspect.signature(func).parameters.values()
        if not param.name.startswith("_")
    ]


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

    @pytest.mark.parametrize(
        "script", ['json_encode({"key": "value"})', 'json_decode("[1, 2]")']
    )
    def test_json_api_is_known(self, script: str) -> None:
        v = ScriptValidator()
        result = v.validate(script)
        assert result.is_valid, result.error_message

    def test_llm_api_is_known(self) -> None:
        v = ScriptValidator()
        result = v.validate('llm("summarize this")')
        assert result.is_valid

    def test_wake_llm_is_known(self) -> None:
        v = ScriptValidator()
        result = v.validate('wake_llm("hello")')
        assert result.is_valid

    @pytest.mark.parametrize("name", DURATION_CONSTANTS)
    def test_duration_constants_are_known(self, name: str) -> None:
        v = ScriptValidator()
        result = v.validate(f"t = time_now()\ntime_add(t, 5 * {name})")
        assert result.is_valid, result.error_message

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

    def test_tool_argument_of_wrong_type_is_error(
        self, sample_tool_definitions: list
    ) -> None:
        v = ScriptValidator(tool_definitions=sample_tool_definitions)
        result = v.validate("search_notes(query=123)")
        assert not result.is_valid
        assert any("search_notes" in d.message for d in result.errors)

    def test_multi_tool_script(self, sample_tool_definitions: list) -> None:
        v = ScriptValidator(tool_definitions=sample_tool_definitions)
        script = """
notes = search_notes(query="project")
add_or_update_note(title="Summary", content="Found notes")
"""
        result = v.validate(script)
        assert result.is_valid


class TestScriptValidatorConfig:
    """Test configuration options."""

    @pytest.mark.parametrize("script", ["time_now()", "5 * MINUTE"])
    def test_disable_time_api(self, script: str) -> None:
        config = ScriptConfig(enable_time_api=False)
        v = ScriptValidator(config=config)
        result = v.validate(script)
        assert not result.is_valid

    @pytest.mark.parametrize("script", ['llm("hi")', 'llm_json("hi")'])
    def test_disable_llm_api(self, script: str) -> None:
        config = ScriptConfig(enable_llm_api=False)
        v = ScriptValidator(config=config)
        result = v.validate(script)
        assert not result.is_valid

    @pytest.mark.parametrize("script", ['json_encode({"a": 1})', 'json_decode("1")'])
    def test_disable_json_api(self, script: str) -> None:
        config = ScriptConfig(enable_json_api=False)
        v = ScriptValidator(config=config)
        result = v.validate(script)
        assert not result.is_valid

    @pytest.mark.parametrize(
        ("config", "other_apis_script"),
        [
            pytest.param(
                ScriptConfig(enable_time_api=False),
                'llm("hi")\njson_encode({"a": 1})',
                id="time-disabled",
            ),
            pytest.param(
                ScriptConfig(enable_llm_api=False),
                'time_now()\njson_encode({"a": 1})',
                id="llm-disabled",
            ),
            pytest.param(
                ScriptConfig(enable_json_api=False),
                'time_now()\nllm("hi")',
                id="json-disabled",
            ),
        ],
    )
    def test_apis_independent(
        self, config: ScriptConfig, other_apis_script: str
    ) -> None:
        """Disabling one API does not disable the others."""
        v = ScriptValidator(config=config)
        result = v.validate(other_apis_script)
        assert result.is_valid, result.error_message

    def test_all_apis_disabled_still_has_wake_llm(self) -> None:
        config = ScriptConfig(
            enable_json_api=False,
            enable_time_api=False,
            enable_llm_api=False,
        )
        v = ScriptValidator(config=config)
        result = v.validate('wake_llm("hello")')
        assert result.is_valid

    @pytest.mark.parametrize("script", TOOLS_API_SCRIPTS)
    def test_exclude_tools_api(self, script: str) -> None:
        v = ScriptValidator()
        result = v.validate(script, include_tools_api=False)
        assert not result.is_valid

    @pytest.mark.parametrize("script", TOOLS_API_SCRIPTS)
    def test_include_tools_api_by_default(self, script: str) -> None:
        v = ScriptValidator()
        result = v.validate(script)
        assert result.is_valid, result.error_message

    @pytest.mark.parametrize("script", ATTACHMENT_API_SCRIPTS)
    def test_exclude_attachment_api(self, script: str) -> None:
        v = ScriptValidator()
        result = v.validate(script, include_attachment_api=False)
        assert not result.is_valid

    @pytest.mark.parametrize("script", ATTACHMENT_API_SCRIPTS)
    def test_include_attachment_api_by_default(self, script: str) -> None:
        v = ScriptValidator()
        result = v.validate(script)
        assert result.is_valid, result.error_message

    def test_exclude_both_tools_and_attachment_api(self) -> None:
        v = ScriptValidator()
        result = v.validate(
            'time_now()\nllm("hi")',
            include_tools_api=False,
            include_attachment_api=False,
        )
        assert result.is_valid, result.error_message


class TestValidationResult:
    """Test ValidationResult structure."""

    def test_no_diagnostics_has_no_errors_or_message(self) -> None:
        result = ValidationResult(is_valid=True, diagnostics=[])
        assert result.errors == []
        assert result.error_message is None

    @pytest.fixture
    def mixed_result(self) -> ValidationResult:
        return ValidationResult(
            is_valid=False,
            diagnostics=[
                ValidationDiagnostic(message="error one", line=1, severity="error"),
                ValidationDiagnostic(
                    message="just a warning", line=2, severity="warning"
                ),
                ValidationDiagnostic(message="error two", line=3, severity="error"),
            ],
        )

    def test_errors_excludes_warnings(self, mixed_result: ValidationResult) -> None:
        assert [d.message for d in mixed_result.errors] == ["error one", "error two"]

    def test_error_message_joins_only_errors(
        self, mixed_result: ValidationResult
    ) -> None:
        assert (
            mixed_result.error_message
            == "error at line 1: error one; error at line 3: error two"
        )

    def test_error_message_is_none_when_only_warnings(self) -> None:
        r = ValidationResult(
            is_valid=True,
            diagnostics=[
                ValidationDiagnostic(message="just a warning", severity="warning")
            ],
        )
        assert r.error_message is None


class TestIdentifierValidation:
    """Test handling of invalid Python identifiers in tool/input names."""

    @pytest.fixture
    def optional_listed_first_tool(self) -> list:
        return [
            {
                "type": "function",
                "function": {
                    "name": "my_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "optional_first": {"type": "string"},
                            "required_param": {"type": "string"},
                        },
                        "required": ["required_param"],
                    },
                },
            },
        ]

    @pytest.mark.parametrize("script", ["search()", "tool_search()"])
    def test_tool_with_hyphenated_name_declares_no_function(self, script: str) -> None:
        """A partially parsed ``def search-notes()`` stub must not declare ``search``."""
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "search-notes",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        v = ScriptValidator(tool_definitions=tool_defs)
        result = v.validate(script)
        assert not result.is_valid

    def test_tool_with_keyword_param_accepts_all_keyword_arguments(self) -> None:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "my_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "class": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                        "required": ["class"],
                    },
                },
            },
        ]
        v = ScriptValidator(tool_definitions=tool_defs)
        result = v.validate('my_tool(limit=5, **{"class": "x"})')
        assert result.is_valid, result.error_message

    @pytest.mark.parametrize("bad_name", ["invalid-name", "class"])
    def test_invalid_input_name_is_rejected(self, bad_name: str) -> None:
        v = ScriptValidator()
        result = v.validate("valid_name", input_names=["valid_name", bad_name])
        assert not result.is_valid
        assert bad_name in (result.error_message or "")

    def test_tool_required_params_before_optional(
        self, optional_listed_first_tool: list
    ) -> None:
        v = ScriptValidator(tool_definitions=optional_listed_first_tool)
        result = v.validate('my_tool("a", optional_first="b")')
        assert result.is_valid, result.error_message

    def test_tool_optional_params_are_keyword_only(
        self, optional_listed_first_tool: list
    ) -> None:
        v = ScriptValidator(tool_definitions=optional_listed_first_tool)
        result = v.validate('my_tool("a", "b")')
        assert not result.is_valid

    def test_json_schema_list_type_accepts_each_member(self) -> None:
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
        v = ScriptValidator(tool_definitions=tool_defs)
        result = v.validate('nullable_tool(value=None)\nnullable_tool(value="s")')
        assert result.is_valid, result.error_message

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

    def test_tool_positional_args_accepted(self) -> None:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "search_notes",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            },
        ]
        v = ScriptValidator(tool_definitions=tool_defs)
        result = v.validate('search_notes("TODO")')
        assert result.is_valid

    @pytest.mark.parametrize(
        ("script", "expected_valid"),
        [
            pytest.param('ordered_tool(1, "x")', True, id="required-list-order"),
            pytest.param('ordered_tool("x", 1)', False, id="property-order"),
        ],
    )
    def test_tool_required_params_ordered_by_required_list(
        self, script: str, expected_valid: bool
    ) -> None:
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "ordered_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "b_param": {"type": "string"},
                            "a_param": {"type": "integer"},
                        },
                        "required": ["a_param", "b_param"],
                    },
                },
            },
        ]
        v = ScriptValidator(tool_definitions=tool_defs)
        result = v.validate(script)
        assert result.is_valid is expected_valid, result.error_message


class TestStubSignaturesMatchRuntime:
    """Verify that validator stubs match actual runtime API signatures.

    These tests catch signature drift between the validator stubs
    (in validator.py) and the actual runtime implementations: a stub that
    diverges makes the validator reject scripts that would run, or accept
    scripts that would fail.
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

    def test_time_api_stubs_match_runtime_signatures(self) -> None:
        """The time API stubs declare exactly the runtime functions and parameters.

        Each parameter is compared by name, position, kind and whether it has a
        default, so missing, extra, renamed, reordered or wrongly-required
        parameters all fail.
        """
        stubs = _stub_functions(generate_prefix_code())
        stubs_without_time_api = _stub_functions(
            generate_prefix_code(include_time_api=False)
        )
        stub_signatures = {
            name: _stub_parameters(func)
            for name, func in stubs.items()
            if name not in stubs_without_time_api
        }
        runtime_signatures = {
            name: _runtime_parameters(func)
            for name, func in inspect.getmembers(time_api, inspect.isfunction)
            if not name.startswith("_") and func.__module__ == time_api.__name__
        }
        assert stub_signatures == runtime_signatures

    @pytest.mark.parametrize("name", ["NANOSECOND", "MICROSECOND", "MILLISECOND"])
    def test_fractional_duration_constants_are_not_typed_as_int(
        self, name: str
    ) -> None:
        assert isinstance(getattr(time_api, name), float)
        v = ScriptValidator()
        result = v.validate(f"x: int = {name}")
        assert not result.is_valid

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
