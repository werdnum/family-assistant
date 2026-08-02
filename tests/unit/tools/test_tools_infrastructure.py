"""Tests for the tools infrastructure module."""

from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    ToolPolicyDeniedError,
    resolve_descriptors_version,
)
from family_assistant.tools.metadata import ToolDescriptor, ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolArguments,
    ToolDefinition,
    ToolExecutionContext,
)


class TestLocalToolsProvider:
    """Test cases for LocalToolsProvider."""

    @pytest.mark.asyncio
    async def test_execute_tool_dict_result_json_formatting(self) -> None:
        """Test that dict results are properly converted to JSON strings."""

        # Define a tool that returns a dict
        async def tool_returns_dict(**kwargs: Any) -> dict:  # noqa: ANN401 # Test tool needs flexibility
            return {"status": "success", "data": {"value": 42, "message": "test"}}

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_dict",
                        "description": "Test tool that returns a dict",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_dict": tool_returns_dict},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-1",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        result = await provider.execute_tool("tool_returns_dict", {}, context)

        # Result should be a JSON string, not Python dict string representation
        assert isinstance(result, str)
        # Should be valid JSON
        parsed = json.loads(result)
        assert parsed == {"status": "success", "data": {"value": 42, "message": "test"}}
        # Should NOT contain Python-style single quotes
        assert "'" not in result
        # Should contain proper JSON double quotes
        assert '"status"' in result
        assert '"success"' in result

    @pytest.mark.asyncio
    async def test_execute_tool_list_result_json_formatting(self) -> None:
        """Test that list results are properly converted to JSON strings."""

        # Define a tool that returns a list
        async def tool_returns_list(**kwargs: Any) -> list:  # noqa: ANN401 # Test tool needs flexibility
            return [
                {"id": 1, "name": "first"},
                {"id": 2, "name": "second"},
                {"id": 3, "name": "third"},
            ]

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_list",
                        "description": "Test tool that returns a list",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_list": tool_returns_list},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-2",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        result = await provider.execute_tool("tool_returns_list", {}, context)

        # Result should be a JSON string
        assert isinstance(result, str)
        # Should be valid JSON
        parsed = json.loads(result)
        assert len(parsed) == 3
        assert parsed[0] == {"id": 1, "name": "first"}
        # Should NOT contain Python-style single quotes
        assert "'" not in result
        # Should be properly formatted JSON
        assert '"id"' in result
        assert '"name"' in result

    @pytest.mark.asyncio
    async def test_execute_tool_complex_nested_result_json_formatting(self) -> None:
        """Test that complex nested structures are properly converted to JSON."""

        # Define a tool that returns a complex nested structure
        async def tool_returns_complex(**kwargs: Any) -> dict:  # noqa: ANN401 # Test tool needs flexibility
            return {
                "metadata": {"version": "1.0", "timestamp": "2025-01-01T00:00:00Z"},
                "items": [
                    {"type": "A", "values": [1, 2, 3], "active": True},
                    {"type": "B", "values": [4, 5, 6], "active": False},
                ],
                "summary": {"total": 6, "types": ["A", "B"]},
                "special_chars": {
                    "unicode": "Hello 世界",
                    "quotes": 'test "quoted" value',
                },
            }

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_complex",
                        "description": "Test tool that returns complex nested data",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_complex": tool_returns_complex},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-3",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        result = await provider.execute_tool("tool_returns_complex", {}, context)

        # Result should be valid JSON
        assert isinstance(result, str)
        parsed = json.loads(result)

        # Check structure is preserved
        assert "metadata" in parsed
        assert parsed["metadata"]["version"] == "1.0"
        assert len(parsed["items"]) == 2
        assert parsed["summary"]["total"] == 6

        # Check special characters are handled correctly
        assert parsed["special_chars"]["unicode"] == "Hello 世界"
        assert parsed["special_chars"]["quotes"] == 'test "quoted" value'

        # Ensure it's proper JSON formatting
        assert "'" not in result or (
            '"' in result and result.count('"') > result.count("'")
        )
        assert result.strip().startswith("{")
        assert result.strip().endswith("}")

    @pytest.mark.asyncio
    async def test_execute_tool_none_result_handling(self) -> None:
        """Test that None results are handled gracefully."""

        # Define a tool that returns None
        async def tool_returns_none(**kwargs: Any) -> None:  # noqa: ANN401 # Test tool needs flexibility
            return None

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_none",
                        "description": "Test tool that returns None",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_none": tool_returns_none},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-4",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        result = await provider.execute_tool("tool_returns_none", {}, context)

        # Should return a descriptive message
        assert isinstance(result, str)
        assert "None" in result
        assert "successfully" in result

    @pytest.mark.asyncio
    async def test_execute_tool_string_result_unchanged(self) -> None:
        """Test that string results are returned unchanged."""

        # Define a tool that returns a string
        async def tool_returns_string(**kwargs: Any) -> str:  # noqa: ANN401 # Test tool needs flexibility
            return "This is a plain string result"

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_string",
                        "description": "Test tool that returns a string",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_string": tool_returns_string},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-5",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        result = await provider.execute_tool("tool_returns_string", {}, context)

        # Should return the string unchanged
        assert result == "This is a plain string result"

    @pytest.mark.asyncio
    async def test_execute_tool_number_result_stringified(self) -> None:
        """Test that numeric results are converted to strings."""

        # Define a tool that returns a number
        async def tool_returns_number(**kwargs: Any) -> int:  # noqa: ANN401 # Test tool needs flexibility
            return 42

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_number",
                        "description": "Test tool that returns a number",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_number": tool_returns_number},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-6",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        result = await provider.execute_tool("tool_returns_number", {}, context)

        # Should be converted to string
        assert isinstance(result, str)
        assert result == "42"

    @pytest.mark.asyncio
    async def test_execute_tool_invalid_attachment_fails_gracefully(self) -> None:
        """Test that tools with invalid attachment IDs fail with proper error messages."""

        # Define a tool that expects an attachment parameter
        async def tool_with_attachment(
            exec_context: ToolExecutionContext,
            image_attachment_id: Any,  # noqa: ANN401
        ) -> str:
            # This should never be reached when attachment is invalid
            return f"Processed attachment: {image_attachment_id}"

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_with_attachment",
                        "description": "Test tool that requires an attachment",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "image_attachment_id": {
                                    "type": "attachment",
                                    "description": "An attachment ID",
                                }
                            },
                            "required": ["image_attachment_id"],
                        },
                    },
                }
            ],
            implementations={"tool_with_attachment": tool_with_attachment},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-attachment",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,  # No attachment registry
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        # Test with a valid UUID format but non-existent attachment
        result = await provider.execute_tool(
            "tool_with_attachment",
            {"image_attachment_id": "5d8f4d9c-8a8d-4f9e-8b3a-9b7e3d6a1b1a"},
            context,
        )

        # Should return an error message, not crash
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert "not found or access denied" in result
        assert "image_attachment_id" in result

    @pytest.mark.asyncio
    async def test_execute_tool_injects_exec_context(self) -> None:
        """Test that exec_context is properly injected into tool functions."""
        received_context: list[ToolExecutionContext | None] = [None]

        async def tool_needs_exec_context(
            exec_context: ToolExecutionContext,
            query: str,
        ) -> str:
            received_context[0] = exec_context
            return f"Got query: {query}"

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_needs_exec_context",
                        "description": "Test tool",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                            "required": ["query"],
                        },
                    },
                }
            ],
            implementations={"tool_needs_exec_context": tool_needs_exec_context},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-inject",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        result = await provider.execute_tool(
            "tool_needs_exec_context", {"query": "test"}, context
        )

        assert result == "Got query: test"
        assert received_context[0] is context
        assert received_context[0] is not None
        assert received_context[0].db_context is mock_db_context

    @pytest.mark.asyncio
    async def test_execute_tool_injects_db_context(self) -> None:
        """Test that db_context is properly injected into tool functions."""
        received_db: list[Database | None] = [None]

        async def tool_needs_db_context(
            db_context: Database,
            query: str,
        ) -> str:
            received_db[0] = db_context
            return f"Got query: {query}"

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_needs_db_context",
                        "description": "Test tool",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                            "required": ["query"],
                        },
                    },
                }
            ],
            implementations={"tool_needs_db_context": tool_needs_db_context},
        )

        mock_db_context = MagicMock(spec=Database)
        context = ToolExecutionContext(
            conversation_id="test-conv-db",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            credential_resolvers=None,
            api_backend=None,
        )

        result = await provider.execute_tool(
            "tool_needs_db_context", {"query": "test"}, context
        )

        assert result == "Got query: test"
        assert received_db[0] is mock_db_context

    @pytest.mark.asyncio
    async def test_execute_tool_injects_db_context_with_string_annotation(
        self,
    ) -> None:
        """Test that db_context is injected even when the annotation is a string.

        This simulates the case where a tool module uses `from __future__ import annotations`
        and imports Database under TYPE_CHECKING, so get_type_hints() cannot
        resolve the annotation and falls back to the raw string.
        """
        received_db: list[Any] = [None]

        async def tool_with_string_annotation(
            db_context: Database,
            query: str,
        ) -> str:
            received_db[0] = db_context
            return f"Got query: {query}"

        # Simulate a module where Database is NOT in the namespace
        # (as if imported only under TYPE_CHECKING)
        fake_module = types.ModuleType("fake_tool_module")
        fake_module.__dict__["__name__"] = "fake_tool_module"
        tool_with_string_annotation.__module__ = "fake_tool_module"
        # Patch so inspect.getmodule finds our fake module
        sys.modules["fake_tool_module"] = fake_module

        try:
            provider = LocalToolsProvider(
                definitions=[
                    {
                        "type": "function",
                        "function": {
                            "name": "tool_with_string_annotation",
                            "description": "Test tool",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                },
                                "required": ["query"],
                            },
                        },
                    }
                ],
                implementations={
                    "tool_with_string_annotation": tool_with_string_annotation
                },
            )

            mock_db_context = MagicMock(spec=Database)
            context = ToolExecutionContext(
                conversation_id="test-conv-str-db",
                user_name="test-user",
                interface_type="test",
                timezone=ZoneInfo("UTC"),
                turn_id=None,
                db_context=mock_db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
                camera_backend=None,
                credential_resolvers=None,
                api_backend=None,
            )

            result = await provider.execute_tool(
                "tool_with_string_annotation", {"query": "test"}, context
            )

            assert result == "Got query: test"
            assert received_db[0] is mock_db_context
        finally:
            del sys.modules["fake_tool_module"]


class TestPolicyConfirmationFlow:
    """Test cases for policy-driven confirmation."""

    @pytest.mark.asyncio
    async def test_execute_tool_passes_confirmation_callback_arguments_by_name(
        self,
    ) -> None:
        """Ensure confirmation callbacks receive semantically correct named values."""

        class StubToolsProvider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object], str | None]] = []
                self.descriptor = ToolDescriptor(
                    name="dangerous_tool",
                    definition={
                        "type": "function",
                        "function": {
                            "name": "dangerous_tool",
                            "description": "Tool requiring confirmation",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                    tags=frozenset({ToolTag.DESTRUCTIVE}),
                    origin="local",
                )

            async def get_tool_definitions(self) -> list[ToolDefinition]:
                return [self.descriptor.definition]

            async def get_tool_descriptors(self) -> list[ToolDescriptor]:
                return [self.descriptor]

            async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
                return self.descriptor if name == self.descriptor.name else None

            async def execute_tool(
                self,
                name: str,
                arguments: dict[str, object],
                context: ToolExecutionContext,
                call_id: str | None = None,
            ) -> str:
                self.calls.append((name, arguments, call_id))
                return "executed"

            async def close(self) -> None:
                return None

        wrapped_provider = StubToolsProvider()
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=wrapped_provider,
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(
                    default_decision=ToolPolicyDecision.DENY,
                    rules=[
                        PolicyRule(
                            match=ToolMatcher(names=["dangerous_tool"]),
                            decision=ToolPolicyDecision.CONFIRM,
                        ),
                    ],
                )
            ),
            confirmation_timeout=42.0,
        )

        captured: dict[str, object] = {}

        async def confirmation_callback(
            interface_type: str,
            conversation_id: str,
            turn_id: str | None,
            tool_name: str,
            call_id: str,
            tool_args: ToolArguments,
            timeout_seconds: float,
            context: ToolExecutionContext,
        ) -> ConfirmationOutcome:
            captured["interface_type"] = interface_type
            captured["conversation_id"] = conversation_id
            captured["turn_id"] = turn_id
            captured["tool_name"] = tool_name
            captured["call_id"] = call_id
            captured["tool_args"] = tool_args
            captured["timeout_seconds"] = timeout_seconds
            captured["context"] = context
            return ConfirmationOutcome(kind="approved")

        mock_db_context = MagicMock(spec=Database)
        exec_context = ToolExecutionContext(
            conversation_id="conv-1",
            user_name="test-user",
            interface_type="telegram",
            timezone=ZoneInfo("UTC"),
            turn_id="turn-1",
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            request_confirmation_callback=confirmation_callback,
            credential_resolvers=None,
            api_backend=None,
        )

        tool_args = {"title": "Hello"}
        result = await provider.execute_tool(
            "dangerous_tool",
            tool_args,
            exec_context,
            call_id="call-explicit-123",
        )

        assert result == "executed"
        assert captured["tool_name"] == "dangerous_tool"
        assert captured["call_id"] == "call-explicit-123"
        assert captured["tool_args"] == tool_args
        assert captured["timeout_seconds"] == 42.0
        assert captured["context"] is exec_context
        assert wrapped_provider.calls == [
            ("dangerous_tool", tool_args, "call-explicit-123")
        ]


class TestPolicyEnforcingToolsProvider:
    """Test cases for PolicyEnforcingToolsProvider."""

    @staticmethod
    def _make_descriptor(
        name: str,
        *,
        tags: set[ToolTag],
    ) -> ToolDescriptor:
        return ToolDescriptor(
            name=name,
            definition={
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"{name} description",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            tags=frozenset(tags),
            origin="local",
        )

    @staticmethod
    def _make_context(
        *,
        request_confirmation_callback: Any = None,  # noqa: ANN401 - test helper
    ) -> ToolExecutionContext:
        mock_db_context = MagicMock(spec=Database)
        return ToolExecutionContext(
            conversation_id="policy-conv",
            user_name="test-user",
            interface_type="test",
            timezone=ZoneInfo("UTC"),
            turn_id="turn-1",
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            request_confirmation_callback=request_confirmation_callback,
            credential_resolvers=None,
            api_backend=None,
        )

    @pytest.mark.asyncio
    async def test_get_tool_definitions_hides_confirm_only_tools_without_confirmation(
        self,
    ) -> None:
        class StubDescriptorProvider:
            def __init__(self, descriptors: list[ToolDescriptor]) -> None:
                self._descriptors = descriptors

            async def get_tool_definitions(self) -> list[ToolDefinition]:
                return [descriptor.definition for descriptor in self._descriptors]

            async def get_tool_descriptors(self) -> list[ToolDescriptor]:
                return list(self._descriptors)

            async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
                for descriptor in self._descriptors:
                    if descriptor.name == name:
                        return descriptor
                return None

            async def execute_tool(
                self,
                name: str,
                arguments: dict[str, object],
                context: ToolExecutionContext,
                call_id: str | None = None,
            ) -> str:
                return f"executed:{name}"

            async def close(self) -> None:
                return None

        descriptors = [
            self._make_descriptor(
                "get_note",
                tags={ToolTag.NOTES, ToolTag.READ_ONLY, ToolTag.SENSITIVE_DATA},
            ),
            self._make_descriptor(
                "delete_note",
                tags={ToolTag.NOTES, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
            ),
        ]
        policy_engine = PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.NOTES]),
                        decision=ToolPolicyDecision.ALLOW,
                        priority=10,
                    ),
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                        decision=ToolPolicyDecision.CONFIRM,
                        priority=20,
                    ),
                ],
            )
        )
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=StubDescriptorProvider(descriptors),
            policy_engine=policy_engine,
        )

        definitions_without_confirm = await provider.get_tool_definitions(
            can_confirm=False
        )
        definitions_with_confirm = await provider.get_tool_definitions(can_confirm=True)

        assert [d["function"]["name"] for d in definitions_without_confirm] == [
            "get_note"
        ]
        assert [d["function"]["name"] for d in definitions_with_confirm] == [
            "get_note",
            "delete_note",
        ]

    @pytest.mark.asyncio
    async def test_get_tool_descriptors_filters_denied_tools(self) -> None:
        class StubDescriptorProvider:
            def __init__(self, descriptors: list[ToolDescriptor]) -> None:
                self._descriptors = descriptors

            async def get_tool_definitions(self) -> list[ToolDefinition]:
                return [descriptor.definition for descriptor in self._descriptors]

            async def get_tool_descriptors(self) -> list[ToolDescriptor]:
                return list(self._descriptors)

            async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
                for descriptor in self._descriptors:
                    if descriptor.name == name:
                        return descriptor
                return None

            async def execute_tool(
                self,
                name: str,
                arguments: dict[str, object],
                context: ToolExecutionContext,
                call_id: str | None = None,
            ) -> str:
                return f"executed:{name}"

            async def close(self) -> None:
                return None

        descriptors = [
            self._make_descriptor(
                "get_note",
                tags={ToolTag.NOTES, ToolTag.READ_ONLY, ToolTag.SENSITIVE_DATA},
            ),
            self._make_descriptor(
                "delete_note",
                tags={ToolTag.NOTES, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
            ),
        ]
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=StubDescriptorProvider(descriptors),
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(
                    default_decision=ToolPolicyDecision.DENY,
                    rules=[
                        PolicyRule(
                            match=ToolMatcher(names=["get_note"]),
                            decision=ToolPolicyDecision.ALLOW,
                            priority=10,
                        )
                    ],
                )
            ),
        )

        filtered_descriptors = await provider.get_tool_descriptors()

        assert [descriptor.name for descriptor in filtered_descriptors] == ["get_note"]

    @pytest.mark.asyncio
    async def test_get_tool_descriptor_returns_none_for_denied_tool(self) -> None:
        class StubDescriptorProvider:
            def __init__(self, descriptors: list[ToolDescriptor]) -> None:
                self._descriptors = descriptors

            async def get_tool_definitions(self) -> list[ToolDefinition]:
                return [descriptor.definition for descriptor in self._descriptors]

            async def get_tool_descriptors(self) -> list[ToolDescriptor]:
                return list(self._descriptors)

            async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
                for descriptor in self._descriptors:
                    if descriptor.name == name:
                        return descriptor
                return None

            async def execute_tool(
                self,
                name: str,
                arguments: dict[str, object],
                context: ToolExecutionContext,
                call_id: str | None = None,
            ) -> str:
                return f"executed:{name}"

            async def close(self) -> None:
                return None

        descriptors = [
            self._make_descriptor(
                "get_note",
                tags={ToolTag.NOTES, ToolTag.READ_ONLY, ToolTag.SENSITIVE_DATA},
            ),
            self._make_descriptor(
                "delete_note",
                tags={ToolTag.NOTES, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
            ),
        ]
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=StubDescriptorProvider(descriptors),
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(
                    default_decision=ToolPolicyDecision.DENY,
                    rules=[
                        PolicyRule(
                            match=ToolMatcher(names=["get_note"]),
                            decision=ToolPolicyDecision.ALLOW,
                            priority=10,
                        )
                    ],
                )
            ),
        )

        allowed_descriptor = await provider.get_tool_descriptor("get_note")
        denied_descriptor = await provider.get_tool_descriptor("delete_note")

        assert allowed_descriptor is not None
        assert allowed_descriptor.name == "get_note"
        assert denied_descriptor is None

    @pytest.mark.asyncio
    async def test_execute_tool_requests_confirmation_when_policy_requires_it(
        self,
    ) -> None:
        class StubDescriptorProvider:
            def __init__(self, descriptor: ToolDescriptor) -> None:
                self._descriptor = descriptor
                self.calls: list[tuple[str, dict[str, object], str | None]] = []

            async def get_tool_definitions(self) -> list[ToolDefinition]:
                return [self._descriptor.definition]

            async def get_tool_descriptors(self) -> list[ToolDescriptor]:
                return [self._descriptor]

            async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
                if name == self._descriptor.name:
                    return self._descriptor
                return None

            async def execute_tool(
                self,
                name: str,
                arguments: dict[str, object],
                context: ToolExecutionContext,
                call_id: str | None = None,
            ) -> str:
                self.calls.append((name, arguments, call_id))
                return "executed"

            async def close(self) -> None:
                return None

        descriptor = self._make_descriptor(
            "delete_note",
            tags={ToolTag.NOTES, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
        )
        wrapped_provider = StubDescriptorProvider(descriptor)
        policy_engine = PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                        decision=ToolPolicyDecision.CONFIRM,
                        priority=20,
                    )
                ],
            )
        )
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=wrapped_provider,
            policy_engine=policy_engine,
            confirmation_timeout=42.0,
        )

        captured: dict[str, object] = {}

        async def confirmation_callback(
            interface_type: str,
            conversation_id: str,
            turn_id: str | None,
            tool_name: str,
            call_id: str,
            tool_args: dict[str, object],
            timeout_seconds: float,
            context: ToolExecutionContext,
        ) -> ConfirmationOutcome:
            captured["interface_type"] = interface_type
            captured["conversation_id"] = conversation_id
            captured["turn_id"] = turn_id
            captured["tool_name"] = tool_name
            captured["call_id"] = call_id
            captured["tool_args"] = tool_args
            captured["timeout_seconds"] = timeout_seconds
            captured["context"] = context
            return ConfirmationOutcome(kind="approved")

        exec_context = self._make_context(
            request_confirmation_callback=confirmation_callback
        )
        result = await provider.execute_tool(
            "delete_note",
            {"title": "hello"},
            exec_context,
            call_id="call-explicit-123",
        )

        assert result == "executed"
        assert captured["tool_name"] == "delete_note"
        assert captured["call_id"] == "call-explicit-123"
        assert captured["timeout_seconds"] == 42.0
        assert captured["context"] is exec_context
        assert wrapped_provider.calls == [
            ("delete_note", {"title": "hello"}, "call-explicit-123")
        ]

    @pytest.mark.asyncio
    async def test_execute_tool_denies_confirm_only_tool_without_confirmation_path(
        self,
    ) -> None:
        class StubDescriptorProvider:
            def __init__(self, descriptor: ToolDescriptor) -> None:
                self._descriptor = descriptor

            async def get_tool_definitions(self) -> list[ToolDefinition]:
                return [self._descriptor.definition]

            async def get_tool_descriptors(self) -> list[ToolDescriptor]:
                return [self._descriptor]

            async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
                if name == self._descriptor.name:
                    return self._descriptor
                return None

            async def execute_tool(
                self,
                name: str,
                arguments: dict[str, object],
                context: ToolExecutionContext,
                call_id: str | None = None,
            ) -> str:
                return "executed"

            async def close(self) -> None:
                return None

        descriptor = self._make_descriptor(
            "delete_note",
            tags={ToolTag.NOTES, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
        )
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=StubDescriptorProvider(descriptor),
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(
                    default_decision=ToolPolicyDecision.DENY,
                    rules=[
                        PolicyRule(
                            match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                            decision=ToolPolicyDecision.CONFIRM,
                            priority=20,
                        )
                    ],
                )
            ),
        )

        with pytest.raises(ToolPolicyDeniedError):
            await provider.execute_tool(
                "delete_note",
                {"title": "hello"},
                self._make_context(),
            )


class _VersionedDescriptorProvider:
    """Descriptor provider whose descriptor set (and version) can change.

    Models an ``MCPToolsProvider`` whose server was down at startup and later
    reconnects: ``add_descriptor`` mutates the descriptor set and bumps
    ``descriptors_version`` exactly as the real provider does.
    """

    def __init__(self, descriptors: list[ToolDescriptor]) -> None:
        self._descriptors = list(descriptors)
        self._descriptors_version = 0

    @property
    def descriptors_version(self) -> int:
        return self._descriptors_version

    def add_descriptor(self, descriptor: ToolDescriptor) -> None:
        self._descriptors.append(descriptor)
        self._descriptors_version += 1

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return [descriptor.definition for descriptor in self._descriptors]

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        return list(self._descriptors)

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        for descriptor in self._descriptors:
            if descriptor.name == name:
                return descriptor
        return None

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, object],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        del arguments, context, call_id
        return f"executed:{name}"

    async def close(self) -> None:
        return None


def _make_mcp_descriptor(name: str, server_id: str) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        definition={
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} description",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        tags=frozenset(),
        origin="mcp",
        mcp_server_id=server_id,
    )


class TestResolveDescriptorsVersion:
    """The version resolver aggregates through wrappers and composites."""

    def test_static_provider_is_version_zero(self) -> None:
        provider = LocalToolsProvider(registrations=[])
        assert resolve_descriptors_version(provider) == 0

    def test_reads_descriptors_version_attribute(self) -> None:
        provider = _VersionedDescriptorProvider([])
        provider.add_descriptor(_make_mcp_descriptor("t", "srv"))
        assert resolve_descriptors_version(provider) == 1

    def test_composite_sums_child_versions(self) -> None:
        versioned = _VersionedDescriptorProvider([])
        versioned.add_descriptor(_make_mcp_descriptor("a", "srv"))
        versioned.add_descriptor(_make_mcp_descriptor("b", "srv"))
        composite = CompositeToolsProvider(
            providers=[LocalToolsProvider(registrations=[]), versioned]
        )
        # Static child contributes 0; versioned child contributes 2.
        assert resolve_descriptors_version(composite) == 2

    def test_wrapper_forwards_inner_version(self) -> None:
        versioned = _VersionedDescriptorProvider([])
        versioned.add_descriptor(_make_mcp_descriptor("a", "srv"))
        wrapper = PolicyEnforcingToolsProvider(
            wrapped_provider=versioned,
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
            ),
        )
        assert resolve_descriptors_version(wrapper) == 1


class TestPolicyEnforcingCacheInvalidation:
    """The advertised-definitions cache rebuilds when descriptors change."""

    @staticmethod
    def _allow_all_engine() -> PolicyEngine:
        return PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        )

    @pytest.mark.asyncio
    async def test_reconnected_server_tools_become_advertisable(self) -> None:
        """A server down at startup becomes advertisable once it reconnects.

        Regression test for the indefinite ``get_tool_definitions`` cache: the
        first call (server down) cached an advertised set without the MCP
        tools, and nothing invalidated it when the tools later appeared.
        """
        wrapped = _VersionedDescriptorProvider([])
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=wrapped,
            policy_engine=self._allow_all_engine(),
        )

        # Server down at startup: nothing advertised, cache warmed empty.
        assert await provider.get_tool_definitions() == []

        # Health-check loop reconnects the server and its tool appears.
        wrapped.add_descriptor(_make_mcp_descriptor("execute_python", "code-execution"))

        names = [d["function"]["name"] for d in await provider.get_tool_definitions()]
        assert names == ["execute_python"]

    @pytest.mark.asyncio
    async def test_cache_served_while_descriptors_unchanged(self) -> None:
        """Without a descriptor change the cached result is reused as-is."""
        wrapped = _VersionedDescriptorProvider([
            _make_mcp_descriptor("execute_python", "code-execution")
        ])
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=wrapped,
            policy_engine=self._allow_all_engine(),
        )

        first = await provider.get_tool_definitions()
        second = await provider.get_tool_definitions()

        # Same cached list object is returned when nothing changed.
        assert first is second

    @pytest.mark.asyncio
    async def test_colliding_names_are_deduped_in_advertised_list(self) -> None:
        """A reconnected tool colliding with a local name is not advertised twice.

        A server down at startup can reconnect exposing a tool whose name
        matches a local/root tool. The rebuilt advertised list must not contain
        duplicate function declarations (some LLM APIs reject them).
        """

        collide_def: ToolDefinition = {
            "type": "function",
            "function": {
                "name": "execute_python",
                "description": "execute_python description",
                "parameters": {"type": "object", "properties": {}},
            },
        }

        class _CollidingProvider:
            async def get_tool_definitions(self) -> list[ToolDefinition]:
                # Same name from two providers (e.g. local + reconnected MCP).
                return [collide_def, collide_def]

            async def get_tool_descriptors(self) -> list[ToolDescriptor]:
                return [_make_mcp_descriptor("execute_python", "code-execution")]

            async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
                if name == "execute_python":
                    return _make_mcp_descriptor("execute_python", "code-execution")
                return None

            async def execute_tool(
                self,
                name: str,
                arguments: dict[str, object],
                context: ToolExecutionContext,
                call_id: str | None = None,
            ) -> str:
                del arguments, context, call_id
                return f"executed:{name}"

            async def close(self) -> None:
                return None

        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=_CollidingProvider(),
            policy_engine=self._allow_all_engine(),
        )

        names = [d["function"]["name"] for d in await provider.get_tool_definitions()]
        assert names == ["execute_python"]


def _make_local_descriptor(name: str) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        definition={
            "type": "function",
            "function": {
                "name": name,
                "description": f"local {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        tags=frozenset(),
        origin="local",
    )


class TestAdvertisementFollowsExecutionResolution:
    """Advertisement evaluates policy on the descriptor execution resolves to.

    Execution resolves a tool name to the *first* descriptor with that name,
    so when two descriptors share a name and policy decides them differently,
    the advertisement decision must come from the first descriptor — not from
    "any descriptor with that name passes policy".
    """

    @pytest.mark.asyncio
    async def test_name_not_advertised_when_first_descriptor_denied(self) -> None:
        """An allowed tool shadowed by a denied one is not advertised.

        The local descriptor comes first and is denied; the MCP descriptor
        with the same name is allowed but unreachable (execution resolves the
        name to the local descriptor). Advertising the name would offer the
        LLM a tool that is always denied on execution.
        """
        wrapped = _VersionedDescriptorProvider([
            _make_local_descriptor("foo"),
            _make_mcp_descriptor("foo", "srv"),
        ])
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=wrapped,
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(
                    default_decision=ToolPolicyDecision.DENY,
                    rules=[
                        PolicyRule(
                            match=ToolMatcher(mcp_server_ids=["srv"]),
                            decision=ToolPolicyDecision.ALLOW,
                            priority=10,
                        )
                    ],
                )
            ),
        )

        assert await provider.get_tool_definitions() == []

    @pytest.mark.asyncio
    async def test_name_advertised_when_first_descriptor_allowed(self) -> None:
        """A denied later descriptor does not suppress the allowed first one.

        The local descriptor comes first and is allowed; a same-named MCP
        descriptor is denied. The name is advertised once, with the first
        (executable) definition.
        """
        wrapped = _VersionedDescriptorProvider([
            _make_local_descriptor("foo"),
            _make_mcp_descriptor("foo", "srv"),
        ])
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=wrapped,
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(
                    default_decision=ToolPolicyDecision.ALLOW,
                    rules=[
                        PolicyRule(
                            match=ToolMatcher(mcp_server_ids=["srv"]),
                            decision=ToolPolicyDecision.DENY,
                            priority=10,
                        )
                    ],
                )
            ),
        )

        definitions = await provider.get_tool_definitions()
        assert [d["function"]["name"] for d in definitions] == ["foo"]
        assert definitions[0]["function"]["description"] == "local foo"

    @pytest.mark.asyncio
    async def test_collision_logs_error_naming_both_origins(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A shadowed descriptor is reported loudly, not dropped silently.

        Collisions are almost always misconfiguration or a misbehaving MCP
        server; the ERROR record reaches the persistent error log surfaced by
        the diagnostics endpoints.
        """
        wrapped = _VersionedDescriptorProvider([
            _make_local_descriptor("foo"),
            _make_mcp_descriptor("foo", "srv"),
        ])
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=wrapped,
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
            ),
        )

        with caplog.at_level("ERROR", logger="family_assistant.tools.infrastructure"):
            await provider.get_tool_definitions()

        collision_records = [
            record
            for record in caplog.records
            if "Tool name collision" in record.getMessage()
        ]
        assert len(collision_records) == 1
        message = collision_records[0].getMessage()
        assert "'foo'" in message
        assert "MCP (server 'srv')" in message
        assert "local" in message

    @pytest.mark.asyncio
    async def test_no_collision_logs_no_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unique tool names never produce a collision error."""
        wrapped = _VersionedDescriptorProvider([
            _make_local_descriptor("foo"),
            _make_mcp_descriptor("bar", "srv"),
        ])
        provider = PolicyEnforcingToolsProvider(
            wrapped_provider=wrapped,
            policy_engine=PolicyEngine.from_policy_config(
                ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
            ),
        )

        with caplog.at_level("ERROR", logger="family_assistant.tools.infrastructure"):
            await provider.get_tool_definitions()

        assert not [
            record
            for record in caplog.records
            if "Tool name collision" in record.getMessage()
        ]
