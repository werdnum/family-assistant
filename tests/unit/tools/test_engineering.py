"""Tests for engineering diagnostic tools.

Pure logic tests and tests that must mock external services (GitHub API, ripgrep).
Database-dependent tests are in tests/functional/tools/test_engineering_database.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from family_assistant.llm.request_buffer import (
    LLMRequestRecord,
    get_request_buffer,
    reset_request_buffer,
)
from family_assistant.tools import AVAILABLE_FUNCTIONS, TOOLS_DEFINITION
from family_assistant.tools.engineering import (
    ENGINEERING_TOOLS_DEFINITION,
    _is_select_only,  # noqa: PLC2701  # Testing private validation logic
    _validate_source_path,  # noqa: PLC2701  # Testing private path validation
    create_github_issue,
    get_llm_request_history,
    read_source_file,
    search_source_code,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.fixture
def exec_context() -> ToolExecutionContext:
    """Create a minimal ToolExecutionContext for testing.

    For tests that don't touch the database, the mock db_context is sufficient.
    """
    mock_db_context = Mock()
    mock_db_context.engine = Mock()
    mock_db_context.engine.dialect = Mock()
    mock_db_context.engine.dialect.name = "sqlite"

    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=mock_db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
    )


# --- _validate_source_path tests ---


class TestValidateSourcePath:
    def test_valid_relative_path(self) -> None:
        result = _validate_source_path("src/family_assistant/tools/engineering.py")
        assert result.exists()
        assert result.name == "engineering.py"

    def test_path_traversal_denied(self) -> None:
        with pytest.raises(ValueError, match="Path traversal denied"):
            _validate_source_path("../../etc/passwd")

    def test_path_traversal_with_dots_in_middle(self) -> None:
        with pytest.raises(ValueError, match="Path traversal denied"):
            _validate_source_path("src/../../../etc/passwd")

    def test_nonexistent_path_still_validates(self) -> None:
        result = _validate_source_path("src/nonexistent_file.py")
        assert not result.exists()


# --- _is_select_only tests ---


class TestIsSelectOnly:
    def test_simple_select(self) -> None:
        assert _is_select_only("SELECT * FROM users") is True

    def test_select_with_where(self) -> None:
        assert _is_select_only("SELECT id, name FROM users WHERE id = 1") is True

    def test_select_with_join(self) -> None:
        assert (
            _is_select_only(
                "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
            )
            is True
        )

    def test_select_with_subquery(self) -> None:
        assert (
            _is_select_only(
                "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)"
            )
            is True
        )

    def test_select_with_trailing_semicolon(self) -> None:
        assert _is_select_only("SELECT * FROM users;") is True

    def test_insert_rejected(self) -> None:
        assert _is_select_only("INSERT INTO users (name) VALUES ('test')") is False

    def test_update_rejected(self) -> None:
        assert _is_select_only("UPDATE users SET name = 'test'") is False

    def test_delete_rejected(self) -> None:
        assert _is_select_only("DELETE FROM users") is False

    def test_drop_rejected(self) -> None:
        assert _is_select_only("DROP TABLE users") is False

    def test_multiple_selects_allowed(self) -> None:
        assert _is_select_only("SELECT 1; SELECT 2;") is True

    def test_select_then_delete_rejected(self) -> None:
        assert _is_select_only("SELECT 1; DELETE FROM users;") is False

    def test_empty_string(self) -> None:
        assert _is_select_only("") is False

    def test_create_table_rejected(self) -> None:
        assert _is_select_only("CREATE TABLE test (id INT)") is False


# --- read_source_file tests ---


class TestReadSourceFile:
    @pytest.mark.anyio
    async def test_read_existing_file(self, exec_context: ToolExecutionContext) -> None:
        result = await read_source_file(exec_context, "pyproject.toml")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "content" in data
        assert data["total_lines"] > 0
        assert "[project]" in data["content"]

    @pytest.mark.anyio
    async def test_read_with_line_range(
        self, exec_context: ToolExecutionContext
    ) -> None:
        result = await read_source_file(
            exec_context, "pyproject.toml", start_line=1, end_line=3
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["start_line"] == 1
        assert data["end_line"] == 3

    @pytest.mark.anyio
    async def test_read_nonexistent_file(
        self, exec_context: ToolExecutionContext
    ) -> None:
        result = await read_source_file(exec_context, "nonexistent_file.py")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "File not found" in data["error"]

    @pytest.mark.anyio
    async def test_path_traversal_blocked(
        self, exec_context: ToolExecutionContext
    ) -> None:
        result = await read_source_file(exec_context, "../../etc/passwd")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "Path traversal denied" in data["error"]

    @pytest.mark.anyio
    async def test_read_directory_returns_error(
        self, exec_context: ToolExecutionContext
    ) -> None:
        result = await read_source_file(exec_context, "src")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "Not a file" in data["error"]

    @pytest.mark.anyio
    async def test_os_error_handled(self, exec_context: ToolExecutionContext) -> None:
        """File deleted between exists() check and open() is handled gracefully."""
        with patch(
            "family_assistant.tools.engineering.aiofiles.open",
            side_effect=PermissionError("Permission denied"),
        ):
            result = await read_source_file(exec_context, "pyproject.toml")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "Failed to read file" in data["error"]


# --- search_source_code tests ---


class TestSearchSourceCode:
    @pytest.mark.anyio
    async def test_search_finds_pattern(
        self, exec_context: ToolExecutionContext
    ) -> None:
        result = await search_source_code(
            exec_context, "ENGINEERING_TOOLS_DEFINITION", "src/family_assistant/tools"
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["match_count"] > 0

    @pytest.mark.anyio
    async def test_search_no_matches(self, exec_context: ToolExecutionContext) -> None:
        # Scope to src/ so the test file itself isn't found
        result = await search_source_code(
            exec_context, "XYZZY_NONEXISTENT_PATTERN_12345", "src/"
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["match_count"] == 0

    @pytest.mark.anyio
    async def test_search_path_traversal_blocked(
        self, exec_context: ToolExecutionContext
    ) -> None:
        result = await search_source_code(exec_context, "test", "../../etc")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "Path traversal denied" in data["error"]

    @pytest.mark.anyio
    async def test_hyphen_pattern_not_treated_as_option(
        self, exec_context: ToolExecutionContext
    ) -> None:
        """Patterns starting with - should not be treated as rg options."""
        result = await search_source_code(exec_context, "-e", "src/")
        data = result.get_data()
        assert isinstance(data, dict)
        # Should succeed or find no matches, not return an error about bad flags
        assert "error" not in data


# --- create_github_issue tests ---


class TestCreateGithubIssue:
    @pytest.mark.anyio
    async def test_missing_token_returns_error(
        self, exec_context: ToolExecutionContext
    ) -> None:
        env = {k: v for k, v in __import__("os").environ.items() if k != "GITHUB_TOKEN"}
        with patch.dict("os.environ", env, clear=True):
            result = await create_github_issue(
                exec_context, "Bug report", "Description"
            )
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "GITHUB_TOKEN" in data["error"]

    @pytest.mark.anyio
    async def test_successful_issue_creation(
        self, exec_context: ToolExecutionContext
    ) -> None:
        mock_response = Mock()
        mock_response.json.return_value = {
            "number": 42,
            "html_url": "https://github.com/test/repo/issues/42",
        }
        mock_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}),
            patch(
                "family_assistant.tools.engineering.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await create_github_issue(exec_context, "Bug title", "Bug body")

        data = result.get_data()
        assert isinstance(data, dict)
        assert data["issue_number"] == 42
        assert "github.com" in data["url"]
        assert data["title"] == "Bug title"

    @pytest.mark.anyio
    async def test_github_api_error_handled(
        self, exec_context: ToolExecutionContext
    ) -> None:
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Forbidden", request=Mock(), response=mock_response
            )
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}),
            patch(
                "family_assistant.tools.engineering.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            result = await create_github_issue(exec_context, "Bug title", "Bug body")

        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "403" in data["error"]


# --- get_llm_request_history tests ---


class TestGetLlmRequestHistory:
    @pytest.fixture(autouse=True)
    def clean_buffer(self) -> None:
        reset_request_buffer()

    @pytest.mark.anyio
    async def test_returns_empty_when_no_records(self) -> None:
        result = await get_llm_request_history()

        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] == 0
        assert data["records"] == []

    @pytest.mark.anyio
    async def test_returns_records(self) -> None:
        buffer = get_request_buffer()
        buffer.add(
            LLMRequestRecord(
                timestamp=datetime.now(tz=UTC),
                request_id="req-1",
                model_id="gpt-4",
                messages=[],
                response={"text": "Hello"},
                duration_ms=150,
            )
        )

        result = await get_llm_request_history(limit=10, minutes=30)

        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] == 1
        assert data["records"][0]["request_id"] == "req-1"
        assert data["filters"]["limit"] == 10
        assert data["filters"]["minutes"] == 30

    @pytest.mark.anyio
    async def test_limit_clamped_to_max(self) -> None:
        result = await get_llm_request_history(limit=999)

        data = result.get_data()
        assert isinstance(data, dict)
        assert data["filters"]["limit"] == 100

    @pytest.mark.anyio
    async def test_limit_clamped_to_min(self) -> None:
        result = await get_llm_request_history(limit=0)

        data = result.get_data()
        assert isinstance(data, dict)
        assert data["filters"]["limit"] == 1


# --- Tool definitions tests ---


_ENGINEERING_TOOL_NAMES: frozenset[str] = frozenset({
    "read_source_file",
    "search_source_code",
    "query_database",
    "read_error_logs",
    "get_llm_request_history",
    "create_github_issue",
    "get_mcp_server_status",
    "reconnect_mcp_server",
    "get_resolved_config",
    "get_profile_config",
    "get_system_info",
})


class TestToolDefinitions:
    def test_definitions_list_is_complete(self) -> None:
        tool_names = {t["function"]["name"] for t in ENGINEERING_TOOLS_DEFINITION}
        assert tool_names == _ENGINEERING_TOOL_NAMES

    def test_tools_registered_in_available_functions(self) -> None:
        for tool_name in _ENGINEERING_TOOL_NAMES:
            assert tool_name in AVAILABLE_FUNCTIONS, (
                f"{tool_name} not in AVAILABLE_FUNCTIONS"
            )

    def test_tools_in_tools_definition(self) -> None:
        all_tool_names = {t["function"]["name"] for t in TOOLS_DEFINITION}
        for tool_name in _ENGINEERING_TOOL_NAMES:
            assert tool_name in all_tool_names, f"{tool_name} not in TOOLS_DEFINITION"
