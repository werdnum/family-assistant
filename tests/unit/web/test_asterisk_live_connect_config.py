"""The phone-call Live session carries every configured Gemini Live setting."""

from google.genai.types import ThinkingConfig

from family_assistant.web.models import (
    GeminiLiveAffectiveDialogConfig,
    GeminiLiveConfig,
    GeminiLiveProactivityConfig,
    GeminiLiveThinkingConfig,
)
from family_assistant.web.routers.asterisk_live_api import build_live_connect_config


def test_defaults_leave_optional_features_unset() -> None:
    config = build_live_connect_config(
        GeminiLiveConfig(), system_instruction=None, tools=None
    )

    assert config.enable_affective_dialog is None
    assert config.thinking_config is None
    assert config.proactivity is None


def test_configured_features_reach_the_session() -> None:
    live_config = GeminiLiveConfig(
        affective_dialog=GeminiLiveAffectiveDialogConfig(enabled=True),
        proactivity=GeminiLiveProactivityConfig(enabled=True, proactive_audio=True),
        thinking=GeminiLiveThinkingConfig(include_thoughts=True),
    )

    config = build_live_connect_config(
        live_config, system_instruction="Be brief.", tools=None
    )

    assert config.enable_affective_dialog is True
    assert config.thinking_config == ThinkingConfig(include_thoughts=True)
    assert config.proactivity is not None
    assert config.proactivity.proactive_audio is True
