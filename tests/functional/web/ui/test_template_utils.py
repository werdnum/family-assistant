"""Tests for template_utils.py that check the real manifest.json."""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from family_assistant.web import template_utils
from family_assistant.web.template_utils import get_static_asset


class TestTemplateUtils:
    """Test the template utilities with real build artifacts."""

    def test_manifest_structure(self) -> None:
        """Test that the manifest.json has the expected structure."""
        manifest_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "family_assistant"
            / "static"
            / "dist"
            / ".vite"
            / "manifest.json"
        )

        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        # Check that index.html entry exists (new structure uses HTML as entry points)
        assert "index.html" in manifest, "index.html entry missing from manifest"

        main_entry = manifest["index.html"]
        assert "file" in main_entry, "file property missing from index.html entry"

        # CSS might be in the entry itself or in imported modules
        has_css = "css" in main_entry and len(main_entry.get("css", [])) > 0

        # Check if CSS is in imported modules
        if not has_css and "imports" in main_entry:
            for import_key in main_entry["imports"]:
                if import_key in manifest and "css" in manifest[import_key]:
                    has_css = True
                    break

        assert has_css, "No CSS files found in index.html entry or its imports"

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_get_static_asset_production_mode_js(self) -> None:
        """Test getting JS assets in production mode."""
        # Force production mode and clear cache

        template_utils._manifest_cache = None

        # Test main.js lookup
        result = get_static_asset("main.js")

        # Should return the hashed filename from manifest
        assert result.startswith("/static/dist/assets/main-"), (
            f"Expected path to start with '/static/dist/assets/main-', got '{result}'"
        )
        assert result.endswith(".js"), (
            f"Expected path to end with '.js', got '{result}'"
        )

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_get_static_asset_production_mode_css(self) -> None:
        """Test getting CSS assets in production mode."""
        # Force production mode and clear cache

        template_utils._manifest_cache = None

        # Test main.css lookup
        result = get_static_asset("main.css", entry_name="main")

        # Should return the first CSS file associated with the main entry
        # The main entry can include both main-*.css and custom-*.css files
        assert result.startswith("/static/dist/assets/"), (
            f"Expected path to start with '/static/dist/assets/', got '{result}'"
        )
        assert result.endswith(".css"), (
            f"Expected path to end with '.css', got '{result}'"
        )
        # Should match patterns for main-*.css, custom-*.css, or globals-*.css files
        assert re.search(
            r"/static/dist/assets/(main|custom|globals)-.*\.css", result
        ), (
            f"Expected path to match 'main-*.css', 'custom-*.css', or 'globals-*.css', got '{result}'"
        )

    def test_get_static_asset_dev_mode(self) -> None:
        """Test getting assets in dev mode."""
        # Test that dev mode returns the Vite dev server URLs
        result = get_static_asset("main.js", dev_mode=True)
        assert result == "/src/main.js"

        # CSS returns empty string in dev mode (handled by Vite JS)
        result = get_static_asset("main.css", dev_mode=True)
        assert not result

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_get_static_asset_missing_file(self) -> None:
        """Test behavior when requested file is not in manifest."""
        # Force production mode and clear cache

        template_utils._manifest_cache = None

        # Test with a file that doesn't exist in manifest
        result = get_static_asset("nonexistent.js")

        # Should fall back to direct path
        assert result == "/static/dist/nonexistent.js"

    def test_manifest_reloads_when_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A changed manifest should update the asset path returned to callers."""
        manifest_dir = tmp_path / ".vite"
        manifest_dir.mkdir()
        manifest_path = manifest_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps({"index.html": {"file": "assets/main-first.js"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(template_utils, "STATIC_DIST_DIR", tmp_path)
        monkeypatch.setattr(template_utils, "_manifest_cache", None)
        monkeypatch.setattr(template_utils, "_manifest_last_read", 0)

        assert get_static_asset("main.js") == "/static/dist/assets/main-first.js"

        previous_mtime_ns = manifest_path.stat().st_mtime_ns
        manifest_path.write_text(
            json.dumps({"index.html": {"file": "assets/main-second.js"}}),
            encoding="utf-8",
        )
        updated_mtime_ns = previous_mtime_ns + 1_000_000_000
        os.utime(manifest_path, ns=(updated_mtime_ns, updated_mtime_ns))

        assert get_static_asset("main.js") == "/static/dist/assets/main-second.js"

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_manifest_error_handling(self) -> None:
        """Test behavior when manifest.json cannot be read."""

        # Clear cache
        template_utils._manifest_cache = None

        # Mock open to raise an exception
        with patch("builtins.open", side_effect=Exception("Read error")):
            result = get_static_asset("main.js")

            # Should fall back to direct path
            assert result == "/static/dist/main.js"

    def test_real_manifest_content_matches_build(self) -> None:
        """Test that the actual manifest content matches what's in the build directory."""
        manifest_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "family_assistant"
            / "static"
            / "dist"
            / ".vite"
            / "manifest.json"
        )

        dist_path = manifest_path.parent.parent  # static/dist/

        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        # Check that all files referenced in manifest actually exist
        for _entry_key, entry_data in manifest.items():
            if "file" in entry_data:
                file_path = dist_path / entry_data["file"]
                assert file_path.exists(), (
                    f"File {entry_data['file']} referenced in manifest "
                    f"does not exist at {file_path}"
                )

            if "css" in entry_data:
                for css_file in entry_data["css"]:
                    css_path = dist_path / css_file
                    assert css_path.exists(), (
                        f"CSS file {css_file} referenced in manifest "
                        f"does not exist at {css_path}"
                    )
