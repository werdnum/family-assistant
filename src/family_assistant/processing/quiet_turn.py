"""Ending a turn without messaging the user.

A turn the application starts on its own -- an automation, an event listener, a
scheduled callback -- does not always have anything worth saying. Such turns are
offered ``end_turn_quietly``: the loop ends, the turn is recorded in history as
an internal row, and nothing is delivered.

The tool is owned by the loop rather than registered with the tool providers,
so it is advertised only on turns whose caller passes ``allow_quiet_end``. That
is the whole gate: a turn somebody is owed a reply on is never shown it, and a
call to it on such a turn fails as an unknown tool.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from family_assistant.llm.tool_call import ToolCallItem
    from family_assistant.tools.types import ToolDefinition

END_TURN_QUIETLY_TOOL_NAME = "end_turn_quietly"

END_TURN_QUIETLY_TOOL_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": END_TURN_QUIETLY_TOOL_NAME,
        "description": (
            "End this turn without sending the user a message. Use it when this "
            "turn was started by an automation, event or scheduled check and "
            "nothing that happened needs the user's attention: the condition "
            "you were checking for isn't met, the work was routine and they "
            "don't need to hear about it, or they have already dealt with it. "
            "If anything is worth telling the user, reply normally instead. "
            "The reason is kept in the conversation history, not shown to the "
            "user. Call it on its own, after any other tools have finished."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "One line on why no message is needed, e.g. 'Washer "
                        "still running; nothing to report'."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
}

QUIET_END_TRIGGER_HINT = (
    "If nothing here needs the user's attention, call end_turn_quietly "
    "instead of replying."
)

QUIET_END_TOOL_RESULT = "Turn ended. No message was sent to the user."


def quiet_end_reason(tool_call: ToolCallItem) -> str:
    """The reason a model gave for ending quietly, or a placeholder."""
    raw_arguments = tool_call.function.arguments
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            arguments = {}
    else:
        arguments = raw_arguments
    reason = arguments.get("reason") if isinstance(arguments, dict) else None
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return "no reason given"


def quiet_end_record(reason: str) -> str:
    """The text of the internal row that records a quiet end."""
    return f"(Ended without messaging the user: {reason})"
