"""The household's local weather forecast as a tool."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.metadata import ToolRegistration
    from family_assistant.tools.types import ToolExecutionContext
    from family_assistant.weather import WeatherService

GET_WEATHER_FORECAST_TOOL_NAME = "get_weather_forecast"

WEATHER_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": GET_WEATHER_FORECAST_TOOL_NAME,
            "description": (
                "Gets the weather forecast for the household's own location, which "
                "the operator configures; it cannot look up any other place. Call it "
                "whenever the weather matters to the request -- what to wear, whether "
                "an outdoor plan will be rained out, laundry, a morning briefing.\n\n"
                "Returns: text with current conditions (temperature, apparent "
                "temperature, wind), today's forecast in detail (high and low, rain "
                "chance and likely rain times, temperatures through the day, "
                "condition changes, sunrise and sunset, UV), then a one-line outlook "
                "for each of the next six days. Times are in the household's "
                "timezone. Forecast data is refreshed at most hourly. If the forecast "
                "cannot be fetched, says so."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


async def get_weather_forecast_tool(exec_context: ToolExecutionContext) -> ToolResult:
    """The registered implementation before a weather service is bound to it.

    ``bind_weather_service`` replaces it in every deployment that configures
    weather; one that does not withholds the tool, so reaching this means the
    root tool registry was built without the service.
    """
    _ = exec_context
    return ToolResult(
        text="Error: The weather forecast is not available in this deployment."
    )


def bind_weather_service(
    registration: ToolRegistration, weather_service: WeatherService
) -> ToolRegistration:
    """The ``get_weather_forecast`` registration, served by ``weather_service``."""

    async def get_weather_forecast(exec_context: ToolExecutionContext) -> ToolResult:
        fragments = await weather_service.get_forecast_fragments(exec_context.timezone)
        return ToolResult(text="\n".join(fragments))

    return dataclasses.replace(registration, implementation=get_weather_forecast)
