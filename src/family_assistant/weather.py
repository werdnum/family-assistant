"""The household's local weather forecast, from the WillyWeather API.

One ``WeatherService`` serves the whole deployment: the location is configured
by the operator, so every caller shares one cached API response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.config_models import AppConfig
    from family_assistant.utils.clock import Clock

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[weather]"
_WILLYWEATHER_BASE_URL = "https://api.willyweather.com.au/v2"


class WeatherService:
    """Fetches, caches and formats the forecast for the configured location."""

    CACHE_DURATION = timedelta(hours=1)

    def __init__(
        self,
        *,
        location_id: int,
        api_key: str,
        httpx_client: httpx.AsyncClient,
        clock: Clock | None = None,
    ) -> None:
        self._location_id = location_id
        self._api_key = api_key
        self._httpx_client = httpx_client
        self._clock: Clock = clock or SystemClock()
        # ast-grep-ignore: no-dict-any - WillyWeather API response has deeply nested dynamic structure
        self._weather_data_cache: dict[str, Any] | None = None
        self._cache_expiry_time: datetime | None = None

    @classmethod
    def from_config(
        cls, config: AppConfig, httpx_client: httpx.AsyncClient
    ) -> WeatherService | None:
        """The deployment's weather service, or ``None`` when it is not configured."""
        if not is_weather_configured(config):
            return None
        assert config.willyweather_api_key is not None
        assert config.willyweather_location_id is not None
        return cls(
            location_id=config.willyweather_location_id,
            api_key=config.willyweather_api_key.get_secret_value(),
            httpx_client=httpx_client,
        )

    # ast-grep-ignore: no-dict-any - WillyWeather API response has deeply nested dynamic structure
    async def _fetch_and_cache_weather_data(self) -> dict[str, Any] | None:
        """Fetches weather data from WillyWeather API and caches it."""
        now_utc = self._clock.now()
        if (
            self._weather_data_cache
            and self._cache_expiry_time
            and now_utc < self._cache_expiry_time
        ):
            logger.debug(f"{_LOG_PREFIX} Using cached weather data.")
            return self._weather_data_cache

        url = f"{_WILLYWEATHER_BASE_URL}/{self._api_key}/locations/{self._location_id}/weather.json"
        params = {
            "forecasts": "weather,rainfall,sunrisesunset,uv",
            "forecastGraphs": "temperature,rainfallprobability,precis",
            "observational": "true",
            "days": "7",  # For a 7-day outlook (today + 6 more days)
            "units": "temperature:c,speed:km/h,amount:mm,pressure:hpa",
        }
        try:
            logger.info(
                f"{_LOG_PREFIX} Fetching weather data for location {self._location_id}."
            )
            response = await self._httpx_client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            logger.exception(
                f"{_LOG_PREFIX} HTTP error fetching weather data: {e.response.status_code} - {e.response.text}"
            )
            return None
        except httpx.RequestError as e:
            logger.exception(f"{_LOG_PREFIX} Request error fetching weather data: {e}")
            return None
        except Exception as e:
            logger.exception(
                f"{_LOG_PREFIX} Unexpected error fetching or parsing weather data: {e}"
            )
            return None

        if not isinstance(data, dict) or "location" not in data:
            logger.error(
                f"{_LOG_PREFIX} Invalid data structure received from WillyWeather API: {data}"
            )
            return None

        self._weather_data_cache = data
        self._cache_expiry_time = now_utc + self.CACHE_DURATION
        logger.debug(f"{_LOG_PREFIX} Weather data fetched and cached.")
        return data

    async def get_forecast_fragments(
        self,
        timezone: ZoneInfo,
        prompts: Mapping[str, str] | None = None,
    ) -> list[str]:
        """The forecast as text fragments: a header, today in detail, then the week.

        ``timezone`` is the one times and "today" are shown in. ``prompts`` may
        override any of the ``weather_*`` format strings.
        """
        formatter = _ForecastFormatter(prompts=prompts or {}, display_tz=timezone)
        fragments: list[str] = []
        weather_data = await self._fetch_and_cache_weather_data()

        if not weather_data or "location" not in weather_data:
            logger.warning(f"{_LOG_PREFIX} No weather data available or invalid data.")
            no_data_msg = formatter.prompts.get(
                "weather_no_data", "Weather data unavailable."
            )
            if no_data_msg:
                fragments.append(no_data_msg)
            return fragments

        location_name = weather_data.get("location", {}).get("name", "Unknown Location")
        api_tz_str = weather_data.get("location", {}).get("timeZone", "UTC")
        today_date_obj = self._clock.now().astimezone(timezone).date()

        header = formatter.prompts.get(
            "weather_context_header", "Weather for {location_name}:"
        ).format(location_name=location_name)
        fragments.append(header)

        def format_forecast_fragments() -> list[str]:
            forecast_fragments = formatter.format_todays_detailed_forecast(
                weather_data, today_date_obj, api_tz_str
            )
            outlook_header = formatter.prompts.get(
                "weather_outlook_header", "\nOutlook for the week:"
            )
            if outlook_header:
                forecast_fragments.append(outlook_header)
            forecast_fragments.extend(
                formatter.format_weekly_outlook(
                    weather_data, today_date_obj, api_tz_str
                )
            )
            return forecast_fragments

        try:
            forecast_fragments = format_forecast_fragments()
        except Exception as e:
            logger.exception(f"{_LOG_PREFIX} Error formatting weather data: {e}")
            no_data_msg = formatter.prompts.get(
                "weather_formatting_error", "Could not format weather details."
            )
            if no_data_msg:
                fragments = [header, no_data_msg] if header else [no_data_msg]
            else:
                fragments = [header] if header else []
        else:
            fragments.extend(forecast_fragments)

        logger.debug(
            f"{_LOG_PREFIX} Formatted weather data into {len(fragments)} fragment(s)."
        )
        return fragments


def is_weather_configured(config: AppConfig) -> bool:
    """Whether the deployment has a WillyWeather API key and location."""
    return bool(config.willyweather_api_key and config.willyweather_location_id)


@dataclass(frozen=True, slots=True)
class _ForecastFormatter:
    """Renders WillyWeather API data as text in one display timezone."""

    prompts: Mapping[str, str]
    display_tz: ZoneInfo

    def _parse_api_datetime(
        self, dt_str: str | None, api_tz_str: str
    ) -> datetime | None:
        """Parses API datetime string (YYYY-MM-DD HH:MM:SS) from API's timezone."""
        if not dt_str:
            return None
        try:
            naive_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            api_tz = ZoneInfo(api_tz_str)
            return naive_dt.replace(tzinfo=api_tz)
        except (ValueError, KeyError) as e:
            logger.warning(
                f"{_LOG_PREFIX} Error parsing API datetime '{dt_str}' with timezone '{api_tz_str}': {e}"
            )
            return None

    def _format_time(self, dt_obj: datetime | None) -> str:
        """Formats datetime object to HH:MM in display timezone."""
        if not dt_obj:
            return "N/A"
        return dt_obj.astimezone(self.display_tz).strftime("%H:%M")

    # ast-grep-ignore: no-dict-any - WillyWeather API UV data has nested alert/entries structure
    def _format_uv_alert(self, uv_day_data: dict[str, Any], api_tz_str: str) -> str:
        """Formats UV information for a day."""
        alert = uv_day_data.get("alert")
        if alert and alert.get("maxIndex", 0) >= 3:
            start_dt = self._parse_api_datetime(alert.get("startDateTime"), api_tz_str)
            end_dt = self._parse_api_datetime(alert.get("endDateTime"), api_tz_str)
            return self.prompts.get(
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
            return self.prompts.get(
                "weather_uv_simple_format", "Max {index} ({scale})"
            ).format(index=first_entry.get("index"), scale=first_entry.get("scale"))
        return self.prompts.get("weather_no_uv_alert", "Low")

    # ast-grep-ignore: no-dict-any - external weather API JSON with no fixed schema
    def _format_rainfall_summary(self, rainfall_day_entry: dict[str, Any]) -> str:
        """Formats rainfall summary for a day."""
        prob = rainfall_day_entry.get("probability", 0)
        start_range = rainfall_day_entry.get("startRange")
        end_range = rainfall_day_entry.get("endRange")

        if start_range is not None and end_range is not None:
            amount_range = f"{start_range}-{end_range}"
            return self.prompts.get(
                "weather_rain_amount_probability",
                "{amount_range}mm, {probability}% chance",
            ).format(amount_range=amount_range, probability=prob)
        elif end_range is not None:  # e.g. <1mm
            amount_range = f"<{end_range}"
            return self.prompts.get(
                "weather_rain_amount_probability",
                "{amount_range}mm, {probability}% chance",
            ).format(amount_range=amount_range, probability=prob)
        elif prob > 0:
            return self.prompts.get(
                "weather_rain_probability_only", "{probability}% chance"
            ).format(probability=prob)
        return self.prompts.get(
            "weather_rain_no_significant", "Little to no rain ({probability}% chance)"
        ).format(probability=prob)

    def _format_daily_weather_summary(
        self,
        # ast-grep-ignore: no-dict-any - WillyWeather API weather entry with precis/min/max fields
        day_weather_entry: dict[str, Any],
        # ast-grep-ignore: no-dict-any - WillyWeather API rainfall entry with probability/range fields
        day_rainfall_entry: dict[str, Any],
        # ast-grep-ignore: no-dict-any - WillyWeather API sun/UV data with nested sunrisesunset and uv entries
        day_sun_uv_data: dict[str, Any],
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

        summary_format = self.prompts.get(
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

    def format_todays_detailed_forecast(
        self,
        # ast-grep-ignore: no-dict-any - WillyWeather API response with deeply nested forecast/graph/observational data
        weather_data: dict[str, Any],
        today_date_obj: date,
        api_tz_str: str,
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
            forecasts
            .get("weather", {})
            .get("days", [{}])[0]
            .get("entries", [{}])[0]
            .get("precis", "N/A")
        )  # Fallback to forecast precis

        wind_obs = obs.get("wind", {})
        wind_speed = wind_obs.get("speed", "N/A")
        wind_dir = wind_obs.get("directionText", "N/A")

        current_conditions_str = self.prompts.get(
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
        today_intro_format = self.prompts.get(
            "weather_today_intro", "Today ({date_str}, {day_name}):"
        )
        fragments.append(
            f"{today_intro_format.format(date_str=today_date_obj.strftime('%b %d'), day_name=today_date_obj.strftime('%A'))} {today_summary_str.split(': ', 1)[1]}"
        )

        # Rain Timing
        rain_prob_graph = (
            graphs
            .get("rainfallprobability", {})
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
                        point_time_unix, tz=ZoneInfo(api_tz_str)
                    )
                    rain_periods.append(f"{self._format_time(dt_obj)} ({point_prob}%)")
            if rain_periods:
                fragments.append(
                    self.prompts.get(
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
                morning_temp, afternoon_temp, evening_temp = "N/A", "N/A", "N/A"
                for p in points:
                    dt = datetime.fromtimestamp(
                        p["x"], tz=ZoneInfo(api_tz_str)
                    ).astimezone(self.display_tz)
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
                        self.prompts.get(
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
                dt_obj = datetime.fromtimestamp(point.get("x"), tz=ZoneInfo(api_tz_str))
                precis_code = point.get("precisCode")
                if precis_code != last_precis:
                    condition_changes.append(
                        f"{self._format_time(dt_obj)}: {precis_code.replace('-', ' ')}"
                    )
                    last_precis = precis_code
            if condition_changes:
                fragments.append(
                    self.prompts.get(
                        "weather_today_condition_changes",
                        "Conditions: {changes_summary}.",
                    ).format(
                        changes_summary=" -> ".join(condition_changes[:3])
                    )  # Limit for brevity
                )
        return fragments

    def format_weekly_outlook(
        self,
        # ast-grep-ignore: no-dict-any - WillyWeather API response with deeply nested forecast data
        weather_data: dict[str, Any],
        today_date_obj: date,
        api_tz_str: str,
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
