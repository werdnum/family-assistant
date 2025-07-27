# Chat UI Implementation

## Overview

A React-based chat interface has been implemented for the Family Assistant using the assistant-ui
library. This provides a modern, interactive chat experience integrated with the existing FastAPI
backend.

## Architecture

### Frontend Components

1. **React Application** (`/frontend/src/chat/`)

   - `ChatApp.jsx` - Main chat component using assistant-ui with integrated layout
   - `NavHeader.jsx` - Reusable navigation header component
   - `index.jsx` - Entry point that imports Simple.css and custom styles
   - `chat.css` - Styling for the chat interface using CSS variables
   - `chat.html` - HTML entry point for Vite to build the chat SPA

2. **Integration with assistant-ui**

   - Uses `@assistant-ui/react` for the chat UI components
   - Implements `useExternalStoreRuntime` for custom backend integration
   - Handles message state management and API communication

### Backend Integration

1. **API Endpoint** (`/api/v1/chat/send_message`)

   - Accepts POST requests with prompt, conversation_id, and profile_id
   - Returns assistant reply, conversation_id, and turn_id
   - Supports authentication via session or bearer tokens

2. **Web Routes** (`/workspace/src/family_assistant/web/routers/chat_ui.py`)

   - `/chat` - Main chat interface page
   - `/chat/conversations` - List of previous conversations

3. **Template System**

   - `chat.html.j2` - Jinja2 template for the chat page
   - Loads appropriate assets based on DEV_MODE (Vite dev server vs production)

## Development Setup

### Vite Configuration

- Entry points defined for both main app and chat module
- Dev server runs on port 5173 with proxy to backend on port 8000
- Production builds output to static directory
- Build process: `frontend/chat.html` → Vite → `static/dist/chat.html`
- The built HTML is served by FastAPI's HTML fallback handler in production
- In development, the Jinja2 template loads scripts directly from Vite dev server

### Dependencies

- React 18.3.1
- @assistant-ui/react 0.5.0
- Vite 7.0.0 for build tooling

## Features Implemented

1. **Real-time Chat Interface**

   - User message input with send button
   - Assistant response display
   - Loading states during API calls
   - Error handling for failed requests

2. **Conversation Management**

   - Automatic conversation_id generation
   - Support for continuing existing conversations via URL parameter
   - Message history stored in database

3. **Authentication Integration**

   - Redirects to login on 401 errors
   - Supports both session-based and token-based auth

4. **Styling and UI Integration**

   - Responsive design matching Simple.css theme
   - Custom styling for chat bubbles matching message_history.html colors:
     - User messages: Light blue background (#e1f5fe)
     - Assistant messages: Light green background (#f1f8e9)
   - Full-height chat container with proper layout structure
   - **NEW**: Integrated navigation header component (`NavHeader.jsx`)
   - **NEW**: Consistent header and footer matching the rest of the application
   - **NEW**: Current page highlighting in navigation
   - **NEW**: CSS variables from Simple.css for consistent theming

5. **Development Environment**

   - **NEW**: Vite configuration supports clean URLs in dev mode (`/chat` instead of `/chat.html`)
   - **NEW**: Conditional base URL for dev vs production builds
   - **NEW**: Query parameter handling in URL rewriting middleware
   - Hot Module Replacement (HMR) working properly

## Implementation Complete

All previously identified issues have been resolved:

1. **Module Loading - FIXED**

   - Vite now builds HTML entry points for each SPA
   - FastAPI serves templates that properly load assets from Vite in dev mode
   - No more duplicate scripts or module conflicts

2. **Development Mode - FIXED**

   - Unified template system works in both dev and production
   - Dev mode detection uses app.state.config instead of environment variables
   - Asset paths are correctly resolved in both modes

3. **Testing Infrastructure - ADDED**

   - `built_frontend` fixture ensures assets are built before tests
   - Console error detection validates no JavaScript errors
   - All web tests pass with the new setup

## Next Steps

Add features like:

- Message history display
- Typing indicators
- File attachments
- Tool execution visualization
- Streaming responses

## Recent Updates (January 2025)

1. **UI Consistency Improvements**

   - Added navigation header component matching the main application
   - Integrated Simple.css and custom styles for consistent theming
   - Updated message styling to match message_history.html color scheme
   - Added proper semantic HTML structure with header, main, and footer

2. **Development Experience Enhancements**

   - Fixed Vite configuration to support clean URLs (`/chat` instead of `/chat.html`)
   - Added conditional base URL handling for dev vs production
   - Improved URL rewriting to handle query parameters
   - Ensured HMR works properly in development mode

3. **Code Review Hook Improvements**

   - Made review hook less strict for minor issues
   - Added ability to override minor warnings with sentinel phrase
   - Only block commits on critical issues (build breaks, runtime errors, security)
   - Simplified sentinel detection to work with multi-line commit messages

## Testing

- Unit tests for API endpoint exist in `test_chat_api_endpoint.py`
- UI endpoint accessibility tests updated to include chat routes
- All web tests pass with the new chat UI implementation
- Console error detection validates proper JavaScript execution
- Running test suite: `poe test -n2` for parallel execution
