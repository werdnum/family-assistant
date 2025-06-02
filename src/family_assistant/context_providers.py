import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import httpx
import pytz

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

try:
    import homeassistant_api
except ImportError:
    homeassistant_api = None  # type: ignore[assignment]
    logger.info(
        "homeassistant_api library not found. HomeAssistantContextProvider will not be available."
    )


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


class HomeAssistantContextProvider(ContextProvider):
    """Provides context by rendering a Jinja2 template via Home Assistant."""

    def __init__(
        self,
        api_url: str,
        token: str,
        context_template: str,
        prompts: PromptsType,
        verify_ssl: bool = True,
    ) -> None:
        """
        Initializes the HomeAssistantContextProvider.

        Args:
            api_url: The base URL of the Home Assistant API (e.g., "http://localhost:8123").
            token: The long-lived access token for Home Assistant.
            context_template: The Jinja2 template string to render.
            prompts: A dictionary containing prompt templates for formatting headers/errors.
            verify_ssl: Whether to verify SSL certificates for the API connection.
            # client_kwargs: Additional keyword arguments for homeassistant_api.Client.
        """
        self._api_url = api_url
        self._token = token
        self._context_template = context_template
        self._prompts = prompts
        self._verify_ssl = verify_ssl

        if homeassistant_api is None:
            raise ImportError(
                "homeassistant_api library is not installed. "
                "HomeAssistantContextProvider cannot be used."
            )

        # The homeassistant_api.Client expects the URL to include /api
        ha_api_url_with_path = self._api_url.rstrip("/") + "/api"
        self._ha_client = homeassistant_api.Client(
            api_url=ha_api_url_with_path,
            token=self._token,
            use_async=True,  # Important for async usage
            verify_ssl=self._verify_ssl,
            # **self._client_kwargs, # For future use
        )
        logger.info(
            f"HomeAssistantContextProvider initialized for URL: {ha_api_url_with_path}"
        )

    @property
    def name(self) -> str:
        return "home_assistant"

    async def get_context_fragments(self) -> list[str]:
        """
        Asynchronously retrieves and formats context by rendering a template
        via the Home Assistant API.
        """
        fragments: list[str] = []
        if not self._context_template:
            logger.warning(f"[{self.name}] No context template configured.")
            return []

        if (
            homeassistant_api is None
        ):  # Should have been caught in __init__, but defensive
            logger.error(f"[{self.name}] homeassistant_api library not available.")
            return []

        try:
            logger.debug(
                f"[{self.name}] Rendering template from Home Assistant: '{self._context_template[:100]}...'"
            )
            rendered_template = await self._ha_client.async_get_rendered_template(
                template=self._context_template
            )

            if rendered_template and rendered_template.strip():
                header = self._prompts.get("home_assistant_context_header", "").strip()
                # Only add header if it's not empty
                full_context = (
                    f"{header}\n{rendered_template.strip()}"
                    if header
                    else rendered_template.strip()
                )
                fragments.append(full_context.strip())
                logger.debug(
                    f"[{self.name}] Successfully rendered Home Assistant template."
                )
            else:
                logger.info(
                    f"[{self.name}] Rendered Home Assistant template was empty or whitespace only."
                )
                empty_message = self._prompts.get(
                    "home_assistant_template_empty", ""
                ).strip()
                if empty_message:
                    fragments.append(empty_message)

        except (
            homeassistant_api.errors.ApiError
        ) as ha_api_err:  # Specific error for HA API issues
            logger.error(
                f"[{self.name}] Home Assistant API error: {ha_api_err}", exc_info=True
            )
            error_message = self._prompts.get(
                "home_assistant_api_error", "Error retrieving data from Home Assistant."
            ).strip()
            if error_message:
                fragments.append(error_message)
        except Exception as e:  # Catch other potential errors (network, etc.)
            logger.error(
                f"[{self.name}] Error rendering Home Assistant template: {e}",
                exc_info=True,
            )
            error_message = self._prompts.get(
                "home_assistant_api_error", "Error retrieving data from Home Assistant."
            ).strip()
            if error_message:
                fragments.append(error_message)

        return fragments


# --- BEGIN WeatherContextProvider ---
# Add necessary imports for WeatherContextProvider


class WeatherContextProvider(ContextProvider):
    """Provides context from WillyWeather API."""

    _CACHE_DURATION = timedelta(hours=1)

    def __init__(
        self,
        location_id: int,
        api_key: str,
        prompts: PromptsType,
        timezone_str: str,  # Target display timezone
        httpx_client: httpx.AsyncClient,
    ) -> None:
        """
        Initializes the WeatherContextProvider.

        Args:
            location_id: The WillyWeather location ID.
            api_key: The WillyWeather API key.
            prompts: A dictionary containing prompt templates for formatting.
            timezone_str: The local timezone string for display (e.g., "Europe/London").
            httpx_client: An instance of httpx.AsyncClient for making API calls.
        """
        self._location_id = location_id
        self._api_key = api_key
        self._prompts = prompts
        self._display_tz_str = timezone_str
        try:
            self._display_pytz_tz = pytz.timezone(timezone_str)
        except pytz.exceptions.UnknownTimeZoneError:
            logger.error(
                f"Unknown display timezone: {timezone_str}. Defaulting to UTC."
            )
            self._display_pytz_tz = pytz.utc
            self._display_tz_str = "UTC"

        self._httpx_client = httpx_client
        self._weather_data_cache: dict[str, Any] | None = None
        self._cache_expiry_time: datetime | None = None

    @property
    def name(self) -> str:
        return "weather"

    def _get_today_date(self) -> date:
        """Gets today's date in the display timezone."""
        return datetime.now(self._display_pytz_tz).date()

    async def _fetch_and_cache_weather_data(self) -> dict[str, Any] | None:
        """Fetches weather data from WillyWeather API and caches it."""
        now_utc = datetime.now(pytz.utc)
        if (
            self._weather_data_cache
            and self._cache_expiry_time
            and now_utc < self._cache_expiry_time
        ):
            logger.debug(f"[{self.name}] Using cached weather data.")
            return self._weather_data_cache

        if not self._api_key:
            logger.error(f"[{self.name}] WillyWeather API key not configured.")
            return None
        if not self._location_id:
            logger.error(f"[{self.name}] WillyWeather location ID not configured.")
            return None

        url = f"https://api.willyweather.com.au/v2/{self._api_key}/locations/{self._location_id}/weather.json"
        params = {
            "forecasts": "weather,rainfall,sunrisesunset,uv",
            "forecastGraphs": "temperature,rainfallprobability,precis",
            "observational": "true",
            "days": "7",  # For a 7-day outlook (today + 6 more days)
            "units": "temperature:c,speed:km/h,amount:mm,pressure:hpa",
        }
        try:
            logger.info(
                f"[{self.name}] Fetching weather data for location {self._location_id}."
            )
            response = await self._httpx_client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            # Basic validation
            if not isinstance(data, dict) or "location" not in data:
                logger.error(
                    f"[{self.name}] Invalid data structure received from WillyWeather API: {data}"
                )
                return None

            self._weather_data_cache = data
            self._cache_expiry_time = now_utc + self._CACHE_DURATION
            logger.debug(f"[{self.name}] Weather data fetched and cached.")
            return data
        except httpx.HTTPStatusError as e:
            logger.error(
                f"[{self.name}] HTTP error fetching weather data: {e.response.status_code} - {e.response.text}",
                exc_info=True,
            )
        except httpx.RequestError as e:
            logger.error(
                f"[{self.name}] Request error fetching weather data: {e}", exc_info=True
            )
        except Exception as e:
            logger.error(
                f"[{self.name}] Unexpected error fetching or parsing weather data: {e}",
                exc_info=True,
            )
        return None

    def _parse_api_datetime(
        self, dt_str: str | None, api_tz_str: str
    ) -> datetime | None:
        """Parses API datetime string (YYYY-MM-DD HH:MM:SS) from API's timezone."""
        if not dt_str:
            return None
        try:
            naive_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            api_pytz_tz = pytz.timezone(api_tz_str)
            return api_pytz_tz.localize(naive_dt)
        except (ValueError, pytz.exceptions.UnknownTimeZoneError) as e:
            logger.warning(
                f"[{self.name}] Error parsing API datetime '{dt_str}' with timezone '{api_tz_str}': {e}"
            )
            return None

    def _format_time(self, dt_obj: datetime | None) -> str:
        """Formats datetime object to HH:MM in display timezone."""
        if not dt_obj:
            return "N/A"
        return dt_obj.astimezone(self._display_pytz_tz).strftime("%H:%M")

    def _format_uv_alert(self, uv_day_data: dict[str, Any], api_tz_str: str) -> str:
        """Formats UV information for a day."""
        alert = uv_day_data.get("alert")
        if alert and alert.get("maxIndex", 0) >= 3:
            start_dt = self._parse_api_datetime(alert.get("startDateTime"), api_tz_str)
            end_dt = self._parse_api_datetime(alert.get("endDateTime"), api_tz_str)
            return self._prompts.get(
                "weather_uv_alert_format",
                "Max {maxIndex} ({scale}) from {start_time} to {end_time}",
            ).format(
                maxIndex=alert.get("maxIndex"),
                scale=alert.get("scale", "N/A"),
                start_time=self._format_time(start_dt),
                end_time=self._format_time(end_dt),
            )
        # Fallback to the first entry if no alert or low UV
        first_entry = uv_day_data.get("entries", [{}])[0]
        if first_entry.get("index") is not None:
            return self._prompts.get(
                "weather_uv_simple_format", "Max {index} ({scale})"
            ).format(index=first_entry.get("index"), scale=first_entry.get("scale"))
        return self._prompts.get("weather_no_uv_alert", "Low")

    def _format_rainfall_summary(self, rainfall_day_entry: dict[str, Any]) -> str:
        """Formats rainfall summary for a day."""
        prob = rainfall_day_entry.get("probability", 0)
        start_range = rainfall_day_entry.get("startRange")
        end_range = rainfall_day_entry.get("endRange")

        if start_range is not None and end_range is not None:
            amount_range = f"{start_range}-{end_range}"
            return self._prompts.get(
                "weather_rain_amount_probability",
                "{amount_range}mm, {probability}% chance",
            ).format(amount_range=amount_range, probability=prob)
        elif end_range is not None:  # e.g. <1mm
            amount_range = f"<{end_range}"
            return self._prompts.get(
                "weather_rain_amount_probability",
                "{amount_range}mm, {probability}% chance",
            ).format(amount_range=amount_range, probability=prob)
        elif prob > 0:
            return self._prompts.get(
                "weather_rain_probability_only", "{probability}% chance"
            ).format(probability=prob)
        return self._prompts.get(
            "weather_rain_no_significant", "Little to no rain ({probability}% chance)"
        ).format(probability=prob)

    def _format_daily_weather_summary(
        self,
        day_weather_entry: dict[str, Any],
        day_rainfall_entry: dict[str, Any],
        day_sun_uv_data: dict[str, Any],  # Contains 'sunrisesunset' and 'uv' day data
        day_date_obj: date,
        api_tz_str: str,
    ) -> str:
        """Formats a concise summary for a single day (used for today and outlook)."""
        precis = day_weather_entry.get("precis", "N/A")
        min_temp = day_weather_entry.get("min", "N/A")
        max_temp = day_weather_entry.get("max", "N/A")

        rain_info = self._format_rainfall_summary(day_rainfall_entry)

        sun_entry = day_sun_uv_data.get("sunrisesunset", {}).get("entries", [{}])[0]
        sunrise_dt = self._parse_api_datetime(sun_entry.get("riseDateTime"), api_tz_str)
        sunset_dt = self._parse_api_datetime(sun_entry.get("setDateTime"), api_tz_str)
        sunrise_time = self._format_time(sunrise_dt)
        sunset_time = self._format_time(sunset_dt)

        uv_info = self._format_uv_alert(day_sun_uv_data.get("uv", {}), api_tz_str)

        day_name = day_date_obj.strftime("%A")
        date_str = day_date_obj.strftime("%b %d")

        summary_format = self._prompts.get(
            "weather_day_summary",
            "{day_name} ({date_str}): {precis}. High: {max_temp}°C, Low: {min_temp}°C. Rain: {rain_info}. Sun: {sunrise_time}-{sunset_time}. UV: {uv_info}.",
        )
        return summary_format.format(
            day_name=day_name,
            date_str=date_str,
            precis=precis,
            max_temp=max_temp,
            min_temp=min_temp,
            rain_info=rain_info,
            sunrise_time=sunrise_time,
            sunset_time=sunset_time,
            uv_info=uv_info,
        )

    def _format_todays_detailed_forecast(
        self, weather_data: dict[str, Any], today_date_obj: date, api_tz_str: str
    ) -> list[str]:
        """Formats a detailed forecast for today."""
        fragments: list[str] = []
        obs = weather_data.get("observational", {}).get("observations", {})
        forecasts = weather_data.get("forecasts", {})
        graphs = weather_data.get("forecastGraphs", {})

        # Current Conditions
        current_temp = obs.get("temperature", {}).get("temperature", "N/A")
        apparent_temp = obs.get("temperature", {}).get("apparentTemperature", "N/A")
        current_precis = (
            forecasts.get("weather", {})
            .get("days", [{}])[0]
            .get("entries", [{}])[0]
            .get("precis", "N/A")
        )  # Fallback to forecast precis

        wind_obs = obs.get("wind", {})
        wind_speed = wind_obs.get("speed", "N/A")
        wind_dir = wind_obs.get("directionText", "N/A")

        current_conditions_str = self._prompts.get(
            "weather_current_conditions",
            "Now: {temp}°C (feels like {apparent_temp}°C), {conditions}. Wind: {wind_speed} km/h {wind_dir}.",
        ).format(
            temp=current_temp,
            apparent_temp=apparent_temp,
            conditions=current_precis,
            wind_speed=wind_speed,
            wind_dir=wind_dir,
        )
        fragments.append(current_conditions_str)

        # Today's Summary (using the daily formatter)
        today_weather_day = forecasts.get("weather", {}).get("days", [{}])[0]
        today_rainfall_day = forecasts.get("rainfall", {}).get("days", [{}])[0]
        # For sun_uv_data, we need to combine relevant parts for the daily formatter
        today_sun_uv_data = {
            "sunrisesunset": forecasts.get("sunrisesunset", {}).get("days", [{}])[0],
            "uv": forecasts.get("uv", {}).get("days", [{}])[0],
        }

        today_summary_str = self._format_daily_weather_summary(
            today_weather_day.get("entries", [{}])[0],
            today_rainfall_day.get("entries", [{}])[0],
            today_sun_uv_data,
            today_date_obj,
            api_tz_str,
        )
        # Prepend "Today:" or similar to the summary
        today_intro_format = self._prompts.get(
            "weather_today_intro", "Today ({date_str}, {day_name}):"
        )
        fragments.append(
            f"{today_intro_format.format(date_str=today_date_obj.strftime('%b %d'), day_name=today_date_obj.strftime('%A'))} {today_summary_str.split(': ', 1)[1]}"
        )

        # Rain Timing
        rain_prob_graph = (
            graphs.get("rainfallprobability", {})
            .get("dataConfig", {})
            .get("series", {})
        )
        if rain_prob_graph.get("groups"):
            rain_periods = []
            # Assuming groups are for today
            for point in rain_prob_graph["groups"][0].get("points", []):
                point_time_unix = point.get("x")
                point_prob = point.get("y")
                if (
                    point_time_unix and point_prob is not None and point_prob > 20
                ):  # Threshold for "significant"
                    dt_obj = datetime.fromtimestamp(
                        point_time_unix, tz=pytz.timezone(api_tz_str)
                    )
                    rain_periods.append(f"{self._format_time(dt_obj)} ({point_prob}%)")
            if rain_periods:
                fragments.append(
                    self._prompts.get(
                        "weather_today_rain_periods", "Rain likely: {periods_details}."
                    ).format(periods_details=", ".join(rain_periods))
                )

        # Temperature Curve (Simplified)
        temp_graph = (
            graphs.get("temperature", {}).get("dataConfig", {}).get("series", {})
        )
        if temp_graph.get("groups"):
            # Assuming groups are for today
            points = temp_graph["groups"][0].get("points", [])
            if points:
                # Simple: Morning (around 9am), Afternoon (around 2pm), Evening (around 7pm)
                # This needs more robust logic to find actual min/max or specific times
                morning_temp, afternoon_temp, evening_temp = "N/A", "N/A", "N/A"
                for p in points:
                    dt = datetime.fromtimestamp(
                        p["x"], tz=pytz.timezone(api_tz_str)
                    ).astimezone(self._display_pytz_tz)
                    if dt.hour >= 8 and dt.hour <= 10:
                        morning_temp = p["y"]
                    if dt.hour >= 13 and dt.hour <= 15:
                        afternoon_temp = p["y"]
                    if dt.hour >= 18 and dt.hour <= 20:
                        evening_temp = p["y"]
                if (
                    morning_temp != "N/A"
                    or afternoon_temp != "N/A"
                    or evening_temp != "N/A"
                ):
                    fragments.append(
                        self._prompts.get(
                            "weather_today_temp_curve",
                            "Temps: Morning {morning_temp}°C, Afternoon {afternoon_temp}°C, Evening {evening_temp}°C.",
                        ).format(
                            morning_temp=morning_temp,
                            afternoon_temp=afternoon_temp,
                            evening_temp=evening_temp,
                        )
                    )

        # Condition Changes
        precis_graph = graphs.get("precis", {}).get("dataConfig", {}).get("series", {})
        if precis_graph.get("groups"):
            # Assuming groups are for today
            condition_changes = []
            last_precis = None
            for point in precis_graph["groups"][0].get("points", []):
                dt_obj = datetime.fromtimestamp(
                    point.get("x"), tz=pytz.timezone(api_tz_str)
                )
                precis_code = point.get(
                    "precisCode"
                )  # You might need a mapping from precisCode to readable text
                if precis_code != last_precis:
                    condition_changes.append(
                        f"{self._format_time(dt_obj)}: {precis_code.replace('-', ' ')}"
                    )
                    last_precis = precis_code
            if condition_changes:
                fragments.append(
                    self._prompts.get(
                        "weather_today_condition_changes",
                        "Conditions: {changes_summary}.",
                    ).format(
                        changes_summary=" -> ".join(condition_changes[:3])
                    )  # Limit for brevity
                )
        return fragments

    def _format_weekly_outlook(
        self, weather_data: dict[str, Any], today_date_obj: date, api_tz_str: str
    ) -> list[str]:
        """Formats a summarized weather outlook for the next 6 days."""
        fragments: list[str] = []
        forecasts = weather_data.get("forecasts", {})
        weather_days = forecasts.get("weather", {}).get("days", [])
        rainfall_days = forecasts.get("rainfall", {}).get("days", [])
        sunrisesunset_days = forecasts.get("sunrisesunset", {}).get("days", [])
        uv_days = forecasts.get("uv", {}).get("days", [])

        # Ensure all forecast types have enough data
        min_len = min(
            len(weather_days), len(rainfall_days), len(sunrisesunset_days), len(uv_days)
        )

        for i in range(1, min(7, min_len)):  # Iterate from tomorrow up to 6 more days
            day_date_obj = today_date_obj + timedelta(days=i)

            weather_day_entry = weather_days[i].get("entries", [{}])[0]
            rainfall_day_entry = rainfall_days[i].get("entries", [{}])[0]

            # Combine sun and uv data for the day
            sun_uv_day_data = {"sunrisesunset": sunrisesunset_days[i], "uv": uv_days[i]}

            day_summary = self._format_daily_weather_summary(
                weather_day_entry,
                rainfall_day_entry,
                sun_uv_day_data,
                day_date_obj,
                api_tz_str,
            )
            fragments.append(day_summary)
        return fragments

    async def get_context_fragments(self) -> list[str]:
        """Asynchronously retrieves and formats weather context fragments."""
        fragments: list[str] = []
        weather_data = await self._fetch_and_cache_weather_data()

        if not weather_data or "location" not in weather_data:
            logger.warning(f"[{self.name}] No weather data available or invalid data.")
            no_data_msg = self._prompts.get(
                "weather_no_data", "Weather data unavailable."
            )
            if no_data_msg:  # Only add if defined and non-empty
                fragments.append(no_data_msg)
            return fragments

        location_name = weather_data.get("location", {}).get("name", "Unknown Location")
        api_tz_str = weather_data.get("location", {}).get("timeZone", "UTC")
        today_date_obj = self._get_today_date()

        header = self._prompts.get(
            "weather_context_header", "Weather for {location_name}:"
        ).format(location_name=location_name)
        fragments.append(header)

        try:
            # Detailed forecast for today
            today_details = self._format_todays_detailed_forecast(
                weather_data, today_date_obj, api_tz_str
            )
            fragments.extend(today_details)

            # Outlook for the rest of the week
            outlook_header = self._prompts.get(
                "weather_outlook_header", "\nOutlook for the week:"
            )
            if outlook_header:
                fragments.append(outlook_header)

            weekly_outlook = self._format_weekly_outlook(
                weather_data, today_date_obj, api_tz_str
            )
            fragments.extend(weekly_outlook)

        except Exception as e:
            logger.error(
                f"[{self.name}] Error formatting weather data: {e}", exc_info=True
            )
            # Fallback to a simpler message if formatting fails
            no_data_msg = self._prompts.get(
                "weather_formatting_error", "Could not format weather details."
            )
            if no_data_msg:
                # Clear potentially partial fragments and add error message
                fragments = [header, no_data_msg] if header else [no_data_msg]
            else:
                fragments = [header] if header else []

        logger.debug(
            f"[{self.name}] Formatted weather data into {len(fragments)} fragment(s)."
        )
        return fragments


# --- END WeatherContextProvider ---


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
