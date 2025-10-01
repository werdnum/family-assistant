# Web API Development Guide

This file provides guidance for working with the FastAPI web application layer for the Family
Assistant.

## Overview

The web layer provides both a web UI and REST API endpoints for interacting with the assistant's
functionality. It follows a modular FastAPI structure with clear separation of concerns.

## Architecture

### Core Components

- **Application Creation**: `app_creator.py` sets up the FastAPI app with middleware, routers, and
  static files
- **Authentication**: `auth.py` provides OIDC and API token authentication with configurable public
  paths
- **Dependencies**: `dependencies.py` contains FastAPI dependency injection for database context and
  services
- **Models**: `models.py` defines Pydantic models for API requests and responses
- **Utilities**: `utils.py` contains shared utility functions

### Routers Organization

Routers are organized in the `routers/` directory by functionality:

#### API Endpoints (`/api` prefix)

- **`api.py`**: Main API router that aggregates other API routers
- **`chat_api.py`**: Chat completion endpoints
- **`documents_api.py`**: Document upload and management
- **`tools_api.py`**: Tool execution and management
- **`api_token_management.py`**: API token CRUD operations
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

## Adding New Web API Endpoints

When adding new web API endpoints:

1. **Create your router** in `src/family_assistant/web/routers/`

   - Organize by functionality (API vs. UI, feature area)
   - Follow existing patterns for dependency injection
   - Use Pydantic models for request/response validation

2. **Define Pydantic models** in `models.py` if needed

   - Use proper type hints
   - Add validation rules where appropriate
   - Include clear descriptions

3. **Register router** in `app_creator.py`

   - Import the router
   - Add to the appropriate router list (API or UI)
   - Set correct prefix and tags

4. **Add authentication requirements** if needed

   - Configure public paths in auth configuration
   - Use dependency injection for auth checks
   - Document auth requirements in endpoint docstrings

5. **Important**: Add your new endpoint to the appropriate test files in `tests/functional/web/` to
   ensure it's tested for basic functionality

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

- **Hybrid Search**: Combines vector similarity with full-text search using RRF (Reciprocal Rank
  Fusion)
- **Multiple Embedding Types**: Support for different embedding models and strategies
- **Search Interface**: Both API and web UI for semantic search capabilities

### Tool Integration

- **MCP Support**: Integration with Model Context Protocol for external tools
- **Tool Confirmation**: Safety mechanism for destructive operations
- **Testing Interface**: Web UI for testing tool functionality

## Development Patterns

### Authentication Flow

1. Requests hit `AuthMiddleware` for path-based auth checking
2. OIDC users get session-based authentication
3. API users provide Bearer tokens validated against database
4. Public paths bypass authentication entirely

### Dependency Injection

Use FastAPI dependencies for shared resources:

```python
from family_assistant.web.dependencies import get_db, get_processing_service

@router.get("/my-endpoint")
async def my_endpoint(
    db: DatabaseContext = Depends(get_db),
    processing_service: ProcessingService = Depends(get_processing_service),
):
    # Your endpoint logic
    pass
```

### Error Handling

Return appropriate HTTP status codes and error messages:

```python
from fastapi import HTTPException

if not resource:
    raise HTTPException(status_code=404, detail="Resource not found")
```

## UI Pages

**Note**: UI pages are now handled entirely by the React frontend. If you need to add new UI views,
create React components in the `frontend/` directory rather than server-side endpoints.

The web layer provides API endpoints that the frontend consumes.

## Testing Web Endpoints

Web layer components should be tested through:

- **Integration Tests**: Full request/response cycles in `tests/functional/web/`
- **Router Tests**: Individual endpoint testing with mocked dependencies
- **Authentication Tests**: Auth flow validation with test tokens

See [tests/functional/web/CLAUDE.md](../../../tests/functional/web/CLAUDE.md) for detailed testing
guidance.

## Configuration

The web layer is configured through:

- **Environment Variables**: Authentication settings, database connections, external service URLs
- **`config.yaml`**: Service profiles, processing configurations, tool definitions
- **Template Variables**: Runtime configuration passed to Jinja2 templates
