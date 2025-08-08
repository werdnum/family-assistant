# Events UI

A complete React implementation for viewing and managing system events.

## Components

### EventsApp.jsx

Main router component that handles navigation between:

- `/events` → EventsList (main events list)
- `/events/{event_id}` → EventDetail (individual event view)

### EventsList.jsx

The main events list component featuring:

- **API Integration**: Fetches events from `/api/events` endpoint
- **URL State Management**: Filters and pagination state stored in URL params
- **Real-time Filtering**: Source, time range, and triggered-only filters
- **Responsive Layout**: Grid layout on desktop, column layout on mobile
- **Loading States**: Proper loading indicators and error handling

### EventDetail.jsx

Single event detail view with:

- **Complete Event Information**: ID, source, timestamp, triggered listeners
- **Formatted JSON Display**: Collapsible, syntax-highlighted event data
- **Responsive Design**: Mobile-friendly layout
- **Navigation**: Back button to return to events list

### EventCard.jsx

Individual event display component featuring:

- **Smart Event Summaries**: Context-aware summaries based on event source
- **Collapsible JSON Viewer**: Toggle event data display
- **Source Badges**: Visual indicators for event sources (Home Assistant, Indexing, Webhook)
- **Triggered Listeners**: Shows count and IDs of triggered listeners

### EventFilters.jsx

Filter controls component with:

- **Source Filtering**: Dropdown for Home Assistant, Indexing, Webhook sources
- **Time Range**: 1, 6, 24, 48 hour options
- **Triggered Filter**: Checkbox to show only events that triggered listeners
- **Active Indicator**: Visual feedback when filters are applied

### EventsPagination.jsx

Pagination component supporting:

- **Page Navigation**: Previous/Next buttons with state management
- **Results Info**: Shows current range and total count
- **Loading States**: Disabled during API calls

## API Integration

The components integrate with these backend endpoints:

- `GET /api/events` - List events with filtering and pagination

  - Parameters: `source_id`, `hours`, `only_triggered`, `limit`, `offset`
  - Returns: `{events: EventModel[], total: number}`

- `GET /api/events/{event_id}` - Get single event details

  - Returns: `EventModel` with full event information

## Features

### URL State Management

All filters and pagination state are stored in URL parameters:

- `source_id` - Filter by event source
- `hours` - Time range filter
- `only_triggered` - Show only events with triggered listeners
- `page` - Current page number

### Smart Event Summaries

Events are summarized intelligently based on their source:

- **Home Assistant**: Shows event type and entity ID
- **Indexing**: Shows document type and indexing status
- **Webhook**: Shows HTTP method and path
- **Generic**: Falls back to event type or generic message

### Responsive Design

- **Mobile**: Single column layout with stacked elements
- **Tablet**: Two-column grids with improved spacing
- **Desktop**: Multi-column grids for optimal space usage

### Error Handling

- **404 Errors**: Graceful handling of missing events
- **Network Errors**: Clear error messages with retry options
- **Loading States**: Consistent loading indicators across components

## CSS Modules

Each component uses CSS Modules for styling:

- Scoped styles prevent conflicts
- CSS custom properties for theming
- Responsive breakpoints for mobile/tablet/desktop
- Consistent spacing and typography

## Usage

The Events UI is accessible at `/events` and provides a complete interface for:

- Viewing recent system events
- Filtering by source, time range, and trigger status
- Examining detailed event data and JSON payloads
- Understanding which event listeners were triggered
