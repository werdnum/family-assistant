import logging
from typing import Protocol, List, Dict, Any, Optional

# Import necessary types and modules from your project.
# These are based on the previously discussed files and common patterns in your project.
from family_assistant.storage.context import DatabaseContext
from family_assistant import storage  # For storage.get_all_notes
from family_assistant import calendar_integration  # For calendar functions

# Define a type alias for prompts if not already a dedicated class
PromptsType = Dict[str, str]

logger = logging.getLogger(__name__)


class ContextProvider(Protocol):
    """
    Interface for objects that can provide context segments for the LLM.
    """

    @property
    def name(self) -> str:
        """A unique, human-readable name for this context provider (e.g., 'calendar', 'notes')."""
        ...

    async def get_context_fragments(self) -> List[str]:
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

    def __init__(self, db_context: DatabaseContext, prompts: PromptsType):
        """
        Initializes the NotesContextProvider.

        Args:
            db_context: The database context for accessing notes.
            prompts: A dictionary containing prompt templates for formatting.
        """
        self._db_context = db_context
        self._prompts = prompts

    @property
    def name(self) -> str:
        return "notes"

    async def get_context_fragments(self) -> List[str]:
        fragments: List[str] = []
        try:
            all_notes = await storage.get_all_notes(db_context=self._db_context)
            if all_notes:
                notes_list_str = ""
                note_item_format = self._prompts.get(
                    "note_item_format", "- {title}: {content}"  # Default format
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
        calendar_config: Dict[str, Any],
        timezone_str: str,
        prompts: PromptsType,
    ):
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

    async def get_context_fragments(self) -> List[str]:
        fragments: List[str] = []
        if not self._calendar_config or not (
            self._calendar_config.get("caldav") or self._calendar_config.get("ical")
        ):
            logger.info(
                f"[{self.name}] Calendar integration not configured or no sources defined."
            )
            # Optionally, add a specific message if desired from prompts:
            # no_calendar_msg = self._prompts.get("calendar_not_configured", "Calendar integration not configured.")
            # if no_calendar_msg:
            # fragments.append(no_calendar_msg)
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

            if formatted_calendar_context: # Ensure not adding empty string
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
# Example:
# class WeatherContextProvider(ContextProvider):
#     def __init__(self, api_key: str, location: str, prompts: PromptsType):
#         self._api_key = api_key
#         self._location = location
#         self._prompts = prompts
#
#     @property
#     def name(self) -> str:
#         return "weather"
#
#     async def get_context_fragments(self) -> List[str]:
#         # ... fetch weather data ...
#         # ... format using self._prompts ...
#         # ... return list of formatted strings ...
#         return []
