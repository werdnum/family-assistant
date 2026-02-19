"""Tests for engineering diagnostic tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from family_assistant.tools import AVAILABLE_FUNCTIONS, TOOLS_DEFINITION
from family_assistant.tools.engineering import (
    ENGINEERING_TOOLS_DEFINITION,
    _is_select_only,  # noqa: PLC2701  # Testing private validation logic
    _validate_source_path,  # noqa: PLC2701  # Testing private path validation
    create_github_issue,
    query_database,
    read_error_logs,
    read_source_file,
    search_source_code,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.fixture
def exec_context() -> ToolExecutionContext:
    """Create a minimal ToolExecutionContext for testing."""
    mock_db_context = Mock()
    mock_db_context.engine = Mock()
    mock_db_context.engine.dialect = Mock()
    mock_db_context.engine.dialect.name = "sqlite"
    mock_db_context.error_logs = Mock()
    mock_db_context.error_logs.get_all = AsyncMock(return_value=[])
    mock_db_context.fetch_all = AsyncMock(return_value=[])
    mock_db_context.execute_with_retry = AsyncMock()

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


# --- query_database tests ---


class TestQueryDatabase:
    @pytest.mark.anyio
    async def test_select_query_executes(
        self, exec_context: ToolExecutionContext
    ) -> None:
        mock_db = Mock()
        mock_db.engine.dialect.name = "sqlite"
        mock_db.fetch_all = AsyncMock(return_value=[{"id": 1, "name": "test"}])
        mock_db.execute_with_retry = AsyncMock()
        exec_context.db_context = mock_db

        result = await query_database(exec_context, "SELECT * FROM users")
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["row_count"] == 1
        assert data["truncated"] is False
        assert data["rows"] == [{"id": 1, "name": "test"}]

    @pytest.mark.anyio
    async def test_non_select_rejected(
        self, exec_context: ToolExecutionContext
    ) -> None:
        result = await query_database(exec_context, "DELETE FROM users WHERE id = 1")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "Only SELECT queries are allowed" in data["error"]

    @pytest.mark.anyio
    async def test_insert_rejected(self, exec_context: ToolExecutionContext) -> None:
        result = await query_database(
            exec_context, "INSERT INTO users (name) VALUES ('test')"
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data

    @pytest.mark.anyio
    async def test_postgresql_sets_read_only(
        self, exec_context: ToolExecutionContext
    ) -> None:
        mock_execute = AsyncMock()
        mock_db = Mock()
        mock_db.engine.dialect.name = "postgresql"
        mock_db.fetch_all = AsyncMock(return_value=[])
        mock_db.execute_with_retry = mock_execute
        exec_context.db_context = mock_db

        await query_database(exec_context, "SELECT 1")

        mock_execute.assert_called_once()
        text_clause = mock_execute.call_args[0][0]
        assert text_clause.text == "SET TRANSACTION READ ONLY"

    @pytest.mark.anyio
    async def test_sqlite_skips_read_only(
        self, exec_context: ToolExecutionContext
    ) -> None:
        mock_execute = AsyncMock()
        mock_db = Mock()
        mock_db.engine.dialect.name = "sqlite"
        mock_db.fetch_all = AsyncMock(return_value=[])
        mock_db.execute_with_retry = mock_execute
        exec_context.db_context = mock_db

        await query_database(exec_context, "SELECT 1")

        mock_execute.assert_not_called()

    @pytest.mark.anyio
    async def test_row_truncation(self, exec_context: ToolExecutionContext) -> None:
        many_rows = [{"id": i} for i in range(1500)]
        mock_db = Mock()
        mock_db.engine.dialect.name = "sqlite"
        mock_db.fetch_all = AsyncMock(return_value=many_rows)
        mock_db.execute_with_retry = AsyncMock()
        exec_context.db_context = mock_db

        result = await query_database(exec_context, "SELECT * FROM big_table")
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["truncated"] is True
        assert data["row_count"] == 1000
        assert data["max_rows"] == 1000

    @pytest.mark.anyio
    async def test_query_error_handled(
        self, exec_context: ToolExecutionContext
    ) -> None:
        mock_db = Mock()
        mock_db.engine.dialect.name = "sqlite"
        mock_db.fetch_all = AsyncMock(side_effect=Exception("table does not exist"))
        mock_db.execute_with_retry = AsyncMock()
        exec_context.db_context = mock_db

        result = await query_database(exec_context, "SELECT * FROM nonexistent")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "table does not exist" in data["error"]


# --- read_error_logs tests ---


class TestReadErrorLogs:
    @pytest.mark.anyio
    async def test_read_logs_default(self, exec_context: ToolExecutionContext) -> None:
        mock_error_logs = Mock()
        mock_error_logs.get_all = AsyncMock(
            return_value=[
                {"id": 1, "level": "ERROR", "message": "test error"},
            ]
        )
        exec_context.db_context = Mock(error_logs=mock_error_logs)

        result = await read_error_logs(exec_context)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] == 1
        assert data["logs"][0]["level"] == "ERROR"

    @pytest.mark.anyio
    async def test_read_logs_with_level_filter(
        self, exec_context: ToolExecutionContext
    ) -> None:
        mock_get_all = AsyncMock(return_value=[])
        exec_context.db_context = Mock(error_logs=Mock(get_all=mock_get_all))

        await read_error_logs(exec_context, level="WARNING")
        mock_get_all.assert_called_once_with(
            level="WARNING", logger_name=None, limit=50
        )

    @pytest.mark.anyio
    async def test_limit_capped_at_200(
        self, exec_context: ToolExecutionContext
    ) -> None:
        mock_get_all = AsyncMock(return_value=[])
        exec_context.db_context = Mock(error_logs=Mock(get_all=mock_get_all))

        await read_error_logs(exec_context, limit=500)
        mock_get_all.assert_called_once_with(level=None, logger_name=None, limit=200)


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


# --- Tool definitions tests ---


class TestToolDefinitions:
    def test_definitions_list_is_complete(self) -> None:
        tool_names = {t["function"]["name"] for t in ENGINEERING_TOOLS_DEFINITION}
        expected = {
            "read_source_file",
            "search_source_code",
            "query_database",
            "read_error_logs",
            "create_github_issue",
        }
        assert tool_names == expected

    def test_tools_registered_in_available_functions(self) -> None:
        expected_tools = [
            "read_source_file",
            "search_source_code",
            "query_database",
            "read_error_logs",
            "create_github_issue",
        ]
        for tool_name in expected_tools:
            assert tool_name in AVAILABLE_FUNCTIONS, (
                f"{tool_name} not in AVAILABLE_FUNCTIONS"
            )

    def test_tools_in_tools_definition(self) -> None:
        all_tool_names = {t["function"]["name"] for t in TOOLS_DEFINITION}
        expected_tools = [
            "read_source_file",
            "search_source_code",
            "query_database",
            "read_error_logs",
            "create_github_issue",
        ]
        for tool_name in expected_tools:
            assert tool_name in all_tool_names, f"{tool_name} not in TOOLS_DEFINITION"
