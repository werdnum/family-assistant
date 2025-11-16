import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from family_assistant.tools.database import database_readonly_query
from family_assistant.tools.github import create_github_issue
from family_assistant.tools.source_code import read_source_code


@pytest.fixture
def mock_db_context():
    with patch("family_assistant.tools.database.DatabaseContext") as mock:
        mock.return_value.__aenter__.return_value.fetch_all = AsyncMock()
        yield mock


def test_read_source_code_success():
    # Create a dummy file to read
    dummy_filepath = "dummy_test_file.txt"
    with open(dummy_filepath, "w") as f:
        f.write("test content")

    result = read_source_code(dummy_filepath)
    assert result == "test content"

    os.remove(dummy_filepath)


def test_read_source_code_file_not_found():
    result = read_source_code("non_existent_file.txt")
    assert "Error: File not found" in result


def test_read_source_code_outside_project_directory():
    result = read_source_code("../../../etc/passwd")
    assert "Error: Access denied" in result


@patch.dict(
    os.environ,
    {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "test/repo"},
)
@patch("family_assistant.tools.github.requests.post")
def test_create_github_issue_success(mock_post):
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"html_url": "http://test.com/issue/1"}
    mock_post.return_value = mock_response

    result = create_github_issue("Test Title", "Test Body")
    assert "Successfully created issue" in result
    assert "http://test.com/issue/1" in result


@patch.dict(os.environ, {}, clear=True)
def test_create_github_issue_missing_env_vars():
    result = create_github_issue("Test Title", "Test Body")
    assert "Error: GITHUB_TOKEN and GITHUB_REPOSITORY environment variables must be set." in result


@pytest.mark.asyncio
async def test_database_readonly_query_success(mock_db_context):
    mock_db_context.return_value.__aenter__.return_value.fetch_all.return_value = [
        {"id": 1, "name": "test"}
    ]
    query = "SELECT * FROM test"
    result = await database_readonly_query(query)
    assert result == '[\n  {\n    "id": 1,\n    "name": "test"\n  }\n]'


@pytest.mark.asyncio
async def test_database_readonly_query_disallowed_keyword():
    result = await database_readonly_query("DELETE FROM test")
    assert "Error: Only SELECT queries are allowed." in result


@pytest.mark.asyncio
async def test_database_readonly_query_disallowed_keyword_in_select():
    result = await database_readonly_query("SELECT * FROM test; DELETE FROM test")
    assert "Error: Query contains disallowed keywords." in result
