"""New conversation ids are written so a double-click selects them whole."""

import re

from family_assistant.threads.creation import new_thread_conversation_id


def test_a_new_web_thread_id_has_no_dashes() -> None:
    assert re.fullmatch(r"web_conv_[0-9a-f]{32}", new_thread_conversation_id("web"))
