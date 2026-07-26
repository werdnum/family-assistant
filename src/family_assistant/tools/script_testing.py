"""LLM-assisted script testing with simulated tool outputs."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.infrastructure import (
    ToolDescriptorProvider,
    ToolNotFoundError,
    ToolProviderComposite,
    ToolProviderWrapper,
    ToolsProvider,
)
from family_assistant.tools.metadata import ToolDescriptor, ToolTag
from family_assistant.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.storage.repositories.message_history import ToolHistoryExample


logger = logging.getLogger(__name__)

_ACTION_TAGS = {
    ToolTag.STATE_CHANGING,
    ToolTag.DESTRUCTIVE,
    ToolTag.EXTERNAL_COMM,
    ToolTag.CODE_EXECUTION,
}
_MAX_EXAMPLES_PER_TOOL = 3


class SimulatedToolCallResponse(BaseModel):
    """Structured response from the simulation LLM."""

    return_value_json: str = Field(
        description=(
            "A valid JSON literal representing the tool return value exactly as the "
            "script should receive it."
        )
    )


class SimulatedToolCallRecord(BaseModel):
    """Serialized transcript entry for a simulated or passthrough tool call."""

    index: int
    tool_name: str
    mode: Literal["passthrough", "simulated"]
    # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON payloads keyed by tool schema
    arguments: dict[str, Any]
    result: Any
    result_text: str | None = None
    historical_examples_used: int = 0
    classification_reason: str


@runtime_checkable
class RawToolDefinitionsProvider(Protocol):
    """Protocol for providers that can expose raw tool definitions."""

    def get_raw_tool_definitions(self) -> list[ToolDefinition]: ...


class ScriptTestingToolsProvider(ToolsProvider):
    """Wrapper provider that simulates selected tools while passing others through."""

    def __init__(
        self,
        *,
        wrapped_provider: ToolsProvider,
        scenario_description: str,
        simulated_tools: set[str],
        passthrough_tools: set[str],
        execution_context: ToolExecutionContext,
    ) -> None:
        self._wrapped_provider = wrapped_provider
        self._scenario_description = scenario_description
        self._simulated_tools = simulated_tools
        self._passthrough_tools = passthrough_tools
        self._execution_context = execution_context
        self._transcript: list[SimulatedToolCallRecord] = []
        self._tool_definitions_by_name: dict[str, ToolDefinition] | None = None
        self._tool_descriptors_by_name: dict[str, ToolDescriptor] | None = None

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return the wrapped provider's tool definitions."""
        definitions = await self._wrapped_provider.get_tool_definitions()
        self._tool_definitions_by_name = {
            definition["function"]["name"]: definition for definition in definitions
        }
        return definitions

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from script tool calls
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Execute the tool for real or simulate it based on metadata and overrides."""
        mode, reason = await self._classify_tool(name)
        if mode == "passthrough":
            result = await self._wrapped_provider.execute_tool(
                name,
                arguments,
                context,
                call_id,
            )
            self._append_transcript(
                tool_name=name,
                mode=mode,
                arguments=arguments,
                result=result,
                classification_reason=reason,
                historical_examples_used=0,
            )
            return result

        examples = await self._execution_context.db_context.message_history.get_recent_tool_examples(
            interface_type=self._execution_context.interface_type,
            conversation_id=self._execution_context.conversation_id,
            subconversation_id=self._execution_context.subconversation_id,
            tool_name=name,
            limit=_MAX_EXAMPLES_PER_TOOL,
            user_id=self._execution_context.user_id,
        )
        simulated_result = await self._simulate_tool_call(
            name=name,
            arguments=arguments,
            examples=examples,
        )
        self._append_transcript(
            tool_name=name,
            mode=mode,
            arguments=arguments,
            result=simulated_result,
            classification_reason=reason,
            historical_examples_used=len(examples),
        )
        return simulated_result

    async def close(self) -> None:
        """Do not close the wrapped provider because it is shared."""
        return

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        """Return descriptors from the wrapped provider when available."""
        if not isinstance(self._wrapped_provider, ToolDescriptorProvider):
            return []
        descriptors = await self._wrapped_provider.get_tool_descriptors()
        self._tool_descriptors_by_name = {
            descriptor.name: descriptor for descriptor in descriptors
        }
        return descriptors

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        """Return the wrapped provider's tool descriptor when available."""
        if self._tool_descriptors_by_name is not None:
            cached_descriptor = self._tool_descriptors_by_name.get(name)
            if cached_descriptor is not None:
                return cached_descriptor
        if not isinstance(self._wrapped_provider, ToolDescriptorProvider):
            return None
        descriptor = await self._wrapped_provider.get_tool_descriptor(name)
        if descriptor is not None:
            if self._tool_descriptors_by_name is None:
                self._tool_descriptors_by_name = {}
            self._tool_descriptors_by_name[name] = descriptor
        return descriptor

    def get_raw_tool_definitions(self) -> list[ToolDefinition]:
        """Expose raw tool definitions so Monty can preserve attachment semantics."""
        return _collect_raw_tool_definitions(self._wrapped_provider)

    # ast-grep-ignore: no-dict-any - Transcript entries are JSON-like dicts for tool output reporting
    def get_transcript(self) -> list[dict[str, Any]]:
        """Return the collected tool transcript as JSON-friendly dicts."""
        return [entry.model_dump(mode="json") for entry in self._transcript]

    async def validate_requested_overrides(self) -> None:
        """Ensure the requested overrides are coherent and safe."""
        if self._simulated_tools & self._passthrough_tools:
            overlap = sorted(self._simulated_tools & self._passthrough_tools)
            msg = "Tools cannot be both simulated and passthrough: " + ", ".join(
                overlap
            )
            raise ValueError(msg)

        await self.get_tool_definitions()
        available_tool_names = set(self._tool_definitions_by_name or {})
        unknown_tools = sorted(
            (self._simulated_tools | self._passthrough_tools) - available_tool_names
        )
        if unknown_tools:
            msg = f"Unknown tool names: {', '.join(unknown_tools)}"
            raise ValueError(msg)

        for tool_name in sorted(self._passthrough_tools):
            descriptor = await self.get_tool_descriptor(tool_name)
            if descriptor is None or ToolTag.READ_ONLY not in descriptor.tags:
                raise ValueError(
                    f"Tool '{tool_name}' cannot be forced into passthrough mode."
                )
            if descriptor.tags & _ACTION_TAGS:
                raise ValueError(
                    f"Tool '{tool_name}' is action-oriented and cannot run for real in this tool."
                )

    async def _classify_tool(
        self,
        tool_name: str,
    ) -> tuple[Literal["passthrough", "simulated"], str]:
        """Classify the tool call using overrides and metadata."""
        if tool_name in self._simulated_tools:
            return "simulated", "explicitly requested for simulation"

        descriptor = await self.get_tool_descriptor(tool_name)
        if descriptor is None:
            if tool_name in self._passthrough_tools:
                raise ValueError(
                    "Tool "
                    f"'{tool_name}' cannot be forced into passthrough mode "
                    "because its metadata is unavailable."
                )
            return "simulated", "missing metadata defaults to simulation"

        if tool_name in self._passthrough_tools:
            return "passthrough", "explicitly allowed passthrough override"

        if descriptor.tags & _ACTION_TAGS:
            return "simulated", "action-oriented tool tags default to simulation"
        if ToolTag.READ_ONLY in descriptor.tags:
            return "passthrough", "read-only tool tags default to passthrough"
        return "simulated", "ambiguous metadata defaults to simulation"

    async def _simulate_tool_call(
        self,
        *,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from script tool calls
        arguments: dict[str, Any],
        examples: Sequence[ToolHistoryExample],
    ) -> ToolResult:
        """Use the default one-shot model to synthesize a realistic tool return value."""
        # Local import avoids a startup circular import through family_assistant.llm.
        from family_assistant.llm.one_shot import (  # noqa: PLC0415
            one_shot_structured,
        )

        tool_definition = await self._get_tool_definition(name)
        prompt = self._build_simulation_prompt(
            tool_name=name,
            arguments=arguments,
            tool_definition=tool_definition,
            examples=examples,
        )
        response = await one_shot_structured(
            prompt,
            system=(
                "You simulate Family Assistant tool outputs for script testing. "
                "Return only a realistic tool return value encoded as a JSON literal. "
                "Do not describe the simulation. Do not invent attachments. "
                "Preserve the real tool's style, field names, and return shape."
            ),
            response_model=SimulatedToolCallResponse,
        )

        try:
            return_value = json.loads(response.return_value_json)
        except json.JSONDecodeError as exc:
            msg = (
                f"Simulation for tool '{name}' returned invalid JSON literal: "
                f"{response.return_value_json}"
            )
            raise ValueError(msg) from exc

        if return_value is None:
            msg = (
                f"Simulation for tool '{name}' returned null, which this harness "
                "cannot represent in Monty script results. Return a concrete JSON "
                "value instead."
            )
            raise ValueError(msg)
        return ToolResult(data=return_value)

    async def _get_tool_definition(self, tool_name: str) -> ToolDefinition:
        """Fetch a single tool definition by name."""
        if self._tool_definitions_by_name is None:
            await self.get_tool_definitions()

        assert self._tool_definitions_by_name is not None
        definition = self._tool_definitions_by_name.get(tool_name)
        if definition is None:
            raise ToolNotFoundError(tool_name, type(self._wrapped_provider).__name__)
        return definition

    def _build_simulation_prompt(
        self,
        *,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON rendered into the simulator prompt
        arguments: dict[str, Any],
        tool_definition: ToolDefinition,
        examples: Sequence[ToolHistoryExample],
    ) -> str:
        """Construct a prompt for synthesizing a realistic tool result."""
        transcript_summary = [
            {
                "tool_name": entry.tool_name,
                "mode": entry.mode,
                "arguments": entry.arguments,
                "result": entry.result,
            }
            for entry in self._transcript
        ]
        example_lines = []
        for example in examples:
            example_lines.append(
                json.dumps(
                    {
                        "arguments": example.arguments,
                        "result_content": example.result_content,
                        "conversation_id": example.conversation_id,
                    },
                    ensure_ascii=False,
                )
            )

        return "\n".join([
            f"Scenario description:\n{self._scenario_description}",
            "",
            f"Tool name: {tool_name}",
            "Tool definition:",
            json.dumps(tool_definition, indent=2, ensure_ascii=False),
            "",
            "Current call arguments:",
            json.dumps(arguments, indent=2, ensure_ascii=False),
            "",
            "Prior tool transcript in this script run:",
            json.dumps(
                transcript_summary,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            "",
            "Historical examples from persisted tool history:",
            "\n".join(example_lines) if example_lines else "(none)",
            "",
            "Return requirements:",
            "- Produce the direct tool return value only.",
            "- The return value must be valid JSON and match the real tool's usual shape.",
            "- If the real tool usually returns a plain string, encode it as a JSON string literal.",
            "- For state-changing or communication tools, simulate the side effect in the return value but do not imply that it really happened.",
            "- Prefer the same wording and field names as the historical examples when they fit the current call.",
        ])

    def _append_transcript(
        self,
        *,
        tool_name: str,
        mode: Literal["passthrough", "simulated"],
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON payloads recorded in the transcript
        arguments: dict[str, Any],
        result: str | ToolResult,
        classification_reason: str,
        historical_examples_used: int,
    ) -> None:
        """Record the tool call in the transcript."""
        result_value, result_text = _serialize_tool_result(result)
        self._transcript.append(
            SimulatedToolCallRecord(
                index=len(self._transcript) + 1,
                tool_name=tool_name,
                mode=mode,
                arguments=arguments,
                result=result_value,
                result_text=result_text,
                historical_examples_used=historical_examples_used,
                classification_reason=classification_reason,
            )
        )


async def test_script_with_simulated_tools_tool(
    exec_context: ToolExecutionContext,
    script: str,
    scenario_description: str,
    simulated_tools: list[str] | None = None,
    passthrough_tools: list[str] | None = None,
    # ast-grep-ignore: no-dict-any - Script globals accept arbitrary JSON-like values keyed by name
    globals: dict[str, Any] | None = None,
) -> ToolResult:
    """Run a script with selected tools simulated by an LLM."""
    real_tools_provider = _get_tools_provider(exec_context)
    if real_tools_provider is None:
        error_text = "Error: test_script_with_simulated_tools requires an available tools provider."
        return _build_script_test_error_result(error_text)

    testing_provider = ScriptTestingToolsProvider(
        wrapped_provider=real_tools_provider,
        scenario_description=scenario_description,
        simulated_tools=set(simulated_tools or []),
        passthrough_tools=set(passthrough_tools or []),
        execution_context=exec_context,
    )

    try:
        await testing_provider.validate_requested_overrides()
    except ValueError as exc:
        return _build_script_test_error_result(f"Error: {exc}")

    script_exec_context = replace(exec_context, tools_provider=testing_provider)
    script_result = await execute_script_tool(
        script_exec_context,
        script=script,
        globals=globals,
    )

    transcript = testing_provider.get_transcript()
    script_error = _get_script_test_error(script_result)
    result_data = {
        "status": "error" if script_error is not None else "success",
        "script_result": script_result.get_data(),
        "script_result_text": script_result.text,
        "error": script_error,
        "transcript": transcript,
    }
    transcript_lines = [
        f"{entry['index']}. [{entry['mode']}] {entry['tool_name']} -> {_format_transcript_result_for_text(entry['result'])}"
        for entry in transcript
    ]
    transcript_block = (
        "\n--- Synthetic Tool Transcript ---\n" + "\n".join(transcript_lines)
        if transcript_lines
        else "\n--- Synthetic Tool Transcript ---\n(no tool calls)"
    )
    result_text = (script_result.text or "Script test completed.") + transcript_block

    return ToolResult(
        text=result_text,
        data=result_data,
        attachments=script_result.attachments,
    )


test_script_with_simulated_tools_tool.__test__ = False


SCRIPT_TESTING_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "test_script_with_simulated_tools",
            "description": (
                "Run a Monty script against the real Family Assistant tool registry while simulating selected "
                "tool outputs with the default LLM. Use this to test whether a script will survive contact "
                "with reality before running it against real action tools.\n\n"
                "Simulation rules:\n"
                "- Real registered tools are exposed to the script.\n"
                "- Read-only tools run for real by default.\n"
                "- State-changing, destructive, external-communication, and code-execution tools are simulated by default.\n"
                "- Simulated tools use the scenario description, the real tool schema, prior calls in this run, and "
                "historical examples from the same conversation or user when available.\n"
                "- Action tools are logged and simulated; they do not execute.\n"
                "- No attachments are synthesized in v1."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "The Monty script to execute.",
                    },
                    "scenario_description": {
                        "type": "string",
                        "description": (
                            "Plain-language description of the test scenario and how simulated tools should behave."
                        ),
                    },
                    "simulated_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional tool names to force into simulation mode even if they are read-only."
                        ),
                    },
                    "passthrough_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional read-only tool names to explicitly allow to run for real. "
                            "Action tools cannot be forced into passthrough."
                        ),
                    },
                    "globals": {
                        "type": "object",
                        "description": "Optional globals to inject into the script.",
                        "additionalProperties": True,
                    },
                },
                "required": ["script", "scenario_description"],
                "additionalProperties": False,
            },
        },
    }
]


def _get_tools_provider(exec_context: ToolExecutionContext) -> ToolsProvider | None:
    """Return the current tools provider from the execution context or processing service."""
    if exec_context.tools_provider is not None:
        return exec_context.tools_provider
    if (
        exec_context.processing_service is not None
        and hasattr(exec_context.processing_service, "tools_provider")
        and exec_context.processing_service.tools_provider is not None
    ):
        return exec_context.processing_service.tools_provider
    return None


def _format_transcript_result_for_text(result: object) -> str:
    """Render transcript results defensively for the user-facing text block."""
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


def _serialize_tool_result(result: str | ToolResult) -> tuple[Any, str | None]:
    """Convert a tool result into transcript-friendly structured data."""
    if isinstance(result, ToolResult):
        # ast-grep-ignore: no-dict-any - Transcript serialization preserves dynamic ToolResult payloads
        serialized: dict[str, Any] = {
            "data": result.get_data(),
            "text": result.text,
        }
        if result.attachments:
            serialized["attachments"] = [
                {
                    "attachment_id": attachment.attachment_id,
                    "mime_type": attachment.mime_type,
                    "description": attachment.description,
                }
                for attachment in result.attachments
            ]
        return serialized, result.text
    return result, result


def _collect_raw_tool_definitions(provider: ToolsProvider) -> list[ToolDefinition]:
    """Collect raw tool definitions through wrapper and composite providers."""
    if isinstance(provider, ToolProviderWrapper):
        return _collect_raw_tool_definitions(provider.wrapped_provider)

    if isinstance(provider, ToolProviderComposite):
        raw_definitions: list[ToolDefinition] = []
        for sub_provider in provider.get_providers():
            raw_definitions.extend(_collect_raw_tool_definitions(sub_provider))
        return raw_definitions

    if isinstance(provider, RawToolDefinitionsProvider):
        return provider.get_raw_tool_definitions() or []

    return []


def _get_script_test_error(script_result: ToolResult) -> str | None:
    """Return the script error text when execute_script reported a failure."""
    if not isinstance(script_result.data, dict):
        return None
    status = script_result.data.get("status")
    error_type = script_result.data.get("error_type")
    error = script_result.data.get("error")
    if (
        status != "error"
        or not isinstance(error_type, str)
        or not isinstance(error, str)
    ):
        return None
    return error


def _build_script_test_error_result(error_text: str) -> ToolResult:
    """Return a structured error envelope for tool-level validation failures."""
    return ToolResult(
        text=error_text,
        data={
            "status": "error",
            "script_result": None,
            "script_result_text": error_text,
            "error": error_text.removeprefix("Error: "),
            "transcript": [],
        },
    )
