"""Rebuilding the household's Home Assistant from recorded tool results and events."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from family_assistant.eval.workhorse.ha_world import build_home_world
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.types import MessageHistoryRow

pytestmark = pytest.mark.no_db

_T = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

_LISTING = """Found 3 matching entities:

- light.desk_lamp - Desk Lamp (Area: Study) [Device: Desk Lamp]
- sensor.desk_lamp_power - Desk Lamp Power [Device: Desk Lamp]
- binary_sensor.back_door - Back Door"""


def _row(
    index: int,
    role: str,
    at: datetime,
    *,
    content: str | None = None,
    tool_calls: list[ToolCallItem] | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> MessageHistoryRow:
    return MessageHistoryRow(
        internal_id=index,
        interface_type="web",
        conversation_id="c1",
        interface_message_id=None,
        turn_id="t1",
        thread_root_id=None,
        timestamp=at,
        role=role,
        content=content,
        tool_calls=tool_calls,
        reasoning_info=None,
        tool_call_id=tool_call_id,
        error_traceback=None,
        processing_profile_id="default_assistant",
        subconversation_id=None,
        user_id="andrew",
        attachments=None,
        tool_name=tool_name,
        provider_metadata=None,
        taint_metadata_json=None,
        taint_metadata_version=None,
        taint_metadata=None,
        is_internal=False,
        echoed_from_conversation_id=None,
        moved_to_conversation_id=None,
    )


def _history(
    calls: list[tuple[datetime, str, dict[str, object], str]],
) -> dict[tuple[str, str], list[MessageHistoryRow]]:
    rows: list[MessageHistoryRow] = []
    for index, (at, name, arguments, result) in enumerate(calls):
        call = ToolCallItem(
            id=f"call-{index}",
            type="function",
            function=ToolCallFunction(name=name, arguments=arguments),
        )
        rows.append(_row(index * 2, "assistant", at, tool_calls=[call]))
        rows.append(
            _row(
                index * 2 + 1,
                "tool",
                at,
                content=result,
                tool_call_id=call.id,
                tool_name=name,
            )
        )
    return {("web", "c1"): rows}


def _state_changed(
    entity_id: str,
    state: str,
    at: datetime,
    attributes: dict[str, object],
) -> dict[str, object]:
    return {
        "timestamp": at,
        "event_data": {
            "event_type": "state_changed",
            "entity_id": entity_id,
            "new_state": {
                "state": state,
                "attributes": attributes,
                "last_changed": at.isoformat(),
            },
            "old_state": None,
        },
    }


def test_listing_gives_names_devices_and_areas() -> None:
    world = build_home_world(
        _history([(_T, "list_home_assistant_entities", {}, _LISTING)]), []
    )

    lamp = world.entity("light.desk_lamp")
    assert lamp is not None
    assert lamp.name == "Desk Lamp"
    assert lamp.device == "Desk Lamp"
    devices = {d.key: d for d in world.devices}
    # The device sits in the area its entities are listed in; the entity no
    # longer needs an area of its own.
    assert devices["Desk Lamp"].area == "Study"
    assert lamp.area is None
    assert {a.area_id: a.name for a in world.areas} == {"study": "Study"}


def test_an_unobserved_entity_gets_a_resting_state_marked_world_built() -> None:
    world = build_home_world(
        _history([(_T, "list_home_assistant_entities", {}, _LISTING)]), []
    )

    door = world.entity("binary_sensor.back_door")
    assert door is not None
    served = door.as_of(_T)
    assert served.state == "off"
    assert served.world_built
    assert served.attributes == {"friendly_name": "Back Door"}
    assert world.summary()["world_built_state"] == 3


def test_state_as_of_a_moment_is_latest_before_else_earliest_after() -> None:
    events = [
        _state_changed("climate.lounge", "heat", _T, {"temperature": 21}),
        _state_changed(
            "climate.lounge", "off", _T + timedelta(hours=2), {"temperature": 21}
        ),
    ]
    world = build_home_world({}, events)
    lounge = world.entity("climate.lounge")
    assert lounge is not None

    assert lounge.as_of(_T + timedelta(hours=1)).state == "heat"
    assert lounge.as_of(_T + timedelta(hours=3)).state == "off"
    early = lounge.as_of(_T - timedelta(days=1))
    assert early.state == "heat"
    assert not early.world_built
    assert early.attributes == {"temperature": 21}


def test_partial_attributes_overlay_the_last_full_set() -> None:
    events = [_state_changed("light.desk_lamp", "on", _T, {"brightness": 100})]
    template = "{{ state_attr('light.desk_lamp', 'brightness') }}"
    world = build_home_world(
        _history([
            (
                _T + timedelta(hours=1),
                "render_home_assistant_template",
                {"template": template},
                "200",
            )
        ]),
        events,
    )
    lamp = world.entity("light.desk_lamp")
    assert lamp is not None

    assert lamp.as_of(_T + timedelta(minutes=30)).attributes["brightness"] == 100
    assert lamp.as_of(_T + timedelta(hours=2)).attributes["brightness"] == 200


def test_straight_line_templates_yield_states_and_registry_facts() -> None:
    template = (
        "Pool pump: {{ states('switch.pool_pump') }}\n"
        "WLED: {{ integration_entities('wled') }}\n"
        "Garage: {{ area_entities('Garage') }}"
    )
    output = (
        "Pool pump: on\n"
        "WLED: ['light.garage_strip']\n"
        "Garage: ['light.garage_strip', 'sensor.garage_humidity']"
    )
    world = build_home_world(
        _history([
            (_T, "render_home_assistant_template", {"template": template}, output)
        ]),
        [],
    )

    pump = world.entity("switch.pool_pump")
    assert pump is not None
    assert pump.as_of(_T).state == "on"
    strip = world.entity("light.garage_strip")
    assert strip is not None
    assert strip.integration == "wled"
    assert strip.area == "Garage"
    humidity = world.entity("sensor.garage_humidity")
    assert humidity is not None
    assert humidity.area == "Garage"


def test_loop_templates_yield_a_row_per_entity() -> None:
    template = (
        "{% set ns = namespace(res=[]) %}{% for s in states %}"
        "{% set ns.res = ns.res + [s.entity_id ~ ' | ' ~ s.name ~ ' | ' ~ s.state] %}"
        "{% endfor %}{{ ns.res | join('\\n') }}"
    )
    output = "sensor.printer | Printer | finished\ncamera.printer | Printer Cam | idle"
    world = build_home_world(
        _history([
            (_T, "render_home_assistant_template", {"template": template}, output)
        ]),
        [],
    )

    printer = world.entity("sensor.printer")
    camera = world.entity("camera.printer")
    assert printer is not None
    assert camera is not None
    assert printer.as_of(_T).state == "finished"
    assert camera.name == "Printer Cam"


def test_ids_only_ever_rendering_unknown_are_not_entities() -> None:
    template = "{{ states('vacuum.guessed_name') }}"
    world = build_home_world(
        _history([
            (_T, "render_home_assistant_template", {"template": template}, "unknown")
        ]),
        [],
    )

    assert world.entity("vacuum.guessed_name") is None


def test_json_results_describe_entities_but_config_references_do_not() -> None:
    state = {
        "data": {
            "entity_id": "vacuum.robot",
            "state": "docked",
            "attributes": {"fan_speed": "balanced", "friendly_name": "Robot"},
            "last_changed": _T.isoformat(),
        }
    }
    trace = {
        "success": True,
        "config": {
            "conditions": [
                {"condition": "state", "entity_id": "light.renamed", "state": "on"}
            ]
        },
    }
    world = build_home_world(
        _history([
            (
                _T,
                "ha_call_read_tool",
                {"name": "ha_get_state", "arguments": {"entity_id": "vacuum.robot"}},
                json.dumps(state),
            ),
            (
                _T,
                "ha_call_read_tool",
                {"name": "ha_get_automation_traces", "arguments": {}},
                json.dumps(trace),
            ),
        ]),
        [],
    )

    robot = world.entity("vacuum.robot")
    assert robot is not None
    served = robot.as_of(_T)
    assert served.state == "docked"
    assert served.attributes["fan_speed"] == "balanced"
    assert world.entity("light.renamed") is None


def test_action_results_record_state_changes_and_forecasts() -> None:
    forecast = {
        "weather.home": {
            "forecast": [
                {"datetime": "2026-10-02T00:00:00", "condition": "rainy"},
                {"datetime": "2026-10-03T00:00:00", "condition": "sunny"},
            ]
        }
    }
    world = build_home_world(
        _history([
            (
                _T,
                "call_home_assistant_action",
                {"domain": "light", "action": "turn_on", "service_data": {}},
                "Called light.turn_on; 1 state change(s): light.desk_lamp=on",
            ),
            (
                _T,
                "call_home_assistant_action",
                {
                    "domain": "weather",
                    "action": "get_forecasts",
                    "service_data": {"entity_id": "weather.home", "type": "daily"},
                    "return_response": True,
                },
                "Called weather.get_forecasts; no state changes reported; "
                f"response={json.dumps(forecast)}",
            ),
            (
                _T,
                "call_home_assistant_action",
                {"domain": "notify", "action": "family_phone", "service_data": {}},
                "Called notify.family_phone; no state changes reported",
            ),
        ]),
        [],
    )

    lamp = world.entity("light.desk_lamp")
    assert lamp is not None
    assert lamp.as_of(_T).state == "on"
    assert "notify.family_phone" in world.services
    snapshot = world.as_of(_T + timedelta(days=5))
    [served] = snapshot.forecasts
    assert served.redated
    assert served.forecast[0]["datetime"] == "2026-10-07T00:00:00"


def test_recorded_automation_configs_are_kept_by_entity() -> None:
    config = {"id": "123", "alias": "Lights at dusk", "triggers": [], "actions": []}
    world = build_home_world(
        _history([
            (
                _T,
                "ha_config_get_automation",
                {"identifier": "automation.lights_at_dusk"},
                json.dumps({
                    "success": True,
                    "automation_id": "automation.lights_at_dusk",
                    "config": config,
                }),
            )
        ]),
        [],
    )

    [automation] = world.as_of(_T).automations
    assert automation.entity_id == "automation.lights_at_dusk"
    assert automation.config["alias"] == "Lights at dusk"
    entity = world.entity("automation.lights_at_dusk")
    assert entity is not None
    assert entity.name == "Lights at dusk"


def test_history_holds_distinct_states_up_to_the_moment() -> None:
    events = [
        _state_changed("switch.pump", "on", _T, {}),
        _state_changed("switch.pump", "on", _T + timedelta(minutes=5), {}),
        _state_changed("switch.pump", "off", _T + timedelta(hours=1), {}),
        _state_changed("switch.pump", "on", _T + timedelta(hours=5), {}),
    ]
    world = build_home_world({}, events)

    history = world.as_of(_T + timedelta(hours=2)).history["switch.pump"]
    assert [(p.at, p.state) for p in history] == [
        (_T, "on"),
        (_T + timedelta(hours=1), "off"),
    ]


def test_rendered_enum_members_are_read_as_their_values() -> None:
    events = [_state_changed("climate.lounge", "heat", _T, {"hvac_modes": ["heat"]})]
    template = "{{ state_attr('climate.lounge', 'hvac_modes') }}"
    world = build_home_world(
        _history([
            (
                _T + timedelta(hours=1),
                "render_home_assistant_template",
                {"template": template},
                "[<HVACMode.HEAT: 'heat'>, <HVACMode.OFF: 'off'>]",
            )
        ]),
        events,
    )
    lounge = world.entity("climate.lounge")
    assert lounge is not None

    assert lounge.as_of(_T + timedelta(hours=2)).attributes["hvac_modes"] == [
        "heat",
        "off",
    ]
