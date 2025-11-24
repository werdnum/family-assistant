import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from collections.abc import Generator
import pytest
from family_assistant.tools.database import database_readonly_query
from family_assistant.tools.github import create_github_issue
from family_assistant.tools.source_reader import (
    list_source_files,
    read_file_chunk,
    search_in_file,
)


@pytest.fixture
def mock_db_context() -> Generator[MagicMock, None, None]:
    with patch("family_assistant.tools.database.DatabaseContext") as mock:
        mock.return_value.__aenter__.return_value.fetch_all = AsyncMock()
        yield mock


def test_list_source_files_success() -> None:
    # Create a dummy file and directory to list
    os.makedirs("dummy_dir", exist_ok=True)
    with open("dummy_dir/dummy_file.txt", "w", encoding="utf-8") as f:
        f.write("test content")

    result = list_source_files("dummy_dir")
    assert "dummy_file.txt" in result

    os.remove("dummy_dir/dummy_file.txt")
    os.rmdir("dummy_dir")


def test_list_source_files_not_found() -> None:
    result = list_source_files("non_existent_dir")
    assert "Error: Path not found" in result


def test_list_source_files_outside_project_directory() -> None:
    result = list_source_files("../../..")
    assert "Error: Access denied" in result


def test_read_file_chunk_success() -> None:
    # Create a dummy file to read
    dummy_filepath = "dummy_test_file.txt"
    with open(dummy_filepath, "w", encoding="utf-8") as f:
        f.write("line 1\nline 2\nline 3\n")

    result = read_file_chunk(dummy_filepath, 2, 3)
    assert result == "line 2\nline 3\n"

    os.remove(dummy_filepath)


def test_read_file_chunk_file_not_found() -> None:
    result = read_file_chunk("non_existent_file.txt", 1, 1)
    assert "Error: File not found" in result


def test_read_file_chunk_outside_project_directory() -> None:
    result = read_file_chunk("../../../etc/passwd", 1, 1)
    assert "Error: Access denied" in result


def test_search_in_file_success() -> None:
    # Create a dummy file to search
    dummy_filepath = "dummy_test_file.txt"
    with open(dummy_filepath, "w", encoding="utf-8") as f:
        f.write("line 1\nline 2 with search string\nline 3\n")

    result = search_in_file(dummy_filepath, "search string")
    assert "2: line 2 with search string" in result

    os.remove(dummy_filepath)


def test_search_in_file_not_found() -> None:
    result = search_in_file("non_existent_file.txt", "search string")
    assert "Error: File not found" in result


def test_search_in_file_outside_project_directory() -> None:
    result = search_in_file("../../../etc/passwd", "root")
    assert "Error: Access denied" in result


@patch.dict(
    os.environ,
    {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "test/repo"},
)
@patch("family_assistant.tools.github.requests.post")
def test_create_github_issue_success(mock_post: MagicMock) -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"html_url": "http://test.com/issue/1"}
    mock_post.return_value = mock_response

    result = create_github_issue("Test Title", "Test Body")
    assert "Successfully created issue" in result
    assert "http://test.com/issue/1" in result


@patch.dict(os.environ, {}, clear=True)
def test_create_github_issue_missing_env_vars() -> None:
    result = create_github_issue("Test Title", "Test Body")
    assert "Error: GITHUB_TOKEN and GITHUB_REPOSITORY environment variables must be set." in result


@pytest.mark.asyncio
async def test_database_readonly_query_success(mock_db_context: MagicMock) -> None:
    mock_db_context.return_value.__aenter__.return_value.fetch_all.return_value = [
        {"id": 1, "name": "test"}
    ]
    query = "SELECT * FROM test"
    result = await database_readonly_query(query)
    assert result == '[\n  {\n    "id": 1,\n    "name": "test"\n  }\n]'


@pytest.mark.asyncio
async def test_database_readonly_query_disallowed_keyword() -> None:
    result = await database_readonly_query("DELETE FROM test")
    assert "Error: Only SELECT queries are allowed." in result


@pytest.mark.asyncio
async def test_database_readonly_query_disallowed_keyword_in_select() -> None:
    result = await database_readonly_query("SELECT * FROM test; DELETE FROM test")
    assert "Error: Query contains disallowed keywords." in result
