"""Tests for template_utils.py that check the real manifest.json."""

import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

from family_assistant.web.template_utils import get_static_asset, should_use_vite_dev

logger = logging.getLogger(__name__)


class TestTemplateUtils:
    """Test the template utilities with real build artifacts."""

    def test_manifest_exists(self) -> None:
        """Test that the manifest.json file exists after npm build."""
        manifest_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "family_assistant"
            / "static"
            / "dist"
            / ".vite"
            / "manifest.json"
        )
        assert manifest_path.exists(), (
            f"manifest.json not found at {manifest_path}. "
            "Run 'npm run build' to generate it."
        )

    def test_manifest_structure(self) -> None:
        """Test that the manifest.json has the expected structure."""
        manifest_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "family_assistant"
            / "static"
            / "dist"
            / ".vite"
            / "manifest.json"
        )

        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        # Check that src/main.js entry exists
        assert "src/main.js" in manifest, "src/main.js entry missing from manifest"

        main_entry = manifest["src/main.js"]
        assert "file" in main_entry, "file property missing from main.js entry"
        assert "css" in main_entry, "css property missing from main.js entry"
        assert isinstance(main_entry["css"], list), "css should be a list"
        assert len(main_entry["css"]) > 0, "main.js should have at least one CSS file"

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_get_static_asset_production_mode_js(self) -> None:
        """Test getting JS assets in production mode."""
        # Force production mode and clear cache
        import family_assistant.web.template_utils as template_utils

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
        import family_assistant.web.template_utils as template_utils

        template_utils._manifest_cache = None

        # Test main.css lookup
        result = get_static_asset("main.css", entry_name="main")

        # Should return the CSS file associated with main.js entry
        assert result.startswith("/static/dist/assets/main-"), (
            f"Expected path to start with '/static/dist/assets/main-', got '{result}'"
        )
        assert result.endswith(".css"), (
            f"Expected path to end with '.css', got '{result}'"
        )

    @patch.dict(os.environ, {"DEV_MODE": "true"})
    def test_get_static_asset_dev_mode(self) -> None:
        """Test getting assets in dev mode."""
        # Test that dev mode returns the simple path
        result = get_static_asset("main.js")
        assert result == "/src/main.js"

        result = get_static_asset("main.css")
        assert result == "/src/main.css"

    def test_should_use_vite_dev_env_var(self) -> None:
        """Test that DEV_MODE environment variable controls dev mode."""
        with patch.dict(os.environ, {"DEV_MODE": "true"}):
            assert should_use_vite_dev() is True

        with (
            patch.dict(os.environ, {"DEV_MODE": "false"}),
            patch("socket.socket") as mock_socket,
        ):
            # This might still return True if Vite is actually running
            # So we mock the socket connection
            mock_socket.return_value.connect_ex.return_value = 1  # Port closed
            assert should_use_vite_dev() is False

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_get_static_asset_missing_file(self) -> None:
        """Test behavior when requested file is not in manifest."""
        # Force production mode and clear cache
        import family_assistant.web.template_utils as template_utils

        template_utils._manifest_cache = None

        # Test with a file that doesn't exist in manifest
        result = get_static_asset("nonexistent.js")

        # Should fall back to direct path
        assert result == "/static/dist/nonexistent.js"

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_manifest_cache_behavior(self) -> None:
        """Test that manifest is cached and reloaded when changed."""
        import family_assistant.web.template_utils as template_utils

        # Clear cache
        template_utils._manifest_cache = None
        template_utils._manifest_last_read = 0

        # First call should load manifest
        result1 = get_static_asset("main.js")
        assert template_utils._manifest_cache is not None
        cache_after_first_call = template_utils._manifest_cache

        # Second call should use cache
        result2 = get_static_asset("main.js")
        assert result1 == result2
        assert template_utils._manifest_cache is cache_after_first_call

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_manifest_error_handling(self) -> None:
        """Test behavior when manifest.json cannot be read."""
        import family_assistant.web.template_utils as template_utils

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
            Path(__file__).parent.parent.parent.parent
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

    @patch.dict(os.environ, {"DEV_MODE": "false"})
    def test_print_actual_paths(self) -> None:
        """Debug test to print actual paths returned by get_static_asset."""
        import family_assistant.web.template_utils as template_utils

        template_utils._manifest_cache = None

        # Get paths for main.js and main.css
        js_path = get_static_asset("main.js")
        css_path = get_static_asset("main.css", entry_name="main")

        logger.info("Actual JS path: %s", js_path)
        logger.info("Actual CSS path: %s", css_path)

        # Also log what's in the manifest
        manifest_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "family_assistant"
            / "static"
            / "dist"
            / ".vite"
            / "manifest.json"
        )
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        logger.info("Manifest content:\n%s", json.dumps(manifest, indent=2))
