import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from family_assistant import (
    calendar_integration,  # For calendar functions
    storage,  # For storage.get_all_notes
)

# Import necessary types and modules from your project.
# These are based on the previously discussed files and common patterns in your project.
from family_assistant.storage.context import DatabaseContext

# Define a type alias for prompts if not already a dedicated class
PromptsType = dict[str, str]

logger = logging.getLogger(__name__)


class ContextProvider(Protocol):
    """
    Interface for objects that can provide context segments for the LLM.
    """

    @property
    def name(self) -> str:
        """A unique, human-readable name for this context provider (e.g., 'calendar', 'notes')."""
        ...

    async def get_context_fragments(self) -> list[str]:
        """
        Asynchronously retrieves and formats context fragments relevant to this provider.
        Each string in the list represents a distinct piece of formatted information
        ready to be included in a larger context block (e.g., the system prompt).

        Returns:
            A list of strings, where each string is a formatted context fragment.
            Returns an empty list if no context is available or an error occurs
            (errors should be logged by the provider).
        """
        ...


class NotesContextProvider(ContextProvider):
    """Provides context from stored notes."""

    def __init__(
        self,
        get_db_context_func: Callable[[], Awaitable[DatabaseContext]],
        prompts: PromptsType,
    ) -> None:
        """
        Initializes the NotesContextProvider.

        Args:
            get_db_context_func: An async function that returns a DatabaseContext.
            prompts: A dictionary containing prompt templates for formatting.
        """
        self._get_db_context_func = get_db_context_func
        self._prompts = prompts

    @property
    def name(self) -> str:
        return "notes"

    async def get_context_fragments(self) -> list[str]:
        fragments: list[str] = []
        try:
            async with (
                await self._get_db_context_func() as db_context
            ):  # Get context per call
                all_notes = await storage.get_all_notes(db_context=db_context)
                if all_notes:
                    notes_list_str = ""
                    note_item_format = self._prompts.get(
                        "note_item_format",
                        "- {title}: {content}",  # Default format
                    )
                    for note in all_notes:
                        notes_list_str += (
                            note_item_format.format(
                                title=note["title"], content=note["content"]
                            )
                            + "\n"
                        )

                    notes_context_header_template = self._prompts.get(
                        "notes_context_header", "Relevant notes:\n{notes_list}"
                    )
                    formatted_notes_context = notes_context_header_template.format(
                        notes_list=notes_list_str.strip()
                    ).strip()
                    # Ensure not adding an empty string if formatting results in it
                    if formatted_notes_context:
                        fragments.append(formatted_notes_context)
                else:
                    # Only add "no notes" message if it's defined and non-empty
                    no_notes_message = self._prompts.get("no_notes")
                    if no_notes_message:  # Check if the message exists and is not empty
                        fragments.append(no_notes_message)
                logger.debug(
                    f"[{self.name}] Formatted {len(all_notes)} notes into {len(fragments)} fragment(s)."
                )
        except Exception as e:
            logger.error(
                f"[{self.name}] Failed to get notes context: {e}", exc_info=True
            )
            # As per protocol, return empty list on error, error is logged.
            return []
        return fragments


class CalendarContextProvider(ContextProvider):
    """Provides context from calendar events."""

    def __init__(
        self,
        calendar_config: dict[str, Any],
        timezone_str: str,
        prompts: PromptsType,
    ) -> None:
        """
        Initializes the CalendarContextProvider.

        Args:
            calendar_config: Configuration dictionary for calendar sources.
            timezone_str: The local timezone string (e.g., "Europe/London").
            prompts: A dictionary containing prompt templates for formatting.
        """
        self._calendar_config = calendar_config
        self._timezone_str = timezone_str
        self._prompts = prompts

    @property
    def name(self) -> str:
        return "calendar"

    async def get_context_fragments(self) -> list[str]:
        fragments: list[str] = []
        if not self._calendar_config or not (
            self._calendar_config.get("caldav") or self._calendar_config.get("ical")
        ):
            logger.info(
                f"[{self.name}] Calendar integration not configured or no sources defined."
            )
            return []  # Return empty list as per protocol

        try:
            upcoming_events = await calendar_integration.fetch_upcoming_events(
                calendar_config=self._calendar_config,
                timezone_str=self._timezone_str,
            )
            # format_events_for_prompt itself uses prompts for individual event lines
            # and messages for no events.
            today_events_str, future_events_str = (
                calendar_integration.format_events_for_prompt(
                    events=upcoming_events,
                    prompts=self._prompts,  # Pass the prompts dict here
                    timezone_str=self._timezone_str,
                )
            )
            calendar_header_template = self._prompts.get(
                "calendar_context_header",
                "Upcoming Events (Today & Tomorrow):\n{today_tomorrow_events}\n\nUpcoming Events (Next 2 Weeks, max 10 shown):\n{next_two_weeks_events}",
            )
            formatted_calendar_context = calendar_header_template.format(
                today_tomorrow_events=today_events_str,
                next_two_weeks_events=future_events_str,
            ).strip()

            if formatted_calendar_context:  # Ensure not adding empty string
                fragments.append(formatted_calendar_context)
            logger.debug(
                f"[{self.name}] Formatted upcoming events into {len(fragments)} fragment(s)."
            )
        except Exception as e:
            logger.error(
                f"[{self.name}] Failed to fetch or format calendar events: {e}",
                exc_info=True,
            )
            # As per protocol, return empty list on error, error is logged.
            return []
        return fragments


# Future providers like WeatherContextProvider, EmailSummaryProvider etc. would go here.


class KnownUsersContextProvider(ContextProvider):
    """Provides context about known users and their chat IDs."""

    def __init__(
        self,
        chat_id_to_name_map: dict[int, str],
        prompts: PromptsType,
    ) -> None:
        """
        Initializes the KnownUsersContextProvider.

        Args:
            chat_id_to_name_map: A dictionary mapping chat IDs to user names.
            prompts: A dictionary containing prompt templates for formatting.
        """
        self._chat_id_to_name_map = chat_id_to_name_map
        self._prompts = prompts

    @property
    def name(self) -> str:
        return "known_users"

    async def get_context_fragments(self) -> list[str]:
        fragments: list[str] = []
        if not self._chat_id_to_name_map:
            no_users_message = self._prompts.get("no_known_users")
            if no_users_message:
                fragments.append(no_users_message)
            logger.debug(f"[{self.name}] No known users configured.")
            return fragments

        try:
            user_list_str = ""
            user_item_format = self._prompts.get(
                "known_user_item_format", "- {name} (Chat ID: {chat_id})"
            )
            for chat_id, name in self._chat_id_to_name_map.items():
                user_list_str += (
                    user_item_format.format(name=name, chat_id=chat_id) + "\n"
                )

            if user_list_str:
                users_header_template = self._prompts.get(
                    "known_users_header",
                    "Known users you can interact with:\n{user_list}",
                )
                formatted_users_context = users_header_template.format(
                    user_list=user_list_str.strip()
                ).strip()
                if formatted_users_context:
                    fragments.append(formatted_users_context)

            logger.debug(
                f"[{self.name}] Formatted {len(self._chat_id_to_name_map)} known users into {len(fragments)} fragment(s)."
            )
        except Exception as e:
            logger.error(
                f"[{self.name}] Failed to get known users context: {e}", exc_info=True
            )
            return []
        return fragments
