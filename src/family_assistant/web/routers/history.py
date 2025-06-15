import json
import logging
import zoneinfo
from datetime import datetime, timezone  # Added timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import AUTH_ENABLED
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)
history_router = APIRouter()


@history_router.get("/history", response_class=HTMLResponse, name="ui_message_history")
async def view_message_history(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    page: Annotated[
        int, Query(ge=1, description="Page number for message history")
    ] = 1,  # noqa: B008
    per_page: Annotated[
        int, Query(ge=1, le=100, description="Number of conversations per page")
    ] = 10,  # noqa: B008
) -> HTMLResponse:
    """Serves the page displaying message history."""
    templates = request.app.state.templates

    # Extract filter parameters from query string
    interface_type = request.query_params.get("interface_type")
    conversation_id = request.query_params.get("conversation_id")
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")

    # Parse date filters if provided
    date_from_dt = None
    date_to_dt = None
    try:
        if date_from:
            date_from_dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
        if date_to:
            date_to_dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(
            f"Invalid date format in filters: from={date_from}, to={date_to}"
        )

    try:
        # Get the configured timezone from app state
        app_config = getattr(request.app.state, "config", {})
        config_timezone_str = app_config.get(
            "timezone", "UTC"
        )  # Default to UTC if not found
        try:
            config_tz = zoneinfo.ZoneInfo(config_timezone_str)
        except zoneinfo.ZoneInfoNotFoundError:
            logger.warning(
                f"Configured timezone '{config_timezone_str}' not found, defaulting to UTC for history view."
            )
            config_tz = zoneinfo.ZoneInfo("UTC")

        # Get unique interface types and conversation IDs for dropdowns
        all_history_unfiltered = await db_context.message_history.get_all_grouped()
        interface_types = sorted(set(key[0] for key in all_history_unfiltered))
        conversation_ids = sorted(set(key[1] for key in all_history_unfiltered))

        # Get filtered history
        history_by_chat = await db_context.message_history.get_all_grouped(
            interface_type=interface_type if interface_type else None,
            conversation_id=conversation_id if conversation_id else None,
            date_from=date_from_dt,
            date_to=date_to_dt,
        )

        # --- Process into Turns using turn_id ---
        turns_by_chat = {}
        for conversation_key, messages in history_by_chat.items():
            # Ensure messages are sorted chronologically (assuming get_grouped_message_history returns them sorted)

            # --- Pre-parse JSON string fields in messages ---
            for msg in messages:
                for field_name in [
                    "tool_calls",
                    "reasoning_info",
                ]:  # 'tool_calls' was 'tool_calls_info' from DB
                    field_val = msg.get(field_name)
                    if isinstance(field_val, str):
                        if field_val.lower() == "null":  # Handle string "null"
                            msg[field_name] = None
                        else:
                            try:
                                msg[field_name] = json.loads(field_val)
                            except json.JSONDecodeError:
                                logger.warning(
                                    f"Failed to parse JSON string for {field_name} in msg {msg.get('internal_id')}: {field_val[:100]}"
                                )

                # Further parse 'arguments' within tool_calls if it's a JSON string
                if msg.get("tool_calls") and isinstance(msg["tool_calls"], list):
                    for tool_call_item in msg["tool_calls"]:
                        if (
                            isinstance(tool_call_item, dict)
                            and "function" in tool_call_item
                            and isinstance(tool_call_item["function"], dict)
                        ):
                            func_args_str = tool_call_item["function"].get("arguments")
                            if isinstance(func_args_str, str):
                                try:
                                    tool_call_item["function"]["arguments"] = (
                                        json.loads(func_args_str)
                                    )
                                except json.JSONDecodeError:
                                    logger.warning(
                                        f"Failed to parse function arguments JSON string within tool_calls for msg {msg.get('internal_id')}: {func_args_str[:100]}"
                                    )
                                    # Keep original string if parsing fails

            conversation_turns = []
            grouped_by_turn_id = {}

            for msg in messages:
                turn_id = msg.get("turn_id")  # Can be None
                if turn_id not in grouped_by_turn_id:
                    grouped_by_turn_id[turn_id] = []
                grouped_by_turn_id[turn_id].append(msg)

            # Sort turn_ids by the timestamp of their first message, in reverse order (newest first)
            sorted_turn_ids = sorted(
                grouped_by_turn_id.keys(),
                key=lambda tid: (
                    (
                        (
                            lambda ts: (
                                ts.replace(tzinfo=config_tz)
                                if ts.tzinfo is None
                                else ts.astimezone(config_tz)
                            )
                        )(grouped_by_turn_id[tid][0]["timestamp"])
                    )
                    if tid is not None and grouped_by_turn_id[tid]
                    else datetime.min.replace(
                        tzinfo=config_tz
                    )  # Treat None/empty turns as oldest
                ),
                reverse=True,  # Newest turns first
            )

            for turn_id in sorted_turn_ids:
                turn_messages_for_current_id = grouped_by_turn_id[turn_id]

                # Find the initiating message (user or system) for this turn_id group
                # The first message in a sorted group (by timestamp) should be the trigger if it's user/system
                trigger_candidates = [
                    m
                    for m in turn_messages_for_current_id
                    if m["role"] in ("user", "system")
                ]
                initiating_user_msg_for_turn = (
                    trigger_candidates[0] if trigger_candidates else None
                )

                # Find the final assistant response for this turn_id group
                # It should be the last assistant message with actual content.
                assistant_candidates = [
                    m for m in turn_messages_for_current_id if m["role"] == "assistant"
                ]
                contentful_assistant_msgs = [
                    m for m in assistant_candidates if m.get("content")
                ]
                if contentful_assistant_msgs:
                    final_assistant_msg_for_turn = contentful_assistant_msgs[-1]
                elif assistant_candidates:  # Fallback to the very last assistant message in the group (might have only tool_calls)
                    final_assistant_msg_for_turn = assistant_candidates[-1]
                else:
                    final_assistant_msg_for_turn = None

                # Ensure the initiating message isn't also the final assistant message if they are the same object
                # This can happen if a turn only has one assistant message that also serves as a trigger (e.g. for a callback)
                # However, our logic now assigns turn_id to user triggers, so this is less likely.
                if (
                    final_assistant_msg_for_turn is initiating_user_msg_for_turn
                    and final_assistant_msg_for_turn is not None
                    and final_assistant_msg_for_turn["role"] == "user"
                ):  # If it's a user message, it can't be the "final assistant response"
                    final_assistant_msg_for_turn = None

                conversation_turns.append({
                    "turn_id": turn_id,  # Store the turn_id itself
                    "initiating_user_message": initiating_user_msg_for_turn,
                    "final_assistant_response": final_assistant_msg_for_turn,
                    "all_messages_in_group": turn_messages_for_current_id,
                })
            turns_by_chat[conversation_key] = conversation_turns

        # Helper function to get the latest timestamp of a conversation
        def get_conversation_latest_timestamp(turns_list: list[dict]) -> datetime:
            latest_ts = datetime.min.replace(tzinfo=config_tz)
            if not turns_list:  # Should not happen if conversation_key exists
                return latest_ts
            # turns_list is already sorted with the newest turn at index 0
            most_recent_turn = turns_list[0]
            most_recent_turn_messages = most_recent_turn.get("all_messages_in_group")
            if most_recent_turn_messages:
                # Messages in all_messages_in_group are sorted oldest to newest by DB query,
                # but then displayed within the turn trace.
                # The timestamp of the *last* message in the *most recent turn's group*
                # (which is the first turn in turns_list due to reverse sort)
                # should give a good proxy for the conversation's latest activity.
                # If all_messages_in_group is sorted chronologically, its last item is newest.
                ts = most_recent_turn_messages[-1]["timestamp"]
                ts_aware = (
                    ts.replace(tzinfo=config_tz)
                    if ts.tzinfo is None
                    else ts.astimezone(config_tz)
                )
                if ts_aware > latest_ts:
                    latest_ts = ts_aware
            return latest_ts

        # Sort conversations by their latest timestamp, newest first.
        all_items_list = list(turns_by_chat.items())
        all_items_list.sort(
            key=lambda item: get_conversation_latest_timestamp(item[1]), reverse=True
        )

        # --- Pagination Logic ---
        total_conversations = len(all_items_list)
        total_pages = (total_conversations + per_page - 1) // per_page

        current_page = min(page, total_pages) if total_pages > 0 else 1
        start_index = (current_page - 1) * per_page
        end_index = start_index + per_page
        paged_items = all_items_list[start_index:end_index]

        # Pagination metadata for the template
        pagination_info = {
            "current_page": current_page,
            "per_page": per_page,
            "total_conversations": total_conversations,
            "total_pages": total_pages,
            "has_prev": current_page > 1,
            "has_next": current_page < total_pages,
            "prev_num": current_page - 1 if current_page > 1 else None,
            "next_num": current_page + 1 if current_page < total_pages else None,
        }

        # Check if any filters are active
        active_filters = any([interface_type, conversation_id, date_from, date_to])

        return templates.TemplateResponse(
            "message_history.html.j2",
            {
                "request": request,
                "paged_conversations": paged_items,  # Renamed for clarity in template
                "pagination": pagination_info,
                "interface_types": interface_types,
                "conversation_ids": conversation_ids,
                "total_conversations": len(paged_items),
                "active_filters": active_filters,
                "user": request.session.get("user"),
                "AUTH_ENABLED": AUTH_ENABLED,  # Pass to base template
                "now_utc": datetime.now(timezone.utc),  # Pass to base template
            },
        )
    except Exception as e:
        logger.error(f"Error fetching message history: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to fetch message history"
        ) from e
