"""Tool wiring shared by the Live (voice) entry points.

Both live paths — the browser/iOS ephemeral-token flow and the Asterisk
telephony websocket — declare their tools once, at session setup, and can never
add one afterwards. They therefore resolve their provider and their tool
catalog the same way, through this module, so the two paths cannot drift apart
on what a voice session may reach.

See docs/design/voice-mode-on-demand-tools.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.tools.live_meta import LiveMetaToolsProvider

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService
    from family_assistant.tools import ToolsProvider


async def resolve_live_tools(
    service: ProcessingService,
    *,
    on_demand: bool,
) -> tuple[ToolsProvider, str | None]:
    """Return the provider a live session declares from, and its catalog text.

    With ``on_demand`` set the session declares only the eager tools plus
    ``search_tools``/``call_tool``, and the returned text lists the tools those
    meta-tools can reach so the model knows they exist. With it unset — or for
    a profile that configures nothing as on-demand — the session declares every
    advertisable tool and there is no catalog to add.
    """
    provider = service.live_tools_provider
    if not on_demand or not isinstance(provider, LiveMetaToolsProvider):
        return service.tools_provider, None
    return provider, await provider.get_system_prompt_addition()
