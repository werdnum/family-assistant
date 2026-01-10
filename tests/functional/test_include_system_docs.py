"""Tests for include_system_docs feature in processing profiles."""

from pathlib import Path

import pytest

from family_assistant.config_loader import load_user_documentation


def test_load_user_documentation_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test loading documentation files successfully."""
    # Create temporary docs directory
    docs_dir = tmp_path / "docs" / "user"
    docs_dir.mkdir(parents=True)

    # Create test documentation files
    user_guide = docs_dir / "USER_GUIDE.md"
    user_guide.write_text("# User Guide\n\nThis is the user guide.")

    scripting = docs_dir / "scripting.md"
    scripting.write_text("# Scripting\n\nThis is the scripting guide.")

    # Set environment to use temp directory
    monkeypatch.setenv("DOCS_USER_DIR", str(docs_dir))

    # Load the documentation
    result = load_user_documentation(["USER_GUIDE.md", "scripting.md"])

    # Verify content
    assert "USER_GUIDE.md" in result
    assert "scripting.md" in result
    assert "This is the user guide." in result
    assert "This is the scripting guide." in result
    assert result.count("# Included Documentation:") == 2


def test_load_user_documentation_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test loading documentation when one file is missing."""
    # Create temporary docs directory
    docs_dir = tmp_path / "docs" / "user"
    docs_dir.mkdir(parents=True)

    # Create only one file
    user_guide = docs_dir / "USER_GUIDE.md"
    user_guide.write_text("# User Guide\n\nThis is the user guide.")

    # Set environment to use temp directory
    monkeypatch.setenv("DOCS_USER_DIR", str(docs_dir))

    # Try to load including missing file
    result = load_user_documentation(["USER_GUIDE.md", "missing.md"])

    # Should still load the available file
    assert "USER_GUIDE.md" in result
    assert "This is the user guide." in result
    # Missing file should not be in output
    assert "missing.md" not in result


def test_load_user_documentation_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test loading documentation with empty list."""
    # Create temporary docs directory
    docs_dir = tmp_path / "docs" / "user"
    docs_dir.mkdir(parents=True)

    # Set environment to use temp directory
    monkeypatch.setenv("DOCS_USER_DIR", str(docs_dir))

    # Load empty list
    result = load_user_documentation([])

    # Should return empty string
    assert not result


def test_load_user_documentation_security_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that directory traversal is blocked."""
    # Create temporary docs directory
    docs_dir = tmp_path / "docs" / "user"
    docs_dir.mkdir(parents=True)

    # Create a file outside the docs directory
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("SECRET DATA")

    # Set environment to use temp directory
    monkeypatch.setenv("DOCS_USER_DIR", str(docs_dir))

    # Try to access file outside directory
    result = load_user_documentation(["../../secret.txt"])

    # Should not load the file
    assert "SECRET DATA" not in result
    assert not result


def test_load_user_documentation_invalid_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that files with invalid extensions are rejected."""
    # Create temporary docs directory
    docs_dir = tmp_path / "docs" / "user"
    docs_dir.mkdir(parents=True)

    # Create a file with invalid extension
    script_file = docs_dir / "script.py"
    script_file.write_text("print('hello')")

    # Set environment to use temp directory
    monkeypatch.setenv("DOCS_USER_DIR", str(docs_dir))

    # Try to load file with invalid extension
    result = load_user_documentation(["script.py"])

    # Should not load the file
    assert "print('hello')" not in result
    assert not result


def test_load_user_documentation_txt_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that .txt files are allowed."""
    # Create temporary docs directory
    docs_dir = tmp_path / "docs" / "user"
    docs_dir.mkdir(parents=True)

    # Create a .txt file
    readme = docs_dir / "README.txt"
    readme.write_text("This is a readme file.")

    # Set environment to use temp directory
    monkeypatch.setenv("DOCS_USER_DIR", str(docs_dir))

    # Load the txt file
    result = load_user_documentation(["README.txt"])

    # Should load successfully
    assert "This is a readme file." in result
    assert "README.txt" in result


def test_load_user_documentation_missing_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test loading documentation when the directory doesn't exist."""
    # Set environment to non-existent directory
    monkeypatch.setenv("DOCS_USER_DIR", "/nonexistent/path")

    # Try to load documentation
    result = load_user_documentation(["USER_GUIDE.md"])

    # Should return empty string
    assert not result
