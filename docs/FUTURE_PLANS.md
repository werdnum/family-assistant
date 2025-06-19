# Future Plans

This document outlines potential future enhancements and features for the Family Assistant application.

## Home Assistant Integration

### Home Assistant Context Provider

-   **Goal:**Provide the LLM with real-time information about the state of devices and entities in Home Assistant.
-   **Implementation Idea:**
    -   Create a new `ContextProvider` similar to `CalendarContextProvider` or `NotesContextProvider`.
    -   This provider would connect to a Home Assistant instance (likely via its WebSocket API or REST API).
    -   It would fetch relevant entity states based on a predefined configuration (e.g., specific entities of interest, or entities matching certain criteria).
    -   The information would be formatted and included in the system prompt, allowing the LLM to answer questions like "Are the living room lights on?" or "What's the temperature in the bedroom?".
-   **Configuration:**Would require Home Assistant URL, long-lived access token, and potentially a list/filter for relevant entities.

### Event Listening & Proactive Actions

-   **Goal:**Enable the assistant to react to events from external systems, starting with Home Assistant, and potentially take proactive actions or notify users.
-   **Mechanism:**
    -   **Event Source:**Initially, Home Assistant events (e.g., motion detected, door opened, specific sensor reading crossing a threshold).
    -   **Event Bus/Listener:**The application would need a component that subscribes to the Home Assistant event bus.
    -   **Filtering:**To avoid being overwhelmed by events, a robust filtering mechanism is needed.
        -   **CEL (Common Expression Language):**Consider using CEL for defining event filters. Users could define rules like `event.data.entity_id == 'sensor.living_room_motion' && event.data.new_state.state == 'on'`.
        -   **Database Storage:**Listener configurations (CEL expressions, target actions/prompts, metadata) would be stored in the database.
    -   **Action Trigger:**When a filtered event is matched:
        -   The assistant could be "woken up."
        -   This might involve:
            -   Sending a specific prompt to the LLM with the event data as context (as defined in the listener configuration).
            -   Executing a predefined tool or script.
            -   Sending a notification to a user.
    -   **LLM-Managed Listeners:**
        -   The assistant (LLM) could be given tools to proactively create, modify, or delete these event listeners in the database based on user requests or its own reasoning. For example, a user might say, "Hey, if the garage door is left open for more than 10 minutes, let me know," and the assistant could translate this into a CEL-based listener and store it.
-   **Use Cases:**
    -   User: "If motion is detected in the backyard after 11 PM, send me a Telegram message." (Assistant creates a listener).
    -   "When the front door opens and I'm not home, log it and ask the LLM if any other sensors were triggered recently."
    -   "If the temperature in the server closet exceeds 30°C, trigger a tool to send an alert."

## Other Potential Enhancements

-   **Advanced Calendar Write Operations:**More granular control over adding/modifying events, selecting specific calendars.
-   **Reminder System Overhaul:**Robust reminder setting, notifications, and management, potentially tied to the enhanced calendar features.
-   **User-Specific Preferences:**Allow users to customize daily brief content, notification preferences, etc.
-   **Web UI Enhancements:**Interactive chat interface, dashboard for key information.
