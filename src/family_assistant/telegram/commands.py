"""The vocabulary of Telegram slash commands, shared by every layer that keys one.

Configuration writes a command, the bot registers it, and a handler looks up an
incoming message against it. Those three see different spellings of the same
word -- Telegram matches a command case-insensitively and accepts it addressed
to a particular bot (``/deep@FamilyBot``) -- so they agree only if they all
normalise it the same way, which is what lives here.

Deliberately free of any dependency on the rest of the application: the
configuration validators import it to refuse a command that could never run, and
they load long before the Telegram service exists.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

# Commands the bot answers itself, with the menu text describing each. A
# configured command claiming one of these names would never run, because these
# handlers are registered first and Telegram dispatches to the first match --
# `AppConfig` refuses such a configuration at startup rather than advertising a
# command that does nothing.
#
# Registration iterates this mapping, so a built-in listed here without a
# callback fails at startup and one added with a callback but not listed here is
# never registered and visibly does nothing. Either way the divergence surfaces,
# rather than quietly leaving a name unprotected from being claimed.
BUILT_IN_COMMANDS: Final[Mapping[str, str]] = MappingProxyType({
    "start": "Start the bot and get a welcome message",
    "interrupt": "Stop the current request",
})

BUILT_IN_SLASH_COMMANDS: Final[frozenset[str]] = frozenset(
    f"/{name}" for name in BUILT_IN_COMMANDS
)


def normalize_slash_command(text: str) -> str:
    """The lookup key for the command ``text`` names, slash included.

    Takes the leading word, drops the ``@bot`` a group chat addresses a command
    with, and lowercases what is left, because Telegram delivers all of those
    spellings to the same handler. Also used on configured commands, so a
    configuration and a message that mean the same command produce the same key.
    """
    words = text.split(maxsplit=1)
    name, _, _ = (words[0] if words else "").partition("@")
    return name.lower()
