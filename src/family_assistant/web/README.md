# Web Directory

This directory contains the FastAPI web application layer for the Family Assistant. It provides both a web UI and REST API endpoints for interacting with the assistant's functionality.

## Architecture Overview

The web layer follows a modular FastAPI structure with clear separation of concerns:

- **Application Creation**: `app_creator.py` sets up the FastAPI app with middleware, routers, and static files
- **Authentication**: `auth.py` provides OIDC and API token authentication with configurable public paths
- **Dependencies**: `dependencies.py` contains FastAPI dependency injection for database context and services
- **Models**: `models.py` defines Pydantic models for API requests and responses
- **Utilities**: `utils.py` contains shared utility functions

## File Structure

### Core Files

- **`app_creator.py`**: Main FastAPI application factory that:
  - Configures middleware (sessions, authentication)
  - Registers all routers with appropriate prefixes
  - Sets up static file serving and Jinja2 templates
  - Handles application startup and shutdown events

- **`auth.py`**: Authentication system supporting:
  - OIDC (OpenID Connect) integration for web UI authentication
  - API token authentication for programmatic access
  - Configurable public paths that bypass authentication
  - Middleware for request authentication and authorization

- **`dependencies.py`**: FastAPI dependency providers for:
  - Database context injection (`get_db`)
  - Processing service access (`get_processing_service`)
  - Shared service configuration

- **`models.py`**: Pydantic models for:
  - Chat API requests and responses
  - Document upload responses
  - API token management
  - Search result items
  - Vector search requests

- **`utils.py`**: Shared utility functions used across routers

### Routers Directory

The `routers/` directory contains modular FastAPI routers organized by functionality:

#### API Endpoints (`/api` prefix)
- **`api.py`**: Main API router that aggregates other API routers
- **`chat_api.py`**: Chat completion endpoints for conversational AI interactions
- **`documents_api.py`**: Document upload and management API endpoints
- **`tools_api.py`**: Tool execution and management API endpoints
- **`api_token_management.py`**: API token CRUD operations for programmatic access
- **`webhooks.py`**: Webhook endpoints for external integrations

#### Web UI Endpoints (no prefix)
- **`documentation.py`**: Document viewing and management UI
- **`documents_ui.py`**: Document upload interface
- **`history.py`**: Message history and conversation management UI
- **`notes.py`**: Note-taking and knowledge management interface
- **`tasks_ui.py`**: Background task monitoring interface
- **`tools_ui.py`**: Tool testing and management interface
- **`ui_token_management.py`**: Web UI for API token management
- **`vector_search.py`**: Vector similarity search interface

#### Utility Endpoints
- **`health.py`**: Health check and status endpoints

## Key Features

### Authentication System
- **OIDC Integration**: Web-based authentication using OpenID Connect providers
- **API Tokens**: Bearer token authentication for API access with configurable expiration
- **Public Paths**: Configurable endpoints that bypass authentication (health checks, webhooks)
- **Middleware**: Request-level authentication with proper error handling

### Chat API
- **Multi-Profile Support**: Route requests to different processing service profiles
- **Conversation Management**: Maintain conversation context with unique conversation IDs
- **Streaming Support**: Real-time response streaming for better user experience

### Document Management
- **Upload Interface**: Web UI and API for document ingestion
- **Processing Pipeline**: Automatic document indexing and embedding generation
- **Metadata Extraction**: Support for various document types with metadata parsing

### Vector Search
- **Hybrid Search**: Combines vector similarity with full-text search using RRF (Reciprocal Rank Fusion)
- **Multiple Embedding Types**: Support for different embedding models and strategies
- **Search Interface**: Both API and web UI for semantic search capabilities

### Tool Integration
- **MCP Support**: Integration with Model Context Protocol for external tools
- **Tool Confirmation**: Safety mechanism for destructive operations
- **Testing Interface**: Web UI for testing tool functionality

## Configuration

The web layer is configured through:

- **Environment Variables**: Authentication settings, database connections, external service URLs
- **`config.yaml`**: Service profiles, processing configurations, tool definitions
- **Template Variables**: Runtime configuration passed to Jinja2 templates

## Development Notes

### Adding New Endpoints
1. Create router file in appropriate subdirectory of `routers/`
2. Define Pydantic models in `models.py` if needed
3. Import and register router in `app_creator.py`
4. Add authentication requirements if needed

### Authentication Flow
1. Requests hit `AuthMiddleware` for path-based auth checking
2. OIDC users get session-based authentication
3. API users provide Bearer tokens validated against database
4. Public paths bypass authentication entirely

### Template System
- Jinja2 templates located in `../templates/`
- Base template provides common layout and styling
- Context includes configuration, user info, and runtime data

### Static Files
- CSS and JavaScript served from `../static/`
- Custom styling in `css/custom.css`
- Interactive functionality in various JS files

## Testing

Web layer components should be tested through:
- **Integration Tests**: Full request/response cycles in `tests/functional/web/`
- **Router Tests**: Individual endpoint testing with mocked dependencies
- **Authentication Tests**: Auth flow validation with test tokens

The web layer integrates closely with the core application services and storage layer, providing a comprehensive interface for all Family Assistant functionality.