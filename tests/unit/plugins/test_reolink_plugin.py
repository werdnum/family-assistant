"""The Reolink camera plugin: where its cameras come from and how tools reach them."""

import json
import logging
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_loader import load_config
from family_assistant.config_models import AppConfig
from family_assistant.plugins.config import PluginsConfig
from family_assistant.plugins.reolink.backend import ReolinkBackend
from family_assistant.plugins.reolink.config import (
    ReolinkCameraItemConfig,
    ReolinkConfig,
)
from family_assistant.plugins.reolink.fake import FakeCameraBackend
from family_assistant.plugins.reolink.instance import ReolinkInstance
from family_assistant.plugins.reolink.plugin import REOLINK_PLUGIN
from family_assistant.plugins.reolink.tools import list_cameras_tool
from family_assistant.plugins.runtime import PluginRuntime, ProfilePlugins
from family_assistant.tools import LOCAL_TOOL_METADATA_BY_NAME
from family_assistant.tools.types import ToolExecutionContext

CAMERA = {"host": "192.168.1.100", "username": "admin", "password": "cam-secret"}


def _load(tmp_path: Path) -> AppConfig:
    return load_config(
        defaults_file_path=str(tmp_path / "missing_defaults.yaml"),
        config_file_path=str(tmp_path / "missing_config.yaml"),
        prompts_file_path=str(tmp_path / "missing_prompts.yaml"),
        load_dotenv_file=False,
    )


def _context(plugins: ProfilePlugins) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="conv",
        user_name="user",
        turn_id=None,
        db_context=Mock(),
        processing_service=None,
        clock=None,
        plugins=plugins,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


class TestConfigSources:
    def test_environment_supplies_the_default_instance_cameras(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """How production runs: REOLINK_CAMERAS holds the cameras as JSON."""
        monkeypatch.setenv("REOLINK_CAMERAS", json.dumps({"coop": CAMERA}))
        config = _load(tmp_path)

        coop = config.plugins.reolink["default"].cameras["coop"]
        assert coop.host == "192.168.1.100"
        assert coop.password.get_secret_value() == "cam-secret"
        assert "cam-secret" not in json.dumps(config.model_dump(mode="json"))

    @pytest.mark.parametrize("value", ["{not json", "[]"])
    def test_a_value_that_is_not_a_json_object_is_logged_and_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        value: str,
    ) -> None:
        monkeypatch.setenv("REOLINK_CAMERAS", value)
        with caplog.at_level(logging.ERROR, logger="family_assistant.config_loader"):
            config = _load(tmp_path)

        assert config.plugins.reolink == {}
        assert "REOLINK_CAMERAS" in caplog.text

    def test_an_unknown_camera_field_is_a_startup_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "REOLINK_CAMERAS", json.dumps({"coop": {**CAMERA, "rtsp_port": 554}})
        )
        with pytest.raises(ValueError, match="rtsp_port"):
            _load(tmp_path)

    def test_a_validation_error_does_not_echo_the_password(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        camera = {key: value for key, value in CAMERA.items() if key != "host"}
        monkeypatch.setenv("REOLINK_CAMERAS", json.dumps({"coop": camera}))
        with (
            caplog.at_level(logging.ERROR, logger="family_assistant.config_loader"),
            pytest.raises(ValueError, match="host") as raised,
        ):
            _load(tmp_path)

        assert "cam-secret" not in str(raised.value)
        assert "cam-secret" not in caplog.text


class TestRuntime:
    def test_an_instance_without_cameras_is_left_out(self) -> None:
        runtime = PluginRuntime(PluginsConfig(reolink={"default": ReolinkConfig()}))
        assert runtime.for_profile({}).get(ReolinkInstance) is None

    def test_configured_cameras_start_a_reolink_backend(self) -> None:
        runtime = PluginRuntime(
            PluginsConfig(
                reolink={
                    "default": ReolinkConfig(
                        cameras={"coop": ReolinkCameraItemConfig.model_validate(CAMERA)}
                    )
                }
            )
        )
        instance = runtime.for_profile({}).get(ReolinkInstance)
        assert instance is not None
        assert isinstance(instance.backend, ReolinkBackend)
        assert runtime.for_profile({"reolink": None}).get(ReolinkInstance) is None

    @pytest.mark.asyncio
    async def test_tools_reach_the_profile_instance_backend(self) -> None:
        backend = FakeCameraBackend()
        backend.add_camera("coop", "Coop")
        with_cameras = _context(ProfilePlugins((ReolinkInstance(backend),)))

        listed = (await list_cameras_tool(with_cameras)).get_data()
        without = (await list_cameras_tool(_context(ProfilePlugins()))).get_data()

        assert isinstance(listed, dict)
        assert listed["count"] == 1
        assert isinstance(without, dict)
        assert "not configured" in without["error"]


def test_camera_tools_keep_their_names_and_tags() -> None:
    assert [tool.name for tool in REOLINK_PLUGIN.tools] == [
        "list_cameras",
        "search_camera_events",
        "get_camera_frame",
        "get_camera_frames_batch",
        "get_camera_recordings",
        "get_live_camera_snapshot",
        "scan_camera_frames",
    ]
    assert {tag.value for tag in LOCAL_TOOL_METADATA_BY_NAME["list_cameras"].tags} == {
        "read_only",
        "sensitive_data",
        "camera",
        "output_trusted",
    }
    for tool in REOLINK_PLUGIN.tools[1:]:
        assert {tag.value for tag in tool.tags} == {
            "read_only",
            "sensitive_data",
            "camera",
            "media",
            "output_untrusted",
        }
