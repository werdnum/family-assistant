"""Tests for the centralized path resolution module."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from family_assistant import paths
from family_assistant.paths import (
    FRONTEND_DIR,
    PACKAGE_ROOT,
    PROJECT_ROOT,
    STATIC_DIR,
    STATIC_DIST_DIR,
    TEMPLATES_DIR,
    WEB_RESOURCES_DIR,
    get_docs_user_dir,
    validate_paths_at_startup,
)

if TYPE_CHECKING:
    import pytest


class TestPathConstants:
    """Verify all path constants resolve to expected locations."""

    def test_project_root_contains_pyproject(self) -> None:
        assert (PROJECT_ROOT / "pyproject.toml").is_file()

    def test_package_root_is_family_assistant(self) -> None:
        assert PACKAGE_ROOT.name == "family_assistant"
        assert (PACKAGE_ROOT / "__init__.py").is_file()

    def test_committed_resource_directories(self) -> None:
        assert FRONTEND_DIR.is_dir()
        assert STATIC_DIR.is_dir()
        assert TEMPLATES_DIR.is_dir()
        assert WEB_RESOURCES_DIR.is_dir()

    def test_resource_directories_resolve_from_their_expected_roots(self) -> None:
        assert PACKAGE_ROOT.parent.parent == PROJECT_ROOT
        assert FRONTEND_DIR == PROJECT_ROOT / "frontend"
        assert STATIC_DIR == PACKAGE_ROOT / "static"
        assert STATIC_DIST_DIR == STATIC_DIR / "dist"
        assert TEMPLATES_DIR == PACKAGE_ROOT / "templates"
        assert WEB_RESOURCES_DIR == PACKAGE_ROOT / "web" / "resources"
        assert all(
            path.is_absolute()
            for path in (
                PROJECT_ROOT,
                PACKAGE_ROOT,
                FRONTEND_DIR,
                STATIC_DIR,
                STATIC_DIST_DIR,
                TEMPLATES_DIR,
                WEB_RESOURCES_DIR,
            )
        )


class TestGetDocsUserDir:
    """Verify docs directory resolution."""

    def test_default_resolves_under_project_root(self) -> None:
        docs = get_docs_user_dir()
        assert docs == PROJECT_ROOT / "docs" / "user"

    def test_env_override(
        self, monkeypatch: "pytest.MonkeyPatch", tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DOCS_USER_DIR", str(tmp_path))
        docs = get_docs_user_dir()
        assert docs == tmp_path.resolve()


class TestValidatePathsAtStartup:
    """Verify startup validation reports missing expected directories."""

    def test_dev_mode_validation(
        self,
        caplog: "pytest.LogCaptureFixture",
        monkeypatch: "pytest.MonkeyPatch",
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("DOCS_USER_DIR", raising=False)
        monkeypatch.setattr(paths, "STATIC_DIST_DIR", tmp_path / "missing-dist")
        validate_paths_at_startup(dev_mode=True)
        assert not [
            record
            for record in caplog.records
            if record.name == paths.logger.name and record.levelno >= logging.WARNING
        ]

    def test_missing_docs_warning(
        self,
        caplog: "pytest.LogCaptureFixture",
        monkeypatch: "pytest.MonkeyPatch",
        tmp_path: Path,
    ) -> None:
        missing_docs = tmp_path / "missing-docs"
        monkeypatch.setenv("DOCS_USER_DIR", str(missing_docs))
        validate_paths_at_startup(dev_mode=True)
        assert any(
            record.name == paths.logger.name
            and record.levelno == logging.WARNING
            and "docs/user" in record.getMessage()
            and str(missing_docs) in record.getMessage()
            for record in caplog.records
        )

    def test_prod_mode_validation(
        self,
        caplog: "pytest.LogCaptureFixture",
        monkeypatch: "pytest.MonkeyPatch",
        tmp_path: Path,
    ) -> None:
        missing_dist = tmp_path / "missing-dist"
        monkeypatch.setattr(paths, "STATIC_DIST_DIR", missing_dist)
        validate_paths_at_startup(dev_mode=False)
        assert any(
            record.name == paths.logger.name
            and record.levelno == logging.WARNING
            and "static/dist (prod)" in record.getMessage()
            and str(missing_dist) in record.getMessage()
            for record in caplog.records
        )
