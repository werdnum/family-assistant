"""Unit tests for engineer profile tools."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from family_assistant.tools.database import database_readonly_query, is_select_only
from family_assistant.tools.github import create_github_issue
from family_assistant.tools.source_reader import (
    list_source_files,
    read_file_chunk,
    search_in_file,
)

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def mock_db_context() -> Generator[MagicMock]:
    """Mock DatabaseContext for testing database queries."""
    with patch("family_assistant.tools.database.DatabaseContext") as mock:
        mock_ctx = MagicMock()
        mock_ctx.fetch_all = AsyncMock()
        # Mock the connection and dialect for PostgreSQL check
        mock_conn = MagicMock()
        mock_conn.dialect.name = "sqlite"
        mock_conn.execute = AsyncMock()
        mock_ctx.conn = mock_conn
        mock.return_value.__aenter__.return_value = mock_ctx
        yield mock


# --- Source Reader Tests ---


def test_list_source_files_success() -> None:
    """Test listing files in a directory."""
    os.makedirs("dummy_dir", exist_ok=True)
    with open("dummy_dir/dummy_file.txt", "w", encoding="utf-8") as f:
        f.write("test content")

    result = list_source_files("dummy_dir")
    assert "dummy_file.txt" in result

    os.remove("dummy_dir/dummy_file.txt")
    os.rmdir("dummy_dir")


def test_list_source_files_not_found() -> None:
    """Test error when directory doesn't exist."""
    result = list_source_files("non_existent_dir")
    assert "Error: Path not found" in result


def test_list_source_files_outside_project_directory() -> None:
    """Test security check for paths outside project."""
    result = list_source_files("../../..")
    assert "Error: Access denied" in result


def test_read_file_chunk_success() -> None:
    """Test reading a range of lines from a file."""
    dummy_filepath = "dummy_test_read_chunk.txt"
    with open(dummy_filepath, "w", encoding="utf-8") as f:
        f.write("line 1\nline 2\nline 3\n")

    result = read_file_chunk(dummy_filepath, 2, 3)
    assert result == "line 2\nline 3\n"

    os.remove(dummy_filepath)


def test_read_file_chunk_file_not_found() -> None:
    """Test error when file doesn't exist."""
    result = read_file_chunk("non_existent_file.txt", 1, 1)
    assert "Error: File not found" in result


def test_read_file_chunk_outside_project_directory() -> None:
    """Test security check for files outside project."""
    result = read_file_chunk("../../../etc/passwd", 1, 1)
    assert "Error: Access denied" in result


def test_read_file_chunk_invalid_line_numbers() -> None:
    """Test validation of line numbers."""
    dummy_filepath = "dummy_test_read_invalid.txt"
    with open(dummy_filepath, "w", encoding="utf-8") as f:
        f.write("line 1\nline 2\nline 3\n")

    # Test start_line < 1
    result = read_file_chunk(dummy_filepath, 0, 1)
    assert "Error: start_line must be at least 1" in result

    # Test end_line < start_line
    result = read_file_chunk(dummy_filepath, 3, 1)
    assert "Error: end_line must be >= start_line" in result

    os.remove(dummy_filepath)


def test_search_in_file_success() -> None:
    """Test searching for a string in a file."""
    dummy_filepath = "dummy_test_search_success.txt"
    with open(dummy_filepath, "w", encoding="utf-8") as f:
        f.write("line 1\nline 2 with search string\nline 3\n")

    result = search_in_file(dummy_filepath, "search string")
    assert "2: line 2 with search string" in result

    os.remove(dummy_filepath)


def test_search_in_file_no_matches() -> None:
    """Test searching for a string that doesn't exist."""
    dummy_filepath = "dummy_test_search_no_match.txt"
    with open(dummy_filepath, "w", encoding="utf-8") as f:
        f.write("line 1\nline 2\nline 3\n")

    result = search_in_file(dummy_filepath, "not found")
    assert result == "No matches found."

    os.remove(dummy_filepath)


def test_search_in_file_not_found() -> None:
    """Test error when file doesn't exist."""
    result = search_in_file("non_existent_file.txt", "search string")
    assert "Error: File not found" in result


def test_search_in_file_outside_project_directory() -> None:
    """Test security check for files outside project."""
    result = search_in_file("../../../etc/passwd", "root")
    assert "Error: Access denied" in result


# --- GitHub Issue Tests ---


@pytest.mark.asyncio
@patch.dict(
    os.environ,
    {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "test/repo"},
)
async def test_create_github_issue_success() -> None:
    """Test successful issue creation."""
    with patch("family_assistant.tools.github.httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"html_url": "http://test.com/issue/1"}

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_client.return_value.__aenter__.return_value = mock_client_instance

        result = await create_github_issue("Test Title", "Test Body")
        assert "Successfully created issue" in result
        assert "http://test.com/issue/1" in result


@pytest.mark.asyncio
@patch.dict(os.environ, {}, clear=True)
async def test_create_github_issue_missing_env_vars() -> None:
    """Test error when environment variables are missing."""
    result = await create_github_issue("Test Title", "Test Body")
    assert (
        "Error: GITHUB_TOKEN and GITHUB_REPOSITORY environment variables must be set."
        in result
    )


# --- Database Query Tests ---


def test_is_select_only_valid_select() -> None:
    """Test that valid SELECT queries are accepted."""
    assert is_select_only("SELECT * FROM users")
    assert is_select_only("SELECT id, name FROM users WHERE id = 1")
    assert is_select_only("select * from users")  # Case insensitive
    assert is_select_only("  SELECT * FROM users  ")  # Whitespace


def test_is_select_only_invalid_queries() -> None:
    """Test that non-SELECT queries are rejected."""
    assert not is_select_only("DELETE FROM users")
    assert not is_select_only("INSERT INTO users VALUES (1)")
    assert not is_select_only("UPDATE users SET name = 'test'")
    assert not is_select_only("DROP TABLE users")
    assert not is_select_only("CREATE TABLE test (id INT)")
    assert not is_select_only("ALTER TABLE users ADD COLUMN email TEXT")


def test_is_select_only_multiple_statements() -> None:
    """Test that multiple statements are rejected if any is not SELECT."""
    assert not is_select_only("SELECT * FROM users; DELETE FROM users")
    assert not is_select_only("SELECT 1; UPDATE users SET name = 'x'")


def test_is_select_only_column_names_with_keywords() -> None:
    """Test that column names containing keywords are allowed."""
    # This is why we use sqlparse instead of keyword matching
    assert is_select_only("SELECT update_time FROM logs")
    assert is_select_only("SELECT created_at, deleted FROM events")


@pytest.mark.asyncio
async def test_database_readonly_query_success(mock_db_context: MagicMock) -> None:
    """Test successful query execution."""
    mock_db_context.return_value.__aenter__.return_value.fetch_all.return_value = [
        {"id": 1, "name": "test"}
    ]
    query = "SELECT * FROM test"
    result = await database_readonly_query(query)
    assert '"id": 1' in result
    assert '"name": "test"' in result


@pytest.mark.asyncio
async def test_database_readonly_query_reject_delete() -> None:
    """Test that DELETE queries are rejected."""
    result = await database_readonly_query("DELETE FROM test")
    assert "Only SELECT queries are allowed" in result


@pytest.mark.asyncio
async def test_database_readonly_query_reject_update() -> None:
    """Test that UPDATE queries are rejected."""
    result = await database_readonly_query("UPDATE test SET name = 'x'")
    assert "Only SELECT queries are allowed" in result


@pytest.mark.asyncio
async def test_database_readonly_query_reject_mixed_statements() -> None:
    """Test that mixed SELECT + modification queries are rejected."""
    result = await database_readonly_query("SELECT * FROM test; DELETE FROM test")
    assert "Only SELECT queries are allowed" in result


@pytest.mark.asyncio
async def test_database_readonly_query_postgres_sets_read_only(
    mock_db_context: MagicMock,
) -> None:
    """Test that PostgreSQL connections are set to read-only mode."""
    mock_ctx = mock_db_context.return_value.__aenter__.return_value
    mock_ctx.fetch_all.return_value = []
    mock_ctx.conn.dialect.name = "postgresql"

    await database_readonly_query("SELECT 1")

    # Verify SET TRANSACTION READ ONLY was executed
    mock_ctx.conn.execute.assert_called()
    call_args = mock_ctx.conn.execute.call_args[0][0]
    assert "SET TRANSACTION READ ONLY" in str(call_args)
