# Events and Event Listeners Web UI Design

## Overview

This document outlines the design for web UI pages to view and manage events and event listeners in the Family Assistant application. The UI will follow the existing server-side rendering patterns using FastAPI and Jinja2 templates.

## Goals

1. **Visibility**: Provide clear visibility into events flowing through the system and which listeners they trigger
2. **Debugging**: Help users understand why listeners did or didn't trigger for specific events
3. **Management**: Allow users to view, enable/disable, and delete event listeners
4. **History**: Show execution history for script-based listeners via the task queue
5. **Coherence**: Maintain consistency with the existing web UI patterns and styling

## Architecture

### Server-Side Rendering Approach

Following the established pattern in the codebase:

- **FastAPI routers** return `TemplateResponse` objects
- **Jinja2 templates** render HTML on the server
- **SimpleCSS** for styling with minimal custom CSS
- **Minimal JavaScript** only for essential interactivity
- **Form submissions** use POST with redirect pattern (PRG)

### Data Flow

1. User requests page → FastAPI router
2. Router fetches data from repositories via DatabaseContext
3. Data is passed to Jinja2 template
4. HTML is rendered server-side and returned
5. Forms submit via POST, redirect after processing

## Page Structure

### 1. Events Page (`/events`)

#### Purpose

Display recent events from all sources with information about which listeners they triggered.

#### Components

**Event List View** (`/events`):

- **Filters**:
  - Source type (Home Assistant, Indexing, Webhook)
  - Time range (last hour, 6 hours, 24 hours, 48 hours)
  - Search by event data content
  - Show only events that triggered listeners
- **Table Columns**:
  - Timestamp (sortable)
  - Source
  - Event Summary (first 100 chars of key data)
  - Triggered Listeners count with links
  - Actions (View Details)
- **Pagination**: 50 events per page with next/previous navigation

**Event Detail View** (`/events/{event_id}`):

- **Event Information**:
  - Full timestamp
  - Source type
  - Event ID
  - Full event data in formatted JSON viewer
- **Triggered Listeners Section**:
  - List of listeners that were triggered
  - Link to each listener's detail page
  - Indication of whether execution succeeded (check task status)
- **Potential Listeners Section**:
  - List of active listeners for this source that didn't trigger
  - Show why they didn't match (match conditions not met)

#### Data Requirements

- Query `recent_events` table with filters
- Join with `event_listeners` to get listener names
- For each triggered listener, check task queue for execution status

### 2. Event Listeners Page (`/event-listeners`)

#### Purpose

Manage event listeners - view configurations, monitor executions, enable/disable.

#### Components

**Listener List View** (`/event-listeners`):

- **Filters**:
  - Source type
  - Action type (LLM callback, Script)
  - Status (enabled/disabled)
  - Has recent executions
- **Table Columns**:
  - Name (linked to detail view)
  - Source
  - Action Type (icon for LLM vs Script)
  - Status (Enabled/Disabled toggle)
  - Today's Executions / Daily Limit
  - Last Triggered
  - Created Date
  - Actions (View, Toggle, Delete)
- **Bulk Actions**:
  - Enable/Disable selected
  - Delete selected (with confirmation)

**Listener Detail View** (`/event-listeners/{listener_id}`):

- **Configuration Section**:
  - Name and description
  - Source type
  - Match conditions (formatted as readable rules)
  - Action type and configuration
  - Created date and conversation ID
- **Script Section** (for script-type listeners):
  - Syntax-highlighted Starlark code
  - "Test Script" button (uses test_event_listener_script tool)
  - Script configuration (timeout, allowed tools)
- **LLM Callback Section** (for wake_llm listeners):
  - Callback prompt template
  - Response interface type
- **Execution Statistics**:
  - Total executions
  - Today's executions / daily limit
  - Last execution time
  - Success rate
- **Recent Executions** (last 20):
  - For script listeners: Link to task detail in tasks UI
  - For LLM listeners: Basic execution info
  - Timestamp, status, error message if failed
- **Recent Events** (last 10 that matched):
  - Events that triggered this listener
  - Link to event detail page
- **Actions**:
  - Enable/Disable toggle
  - Delete (with confirmation)
  - Clone (create new listener with same config)

#### Data Requirements

- Query `event_listeners` table
- For execution history:
  - Script listeners: Query tasks table for `script_execution` tasks with matching listener_id
  - LLM listeners: Track via daily_executions and last_execution_at
- Query recent_events for events with this listener in triggered_listener_ids

## Implementation Plan

### Phase 1: Core Infrastructure

1. **Create Routers**:

   ```python
   # web/routers/events_ui.py
   - GET /events - List view
   - GET /events/{event_id} - Detail view
   
   # web/routers/listeners_ui.py  
   - GET /event-listeners - List view
   - GET /event-listeners/{listener_id} - Detail view
   - POST /event-listeners/{listener_id}/toggle - Enable/disable
   - POST /event-listeners/{listener_id}/delete - Delete listener
   ```

2. **Create Templates**:

   ```
   templates/events/
   ├── events_list.html.j2
   └── event_detail.html.j2
   
   templates/listeners/
   ├── listeners_list.html.j2
   └── listener_detail.html.j2
   ```

3. **Update Navigation**:
   - Add "Events" and "Event Listeners" to the automation section in base.html.j2

### Phase 2: Repository Enhancements

1. **EventsRepository additions**:

   ```python
   async def get_events_with_listeners(
       source_id: Optional[str] = None,
       hours: int = 24,
       limit: int = 50,
       offset: int = 0,
       only_triggered: bool = False
   ) -> tuple[list[dict], int]:
       """Get events with listener information."""
   
   async def get_listener_execution_stats(
       listener_id: int
   ) -> dict:
       """Get execution statistics for a listener."""
   ```

2. **TasksRepository additions**:

   ```python
   async def get_tasks_for_listener(
       listener_id: int,
       limit: int = 20
   ) -> list[dict]:
       """Get script execution tasks for a specific listener."""
   ```

### Phase 3: UI Components

1. **Reusable Components**:
   - JSON viewer component (already exists)
   - Match conditions formatter
   - Script syntax highlighter (use Prism.js)
   - Status badge component

2. **JavaScript Enhancements**:
   - `events_filters.js` - Filter form handling
   - `listener_toggle.js` - AJAX enable/disable without page reload

### Phase 4: Testing

1. **Add to UI endpoint tests**:
   - Add `/events` and `/event-listeners` to BASE_UI_ENDPOINTS
   - Test pagination, filters, and detail pages

2. **Integration tests**:
   - Test event → listener → task execution flow
   - Test UI updates when listeners are toggled

## UI/UX Considerations

### Visual Design

1. **Status Indicators**:
   - Green dot: Enabled listener
   - Gray dot: Disabled listener  
   - Yellow dot: Rate limited
   - Red dot: Recent failures

2. **Action Type Icons**:
   - 🤖 LLM callback
   - 📜 Script execution

3. **Color Coding**:
   - Use SimpleCSS's semantic colors
   - Success: green backgrounds
   - Errors: red text on light red background
   - Warnings: yellow accents

### Responsive Design

- Tables scroll horizontally on mobile
- Key information visible without scrolling
- Actions accessible via dropdown on small screens

### Performance

1. **Pagination**: Limit to 50 events / 20 listeners per page
2. **Caching**: Use browser caching for static assets
3. **Indexes**: Ensure database indexes support common queries
4. **Lazy Loading**: Load execution history on demand

## Security Considerations

1. **Authorization**:
   - Respect conversation_id filtering
   - Users can only see their own listeners
   - Admin mode shows all listeners

2. **CSRF Protection**:
   - Use CSRF tokens for all POST forms
   - Validate referrer for state changes

3. **Input Validation**:
   - Sanitize search inputs
   - Validate listener IDs belong to user

## Future Enhancements

1. **Real-time Updates**:
   - WebSocket connection for live event stream
   - Show events as they occur
   - Update execution counts in real-time

2. **Advanced Filtering**:
   - Filter events by JSON path expressions
   - Save common filters as presets
   - Export filtered data as CSV

3. **Batch Operations**:
   - Clone multiple listeners
   - Bulk update match conditions
   - Import/export listener configurations

4. **Analytics Dashboard**:
   - Event volume over time
   - Most active listeners
   - Performance metrics
   - Cost tracking for LLM callbacks

## Conclusion

This design provides comprehensive visibility into the event system while maintaining consistency with the existing UI patterns. The server-side rendering approach ensures good performance and SEO while keeping complexity low. The phased implementation allows for incremental delivery of value while building toward a complete event management solution.
