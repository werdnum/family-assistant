"""Tests for the centralized path resolution module."""

from pathlib import Path
from typing import TYPE_CHECKING

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

    def test_frontend_dir(self) -> None:
        assert FRONTEND_DIR == PROJECT_ROOT / "frontend"
        assert FRONTEND_DIR.is_dir()

    def test_static_dir(self) -> None:
        assert STATIC_DIR == PACKAGE_ROOT / "static"

    def test_static_dist_dir(self) -> None:
        assert STATIC_DIST_DIR == STATIC_DIR / "dist"

    def test_templates_dir(self) -> None:
        assert TEMPLATES_DIR == PACKAGE_ROOT / "templates"

    def test_web_resources_dir(self) -> None:
        assert WEB_RESOURCES_DIR == PACKAGE_ROOT / "web" / "resources"

    def test_paths_are_absolute(self) -> None:
        for path in [
            PROJECT_ROOT,
            PACKAGE_ROOT,
            FRONTEND_DIR,
            STATIC_DIR,
            STATIC_DIST_DIR,
            TEMPLATES_DIR,
            WEB_RESOURCES_DIR,
        ]:
            assert path.is_absolute(), f"{path} is not absolute"

    def test_hierarchy_consistency(self) -> None:
        assert PACKAGE_ROOT.parent.parent == PROJECT_ROOT
        assert STATIC_DIR.parent == PACKAGE_ROOT
        assert STATIC_DIST_DIR.parent == STATIC_DIR


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
    """Verify that startup validation runs without errors."""

    def test_dev_mode_validation(self) -> None:
        validate_paths_at_startup(dev_mode=True)

    def test_prod_mode_validation(self) -> None:
        validate_paths_at_startup(dev_mode=False)
