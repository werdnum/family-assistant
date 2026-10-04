import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable
from zoneinfo import ZoneInfo

from family_assistant import (
    calendar_integration,  # For calendar functions
)
from family_assistant.google_calendar import (
    google_event_to_calendar_event,
    is_user_vetted_event,
)
from family_assistant.security.note_provenance import note_read_taint
from family_assistant.security.taint import (
    TaintSource,
)
from family_assistant.services.api_backend import ApiBackendError
from family_assistant.services.google_api import GoogleApiError
from family_assistant.services.oauth_credentials import (
    OAuthCredentialError,
    OAuthNoActingUserError,
    OAuthNotConnectedError,
    OAuthScopeNotGrantedError,
)
from family_assistant.storage.database import Database

# Define a type alias for prompts if not already a dedicated class
PromptsType = dict[str, str]

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from family_assistant.google_calendar import (
        GoogleCalendarClient,
        GoogleCalendarFactory,
    )
    from family_assistant.skills.registry import NoteRegistry
    from family_assistant.storage.repositories.notes import NoteReadPolicy
    from family_assistant.tools.types import CalendarConfig, CalendarEvent
    from family_assistant.weather import WeatherService

# Matches the window fetch_upcoming_events reads from CalDAV and iCal.
_GOOGLE_CONTEXT_WINDOW_DAYS = 16
# Event titles are short in practice; the cap keeps one oversized title from
# crowding the rest of the per-turn context.
_GOOGLE_CONTEXT_SUMMARY_LIMIT = 200


class ContextProvider(Protocol):
    """
    Interface for objects that can provide context segments for the LLM.
    """

    @property
    def name(self) -> str:
        """A unique, human-readable name for this context provider (e.g., 'calendar', 'notes')."""
        ...

    async def get_context_fragments(self, acting_user_id: str | None) -> list[str]:
        """
        Asynchronously retrieves and formats context fragments relevant to this provider.

        ``acting_user_id`` is the user the turn acts for (None when there is
        none). Providers of deployment-wide data ignore it; a provider of
        per-user data must show only that user's own.
        Each string in the list represents a distinct piece of formatted information
        ready to be included in a larger context block (e.g., the per-turn
        ``<turn_context>`` block).

        Returns:
            A list of strings, where each string is a formatted context fragment.
            Returns an empty list if no context is available or an error occurs
            (errors should be logged by the provider).
        """
        ...


@runtime_checkable
class TaintedContextProvider(Protocol):
    """Optional interface for context providers that surface stored taint."""

    async def get_context_taint_sources(self) -> tuple[TaintSource, ...]:
        """Return taint sources introduced by the context fragments."""
        ...


@runtime_checkable
class AmbientReviewContextProvider(Protocol):
    """A context provider whose ambient material the tool-call reviewer may read."""

    async def get_ambient_review_fragments(self) -> list[str]:
        """Return the eligible ambient material as the prompt renders it."""
        ...


class NotesContextProvider(ContextProvider):
    """Provides context from stored notes and skills."""

    def __init__(
        self,
        get_db_context_func: Callable[[], Database],
        prompts: PromptsType,
        read_policy: "NoteReadPolicy",
        attachment_registry: Any = None,  # noqa: ANN401 # AttachmentRegistry | None
        note_registry: "NoteRegistry | None" = None,
    ) -> None:
        """
        Initializes the NotesContextProvider.

        Args:
            get_db_context_func: A function that returns a Database handle.
            prompts: A dictionary containing prompt templates for formatting.
            attachment_registry: Optional attachment registry for fetching attachment metadata.
            read_policy: The profile's note read confinement. Every note and
                skill this provider surfaces is resolved through it.
            note_registry: Optional registry of file-based skills.
        """
        self._get_db_context_func = get_db_context_func
        self._prompts = prompts
        self._attachment_registry = attachment_registry
        self._read_policy = read_policy
        self._note_registry = note_registry

    @property
    def name(self) -> str:
        return "notes"

    async def _format_attachment(
        self, db_context: Database, attachment_id: str, attachment_format: str
    ) -> str:
        registry = self._attachment_registry
        if registry is None:
            return ""

        # Note attachments are ownerless, and this provider runs with no
        # acting-user context, so ``None`` (ownerless-only) is correct:
        # owner-scoped attachments never surface in note context lines.
        metadata = await registry.get_attachment(
            db_context, attachment_id, acting_user_id=None
        )
        if not metadata:
            logger.warning(
                f"[{self.name}] Attachment {attachment_id} not found in registry"
            )
            return ""

        return attachment_format.format(
            id=attachment_id,
            filename=metadata.description or "attachment",
            mime_type=metadata.mime_type,
        )

    async def _format_attachments(
        self, db_context: Database, attachment_ids: list[str]
    ) -> str:
        """
        Formats attachment metadata for display in the prompt.

        Args:
            db_context: Database context for fetching attachment metadata.
            attachment_ids: List of attachment IDs to format.

        Returns:
            Formatted string with attachment references, one per line.
        """
        if not attachment_ids or not self._attachment_registry:
            return ""

        attachment_lines = []
        attachment_format = self._prompts.get(
            "note_attachment_format", "  📎 [{id}] {filename} ({mime_type})"
        )

        for attachment_id in attachment_ids:
            try:
                attachment_line = await self._format_attachment(
                    db_context, attachment_id, attachment_format
                )
            except Exception as e:
                logger.warning(
                    f"[{self.name}] Failed to fetch attachment metadata for {attachment_id}: {e}"
                )
            else:
                if attachment_line:
                    attachment_lines.append(attachment_line)

        return "\n".join(attachment_lines)

    async def _build_context_fragments(
        self, *, ambient_material_only: bool = False
    ) -> list[str]:
        fragments: list[str] = []
        db_context = self._get_db_context_func()
        # Use targeted queries - skills are identified at write time via is_skill column
        prompt_notes = await db_context.notes.get_prompt_notes(
            read_policy=self._read_policy
        )
        db_skills = await db_context.notes.get_skills(read_policy=self._read_policy)
        excluded_titles = (
            []
            if ambient_material_only
            else await db_context.notes.get_excluded_notes_titles(
                read_policy=self._read_policy
            )
        )

        # 1. Regular notes section
        if prompt_notes:
            notes_list_str = ""
            note_item_format = self._prompts.get(
                "note_item_format",
                "- {title}: {content}",  # Default format
            )
            for note in prompt_notes:
                note_text = note_item_format.format(
                    title=note.title, content=note.content
                )
                notes_list_str += note_text + "\n"

                # Add attachment references if present
                attachment_ids = note.attachment_ids
                if attachment_ids:
                    attachment_text = await self._format_attachments(
                        db_context, attachment_ids
                    )
                    if attachment_text:
                        notes_list_str += attachment_text + "\n"

            notes_context_header_template = self._prompts.get(
                "notes_context_header", "Relevant notes:\n{notes_list}"
            )
            formatted_notes_context = notes_context_header_template.format(
                notes_list=notes_list_str.strip()
            ).strip()
            if formatted_notes_context:
                fragments.append(formatted_notes_context)
        elif not ambient_material_only:
            no_notes_message = self._prompts.get("no_notes")
            if no_notes_message:
                fragments.append(no_notes_message)

        # 2. Skill catalog (DB skills + file-based skills)
        file_skills = (
            self._note_registry.get_skill_catalog(self._read_policy)
            if self._note_registry
            else []
        )
        if db_skills or file_skills:
            catalog_lines = [
                "## Available Skills",
                "Use the `get_note` tool to load a skill's full instructions.",
            ]
            for skill in db_skills:
                catalog_lines.append(
                    f"- **{skill.skill_name}**: {skill.skill_description}"
                )
            for skill in file_skills:
                catalog_lines.append(f"- **{skill.name}**: {skill.description}")
            fragments.append("\n".join(catalog_lines))

        # 3. Excluded regular notes
        if excluded_titles:
            excluded_notes_format = self._prompts.get(
                "excluded_notes_format",
                "Other available notes (not included above): {excluded_titles}",
            )
            excluded_titles_str = ", ".join(f'"{title}"' for title in excluded_titles)
            formatted_excluded_notes = excluded_notes_format.format(
                excluded_titles=excluded_titles_str
            ).strip()
            if formatted_excluded_notes:
                fragments.append(formatted_excluded_notes)

        logger.debug(
            "[%s] Formatted %d notes, %d DB skills, %d file skills into %d fragment(s).",
            self.name,
            len(prompt_notes),
            len(db_skills),
            len(file_skills),
            len(fragments),
        )
        return fragments

    async def get_context_fragments(self, acting_user_id: str | None) -> list[str]:
        try:
            return await self._build_context_fragments()
        except Exception as e:
            logger.exception(f"[{self.name}] Failed to get notes context: {e}")
            return []

    async def get_ambient_review_fragments(self) -> list[str]:
        """The eligible ambient material, exactly as the prompt renders it.

        Included notes with their attachment metadata, and the skill catalog --
        nothing else from the turn-context block, so unreviewed titles never
        reach the tool-call reviewer through this path.
        """
        return await self._build_context_fragments(ambient_material_only=True)

    async def get_context_taint_sources(self) -> tuple[TaintSource, ...]:
        """Return provenance taint for the notes and skills the prompt carries.

        Both the included bodies and the catalogued database skills: a skill's
        name and description are in every prompt exactly as a body is. Only
        eligible rows reach the prompt, so what this contributes is at most
        ``machine_reviewed`` -- the turn's tier then says the model processed
        reviewed external text.
        """
        sources: list[TaintSource] = []
        db_context = self._get_db_context_func()
        prompt_notes = await db_context.notes.get_prompt_notes(
            read_policy=self._read_policy
        )
        db_skills = await db_context.notes.get_skills(read_policy=self._read_policy)
        for note in (*prompt_notes, *db_skills):
            kind = "skill" if note.is_skill else "note"
            state = note_read_taint(
                note.provenance_metadata,
                title=note.title,
                labels=frozenset(note.visibility_labels),
                reason=(
                    f"Prompt-included {kind} '{note.title}' carries stored "
                    "provenance taint."
                ),
            )
            if state is not None:
                sources.extend(state.sources)
        return tuple(sources)


# --- BEGIN WeatherContextProvider ---
# Add necessary imports for WeatherContextProvider


class WeatherContextProvider(ContextProvider):
    """Provides the WillyWeather forecast as per-turn context."""

    def __init__(
        self,
        weather_service: "WeatherService",
        prompts: PromptsType,
        timezone: ZoneInfo,  # Target display timezone
    ) -> None:
        self._weather_service = weather_service
        self._prompts = prompts
        self._display_tz = timezone

    @property
    def name(self) -> str:
        return "weather"

    async def get_context_fragments(self, acting_user_id: str | None) -> list[str]:
        """Asynchronously retrieves and formats weather context fragments."""
        return await self._weather_service.get_forecast_fragments(
            self._display_tz, self._prompts
        )


# --- END WeatherContextProvider ---


class CalendarContextProvider(ContextProvider):
    """Provides context from calendar events.

    Configured CalDAV calendars and iCal feeds are shown to every turn. When the
    deployment offers Google Calendar, the acting user's *primary* Google
    calendar is added for that user's turns only; their other Google calendars
    stay reachable through the calendar tools rather than filling every prompt.
    """

    def __init__(
        self,
        calendar_config: "CalendarConfig",
        timezone: ZoneInfo,
        prompts: PromptsType,
        clock: calendar_integration.Clock | None = None,
        google_calendar_for_user: "GoogleCalendarFactory | None" = None,
    ) -> None:
        """
        Initializes the CalendarContextProvider.

        Args:
            calendar_config: Configuration dictionary for calendar sources.
            timezone: The local timezone for display.
            prompts: A dictionary containing prompt templates for formatting.
            clock: A clock object for managing time.
            google_calendar_for_user: Builds a Google Calendar client for a
                turn's acting user; None when the deployment does not offer
                Google Calendar.
        """
        self._calendar_config = calendar_config
        self._timezone = timezone
        self._prompts = prompts
        self._clock = clock or calendar_integration.SystemClock()
        self._google_calendar_for_user = google_calendar_for_user

    @property
    def name(self) -> str:
        return "calendar"

    async def get_context_fragments(self, acting_user_id: str | None) -> list[str]:
        has_configured_sources = bool(
            self._calendar_config
            and (
                self._calendar_config.get("caldav") or self._calendar_config.get("ical")
            )
        )
        google_client = (
            self._google_calendar_for_user(acting_user_id)
            if self._google_calendar_for_user is not None and acting_user_id
            else None
        )
        if not has_configured_sources and google_client is None:
            logger.info(
                f"[{self.name}] Calendar integration not configured or no sources defined."
            )
            return []  # Return empty list as per protocol

        try:
            upcoming_events, google_note = await self._gather_upcoming_events(
                has_configured_sources, google_client
            )
            formatted_calendar_context = self._format_calendar_context(upcoming_events)
        except Exception as e:
            logger.exception(
                f"[{self.name}] Failed to fetch or format calendar events: {e}"
            )
            # As per protocol, return empty list on error, error is logged.
            return []

        fragments: list[str] = []
        if formatted_calendar_context:  # Ensure not adding empty string
            if google_note:
                formatted_calendar_context = (
                    f"{formatted_calendar_context}\n\n{google_note}"
                )
            fragments.append(formatted_calendar_context)
        logger.debug(
            f"[{self.name}] Formatted upcoming events into {len(fragments)} fragment(s)."
        )
        return fragments

    async def _gather_upcoming_events(
        self,
        has_configured_sources: bool,
        google_client: "GoogleCalendarClient | None",
    ) -> "tuple[list[CalendarEvent], str | None]":
        """Configured and Google events in start order, plus any Google note."""
        upcoming_events: list[CalendarEvent] = []
        if has_configured_sources:
            upcoming_events = await calendar_integration.fetch_upcoming_events(
                calendar_config=cast("CalendarConfig", self._calendar_config),
                timezone=self._timezone,
            )
        if google_client is None:
            return upcoming_events, None
        google_events, google_note = await self._fetch_google_primary_events(
            google_client
        )
        merged = sorted(
            [*upcoming_events, *google_events],
            key=lambda event: calendar_integration.event_sort_key(
                event, self._timezone
            ),
        )
        return merged, google_note

    def _format_calendar_context(self, events: "list[CalendarEvent]") -> str:
        # format_events_for_prompt itself uses prompts for individual event lines
        # and messages for no events.
        today_events_str, future_events_str = (
            calendar_integration.format_events_for_prompt(
                events=events,
                prompts=self._prompts,
                timezone=self._timezone,
                clock=self._clock,
            )
        )
        calendar_header_template = self._prompts.get(
            "calendar_context_header",
            "Upcoming Events (Today & Tomorrow):\n{today_tomorrow_events}\n\nUpcoming Events (Next 2 Weeks, max 10 shown):\n{next_two_weeks_events}",
        )
        return calendar_header_template.format(
            today_tomorrow_events=today_events_str,
            next_two_weeks_events=future_events_str,
        ).strip()

    async def _fetch_google_primary_events(
        self, client: "GoogleCalendarClient"
    ) -> "tuple[list[CalendarEvent], str | None]":
        """The user's own upcoming events on their primary Google calendar.

        Only events the user created, organises or accepted are included (see
        :func:`is_user_vetted_event`): an unanswered invitation is authored by
        whoever sent it, and this context reaches every turn untainted.

        A user who has not connected Google, or who declined calendar access,
        simply gets no Google events. Any other failure is reported in the
        context so the assistant does not present an incomplete calendar as the
        whole picture.
        """
        today = self._clock.now().astimezone(self._timezone).date()
        time_min = datetime.combine(today, datetime.min.time(), tzinfo=self._timezone)
        time_max = time_min + timedelta(days=_GOOGLE_CONTEXT_WINDOW_DAYS)
        source = client.primary_source()
        try:
            items = await client.list_events("primary", time_min, time_max)
        except (
            OAuthNoActingUserError,
            OAuthNotConnectedError,
            OAuthScopeNotGrantedError,
        ):
            return [], None
        except (OAuthCredentialError, GoogleApiError, ApiBackendError) as exc:
            logger.warning(
                "[%s] Could not load Google Calendar events: %s", self.name, exc
            )
            return [], f"Note: Google Calendar events could not be loaded: {exc}"

        events: list[CalendarEvent] = []
        for item in items:
            if not is_user_vetted_event(item):
                continue
            event = google_event_to_calendar_event(item, source, self._timezone)
            if event is None:
                continue
            if len(event["summary"]) > _GOOGLE_CONTEXT_SUMMARY_LIMIT:
                event["summary"] = (
                    event["summary"][:_GOOGLE_CONTEXT_SUMMARY_LIMIT] + "…"
                )
            events.append(event)
        return events, None


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

    def _format_known_users(self) -> list[str]:
        user_item_format = self._prompts.get(
            "known_user_item_format", "- {name} (Chat ID: {chat_id})"
        )
        user_list_str = "".join(
            user_item_format.format(name=name, chat_id=chat_id) + "\n"
            for chat_id, name in self._chat_id_to_name_map.items()
        )
        if not user_list_str:
            return []

        users_header_template = self._prompts.get(
            "known_users_header",
            "Known users you can interact with:\n{user_list}",
        )
        formatted_users_context = users_header_template.format(
            user_list=user_list_str.strip()
        ).strip()
        return [formatted_users_context] if formatted_users_context else []

    async def get_context_fragments(self, acting_user_id: str | None) -> list[str]:
        fragments: list[str] = []
        if not self._chat_id_to_name_map:
            no_users_message = self._prompts.get("no_known_users")
            if no_users_message:
                fragments.append(no_users_message)
            logger.debug(f"[{self.name}] No known users configured.")
            return fragments

        try:
            fragments = self._format_known_users()
        except Exception as e:
            logger.exception(f"[{self.name}] Failed to get known users context: {e}")
            return []
        logger.debug(
            f"[{self.name}] Formatted {len(self._chat_id_to_name_map)} known users into {len(fragments)} fragment(s)."
        )
        return fragments

    async def get_context_taint_sources(self) -> tuple[TaintSource, ...]:
        """Return no taint: the known-users map is deployment-authored config.

        Names and chat ids come from the operator's ``users`` configuration,
        never from a message, so this fragment introduces no external content.
        Declared explicitly rather than omitted so the provider satisfies
        :class:`TaintedContextProvider` and a profile that admits only
        provenance-declaring providers can admit it.
        """
        return ()
