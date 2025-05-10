import asyncio
import contextlib
import json
import logging
import os
import pathlib
import uuid
import zoneinfo
from datetime import date, datetime, timezone
from typing import Annotated, Any

import aiofiles
import telegram.error  # Import telegram errors for specific checking in health check

# OAuth import removed, will be in auth.py
from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# MarkdownIt import removed, will be in utils.py
from pydantic import BaseModel, ValidationError
from sqlalchemy import text

# Starlette Config, Middleware, SessionMiddleware, ASGIApp, Receive, Scope, Send imports might be needed by auth.py
# For now, keep them if web_server.py still uses them directly, or remove if fully delegated.
# Assuming SessionMiddleware is still configured here or passed to auth module.
from starlette.middleware import Middleware  # Keep for app setup
from starlette.middleware.sessions import SessionMiddleware  # Keep for app setup

# Import storage functions using absolute package path
from family_assistant import storage

# Import embedding generator (adjust path based on actual location)
# Assuming it's accessible via a function or app state
from family_assistant.embeddings import EmbeddingGenerator  # Example
from family_assistant.storage import (
    get_all_tasks,
    get_grouped_message_history,
)
from family_assistant.storage.context import DatabaseContext

# Import protocol for type hinting when creating the dict for add_document
# Import vector search components
from family_assistant.storage.vector_search import (
    MetadataFilter,
    VectorSearchQuery,
    query_vector_store,
)

# Import tool-related components
# Import tool functions directly from the tools package
from family_assistant.tools import (
    ToolExecutionContext,
    ToolNotFoundError,
    ToolsProvider,
    _scan_user_docs,  # Removed incorrect import of render_schema_as_html
)
from family_assistant.tools.schema import render_schema_as_html  # Correct import path

# Import new auth and utils modules
from family_assistant.web.auth import (
    AUTH_ENABLED,
    PUBLIC_PATHS,
    SESSION_SECRET_KEY,
    AuthMiddleware,
    auth_router,
)
from family_assistant.web.dependencies import (
    get_db,
    get_embedding_generator_dependency,
    get_tools_provider_dependency,
)
from family_assistant.web.models import DocumentUploadResponse, SearchResultItem
from family_assistant.web.routers.documentation import (
    documentation_router,
)  # Moved import
from family_assistant.web.routers.notes import notes_router  # Moved import
from family_assistant.web.routers.webhooks import webhooks_router  # New webhooks router
from family_assistant.web.utils import md_renderer

logger = logging.getLogger(__name__)

# Simple in-memory cache for rendered tool schema HTML, keyed by tool name
_tool_html_cache: dict[str, str] = {}

# Directory to save raw webhook request bodies for debugging/replay
MAILBOX_RAW_DIR = "/mnt/data/mailbox/raw_requests"  # TODO: Consider making this configurable via env var

# Load server URL from environment variable (used in templates)
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")


# --- Determine base path for templates and static files ---
# This assumes web_server.py is at src/family_assistant/web_server.py
# We want the paths relative to the 'family_assistant' package directory
try:
    # Get the directory containing the current file (web_server.py)
    _project_root = pathlib.Path(__file__).parent.parent.parent.resolve()
    current_file_dir = pathlib.Path(__file__).parent.resolve()
    # Go up one level to the package root (src/family_assistant/)
    package_root_dir = current_file_dir
    # Define template and static directories relative to the package root
    templates_dir = package_root_dir / "templates"
    static_dir = package_root_dir / "static"

    # Define docs directory relative to project root
    if not templates_dir.is_dir():
        logger.warning(
            f"Templates directory not found at expected location: {templates_dir}"
        )
        # Fallback or raise error? For now, log warning.
    if not static_dir.is_dir():
        logger.warning(f"Static directory not found at expected location: {static_dir}")
        # Fallback or raise error?

    # Define docs directory relative to project root
    docs_user_dir = _project_root / "docs" / "user"

    # Configure templates using the calculated path
    templates = Jinja2Templates(directory=templates_dir)
    # Add the 'tojson' filter to the Jinja environment
    templates.env.filters["tojson"] = json.dumps


except NameError:
    # __file__ might not be defined in some execution contexts (e.g., interactive)
    logger.error(
        "Could not determine package path using __file__. Static/template files might not load."
    )
    # Provide fallback paths relative to CWD, although this might not work reliably
    templates = Jinja2Templates(directory="src/family_assistant/templates")
    static_dir = pathlib.Path(
        "src/family_assistant/static"
    )  # Define fallback static_dir
    # Fallback docs path relative to CWD
    docs_user_dir = pathlib.Path("docs") / "user"
    logger.warning(f"Using fallback user docs directory: {docs_user_dir}")

# md_renderer is now imported from family_assistant.web.utils

# --- Auth Configuration (now largely in family_assistant.web.auth) ---
# AUTH_ENABLED, SESSION_SECRET_KEY are imported.
# oauth object is imported but might not be directly used here anymore.

middleware = []

# Always add SessionMiddleware if the secret key is set.
if SESSION_SECRET_KEY:
    middleware.append(Middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY))
    logger.info("SessionMiddleware added (SESSION_SECRET_KEY is set).")
else:
    logger.warning(
        "SessionMiddleware NOT added (SESSION_SECRET_KEY is not set). Accessing request.session will fail, which might break OIDC if it were enabled."
    )

# AuthMiddleware is imported and will be added to the middleware list later if AUTH_ENABLED.

# Dependency functions are now in family_assistant.web.dependencies

# md_renderer is imported from family_assistant.web.utils
# PUBLIC_PATHS is imported from family_assistant.web.auth
# AuthMiddleware class is imported from family_assistant.web.auth

# --- Add AuthMiddleware to the list if enabled ---
if AUTH_ENABLED:
    # IMPORTANT: Add this *after* SessionMiddleware in the list (added earlier)
    middleware.append(
        Middleware(AuthMiddleware, public_paths=PUBLIC_PATHS, auth_enabled=AUTH_ENABLED)
    )
    logger.info("AuthMiddleware added to the application middleware stack.")
else:
    logger.info("AuthMiddleware NOT added as AUTH_ENABLED is false.")


# --- FastAPI App Initialization ---
app = FastAPI(
    title="Family Assistant Web Interface",
    docs_url="/api/docs",  # URL for Swagger UI (part of /api/, public)
    redoc_url="/api/redoc",  # URL for ReDoc (part of /api/, public)
    middleware=middleware,  # Add configured middleware
)

# --- Store shared objects on app.state ---
app.state.templates = templates  # Make templates instance available to routers
app.state.server_url = SERVER_URL  # Make SERVER_URL available to routers
app.state.docs_user_dir = (
    docs_user_dir  # Make docs_user_dir available (used by documentation_router)
)


# Include the authentication routes
if AUTH_ENABLED:
    app.include_router(auth_router, tags=["Authentication"])
    logger.info("Authentication routes included.")


# --- Mount Static Files (after app initialization) ---
if "static_dir" in locals() and static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Mounted static files from: {static_dir}")
else:
    logger.error(
        f"Static directory '{static_dir if 'static_dir' in locals() else 'Not Defined'}' not found or not a directory. Static files will not be served."
    )

# Models are now in family_assistant.web.models

# --- Auth Routes are now in family_assistant.web.auth and included via auth_router ---

# --- Include Routers ---
# Imports were moved to the top of the file.
app.include_router(notes_router, tags=["Notes"])
app.include_router(
    documentation_router, tags=["Documentation"]
)  # Keep existing documentation router
app.include_router(webhooks_router, tags=["Webhooks"]) # Include new webhooks router

# --- Remaining Application Routes (to be moved) ---


@app.get("/history", response_class=HTMLResponse)
async def view_message_history(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],  # noqa: B008
    page: Annotated[
        int, Query(ge=1, description="Page number for message history")
    ] = 1,  # noqa: B008
    per_page: Annotated[
        int, Query(ge=1, le=100, description="Number of conversations per page")
    ] = 10,  # noqa: B008
) -> HTMLResponse:
    """Serves the page displaying message history."""
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

        history_by_chat = await get_grouped_message_history(db_context)

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
                    else datetime.min.replace(tzinfo=config_tz)
                ),
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
                elif (
                    assistant_candidates
                ):  # Fallback to the very last assistant message in the group (might have only tool_calls)
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

                conversation_turns.append(
                    {
                        "turn_id": turn_id,  # Store the turn_id itself
                        "initiating_user_message": initiating_user_msg_for_turn,
                        "final_assistant_response": final_assistant_msg_for_turn,
                        "all_messages_in_group": turn_messages_for_current_id,
                    }
                )
            turns_by_chat[conversation_key] = conversation_turns

        # --- Pagination Logic ---
        # Convert dict items to a list for slicing. Note: Dict order is not guaranteed
        # before Python 3.7, but generally insertion order from 3.7+.
        # If a specific order of *conversations* is needed (e.g., by most recent message),
        # more complex sorting would be required here *before* pagination.
        # Paginate based on the processed turns_by_chat
        all_items = list(turns_by_chat.items())
        total_conversations = len(all_items)
        total_pages = (total_conversations + per_page - 1) // per_page

        # Ensure page number is valid
        current_page = min(page, total_pages) if total_pages > 0 else 1
        start_index = (current_page - 1) * per_page
        end_index = start_index + per_page
        paged_items = all_items[start_index:end_index]

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

        return templates.TemplateResponse(
            "message_history.html",
            {
                "request": request,
                "paged_conversations": paged_items,  # Renamed for clarity in template
                "pagination": pagination_info,
                "user": request.session.get("user"),
                "auth_enabled": AUTH_ENABLED,  # Pass auth info
            },
        )
    except Exception as e:
        logger.error(f"Error fetching message history: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to fetch message history"
        ) from e


@app.get("/tools", response_class=HTMLResponse)
async def view_tools(request: Request) -> HTMLResponse:
    """Serves the page displaying available tools."""
    global _tool_html_cache  # Use the global cache
    try:
        tool_definitions = getattr(request.app.state, "tool_definitions", [])
        if not tool_definitions:
            logger.warning("No tool definitions found in app state for /tools page.")
        # Generate HTML for each tool's parameters on demand, using cache
        rendered_tools = []
        for tool in tool_definitions:
            tool_copy = tool.copy()  # Avoid modifying the original dict in state
            tool_name = tool_copy.get("function", {}).get("name", "UnknownTool")

            # Check cache first
            if tool_name in _tool_html_cache:
                tool_copy["parameters_html"] = _tool_html_cache[tool_name]
            else:
                schema_dict = tool_copy.get("function", {}).get("parameters")
                # Serialize the schema dict to a stable JSON string for the rendering function
                schema_json_str = (  # Removed duplicate line
                    json.dumps(schema_dict, sort_keys=True) if schema_dict else None
                )
                # Call the rendering function (no longer cached itself)
                generated_html = render_schema_as_html(schema_json_str)
                tool_copy["parameters_html"] = generated_html
                _tool_html_cache[tool_name] = generated_html  # Store in cache

            rendered_tools.append(tool_copy)
        return templates.TemplateResponse(
            "tools.html",
            {
                "request": request,
                "tools": rendered_tools,
                "user": request.session.get("user"),
                "auth_enabled": AUTH_ENABLED,
            },
        )
    except Exception as e:
        logger.error(f"Error fetching tool definitions: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to fetch tool definitions"
        ) from e


@app.get("/tasks", response_class=HTMLResponse)
async def view_tasks(
    request: Request, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> HTMLResponse:
    """Serves the page displaying scheduled tasks."""
    try:
        tasks = await get_all_tasks(db_context, limit=200)  # Pass context, fetch tasks
        return templates.TemplateResponse(
            "tasks.html",
            {
                "request": request,
                "tasks": tasks,
                "user": request.session.get("user"),
                "auth_enabled": AUTH_ENABLED,
            },
        )
    except Exception as e:
        logger.error(f"Error fetching tasks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch tasks") from e


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check(request: Request) -> JSONResponse:
    """Checks basic service health and Telegram polling status."""
    telegram_service = getattr(request.app.state, "telegram_service", None)

    if (
        not telegram_service
        or not hasattr(telegram_service, "application")
        or not hasattr(telegram_service.application, "updater")
    ):
        # Service not initialized or structure unexpected
        return JSONResponse(
            content={
                "status": "unhealthy",
                "reason": "Telegram service not initialized",
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Check if polling was ever started and if it's currently running
    was_started = getattr(telegram_service, "_was_started", False)
    is_running = telegram_service.application.updater.running

    if was_started and not is_running:
        # Polling was started but has stopped
        last_error = getattr(telegram_service, "last_error", None)
        reason = "Telegram polling stopped"
        if isinstance(last_error, telegram.error.Conflict):
            reason = f"Telegram polling stopped due to Conflict error: {last_error}"
            logger.warning(
                f"Health check failing due to Telegram Conflict: {last_error}"
            )  # Log warning
        elif last_error:
            reason = (
                f"Telegram polling stopped. Last error: {type(last_error).__name__}"
            )
            logger.warning(
                f"Health check failing because Telegram polling stopped. Last error: {last_error}"
            )  # Log warning
        else:
            logger.warning(
                "Health check failing because Telegram polling stopped (no specific error recorded)."
            )  # Log warning

        return JSONResponse(
            content={"status": "unhealthy", "reason": reason},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    elif not was_started:
        # Polling hasn't been started yet (still initializing)
        return JSONResponse(
            content={
                "status": "initializing",
                "reason": "Telegram service initializing",
            },
            status_code=status.HTTP_200_OK,  # Or 503 if you prefer to fail until fully ready
        )
    else:
        # Polling was started and is running
        return JSONResponse(
            content={"status": "ok", "reason": "Telegram polling active"},
            status_code=status.HTTP_200_OK,
        )


# --- Vector Search Routes ---


@app.get("/vector-search", response_class=HTMLResponse)
async def vector_search_form(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],  # noqa: B008
) -> HTMLResponse:
    """Serves the vector search form."""
    distinct_models = []
    distinct_types = []
    distinct_source_types = []
    distinct_metadata_keys = []  # Added for metadata keys
    error = None
    try:
        # Fetch distinct values for dropdowns/filters
        # Ensure table/column names match your actual schema
        q_models = text(
            "SELECT DISTINCT embedding_model FROM document_embeddings ORDER BY embedding_model;"
        )
        q_types = text(
            "SELECT DISTINCT embedding_type FROM document_embeddings ORDER BY embedding_type;"
        )
        q_source_types = text(
            "SELECT DISTINCT source_type FROM documents ORDER BY source_type;"
        )
        # Query to get distinct top-level keys from the JSONB metadata column
        q_meta_keys = text(
            "SELECT DISTINCT key FROM documents, jsonb_object_keys(doc_metadata) AS keys(key) ORDER BY key;"
        )

        (
            models_result,
            types_result,
            source_types_result,
            meta_keys_result,
        ) = await asyncio.gather(
            db_context.fetch_all(q_models),
            db_context.fetch_all(q_types),
            db_context.fetch_all(q_source_types),
            db_context.fetch_all(q_meta_keys),  # Fetch metadata keys
        )

        distinct_models = [row["embedding_model"] for row in models_result]
        distinct_types = [row["embedding_type"] for row in types_result]
        distinct_source_types = [row["source_type"] for row in source_types_result]
        distinct_metadata_keys = [
            row["key"] for row in meta_keys_result
        ]  # Populate metadata keys

    except Exception as e:
        logger.error(
            f"Failed to fetch distinct values for search form: {e}", exc_info=True
        )
        error = "Could not load filter options from database."
        # Continue without pre-populated dropdowns

    return templates.TemplateResponse(
        "vector_search.html",
        {
            "request": request,
            "results": None,
            "search_params": {},  # Empty for initial GET
            "error": error,
            "distinct_models": distinct_models,
            "distinct_types": distinct_types,
            "distinct_source_types": distinct_source_types,
            "distinct_metadata_keys": distinct_metadata_keys,  # Pass keys to template
            "user": request.session.get("user"),
            "auth_enabled": AUTH_ENABLED,
        },
    )


@app.post("/vector-search", response_class=HTMLResponse)
async def handle_vector_search(
    request: Request,
    # --- Dependencies ---
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    embedding_generator: Annotated[
        EmbeddingGenerator, Depends(get_embedding_generator_dependency)
    ],
    # --- Form Inputs ---
    semantic_query: Annotated[str | None, Form()] = None,
    keywords: Annotated[str | None, Form()] = None,
    search_type: Annotated[str, Form()] = "hybrid",  # 'semantic', 'keyword', 'hybrid'
    embedding_model: Annotated[str | None, Form()] = None,  # CRUCIAL for vector search
    embedding_types: Annotated[list[str], Form()] = None,  # Allow multiple types
    source_types: Annotated[list[str], Form()] = None,  # Allow multiple source types
    created_after: Annotated[str | None, Form()] = None,  # Expect YYYY-MM-DD
    created_before: Annotated[str | None, Form()] = None,  # Expect YYYY-MM-DD
    title_like: Annotated[str | None, Form()] = None,
    # --- Metadata Filters (expect lists) ---
    metadata_keys: Annotated[list[str], Form()] = None,
    metadata_values: Annotated[list[str], Form()] = None,
    # --- Control Params ---
    limit: Annotated[int, Form()] = 10,
    rrf_k: Annotated[int, Form()] = 60,
) -> HTMLResponse:
    """Handles the vector search form submission."""
    if metadata_values is None:
        metadata_values = []
    if metadata_keys is None:
        metadata_keys = []
    if source_types is None:
        source_types = []
    if embedding_types is None:
        embedding_types = []
    results = None
    error = None
    query_embedding = None

    # --- Default keywords to semantic query if keywords are empty ---
    effective_keywords = keywords
    if not keywords and semantic_query:
        effective_keywords = semantic_query
        logger.info(
            f"Keywords field was empty, defaulting to semantic query: '{semantic_query}'"
        )

    search_params = {  # Store params to repopulate form
        "semantic_query": semantic_query,
        "keywords": keywords,  # Store original keywords for form
        "search_type": search_type,
        "embedding_model": embedding_model,
        "embedding_types": embedding_types,
        "source_types": source_types,
        "created_after": created_after,
        "created_before": created_before,
        "title_like": title_like,
        # Store lists for metadata filters
        "metadata_keys": metadata_keys,
        "metadata_values": metadata_values,
        "limit": limit,
        "rrf_k": rrf_k,
    }
    distinct_models = []  # Fetch again or pass from GET state if possible
    distinct_types = []
    distinct_source_types = []
    distinct_metadata_keys = []  # Fetch again

    try:
        # --- Parse Dates (handle potential errors) ---
        created_after_dt: datetime | None = None
        if created_after:
            try:
                # Assume YYYY-MM-DD, make it timezone-aware (start of day UTC)
                created_after_dt = datetime.strptime(created_after, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                raise ValueError(
                    "Invalid 'Created After' date format. Use YYYY-MM-DD."
                ) from None
        created_before_dt: datetime | None = None
        if created_before:
            try:
                # Assume YYYY-MM-DD, make it timezone-aware (end of day UTC)
                # Add 1 day and subtract epsilon or use < comparison in SQL
                # Simpler: use the date directly, SQL query uses <=
                created_before_dt = datetime.strptime(
                    created_before, "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                raise ValueError(
                    "Invalid 'Created Before' date format. Use YYYY-MM-DD."
                ) from None

        # --- Build List of Metadata Filters ---
        metadata_filters_list: list[MetadataFilter] = []
        if len(metadata_keys) != len(metadata_values):
            # This indicates a potential issue with form submission or client-side JS
            logger.error(
                f"Mismatch between metadata keys ({len(metadata_keys)}) and values ({len(metadata_values)}). Ignoring metadata filters."
            )
            error = "Error: Mismatch in metadata filter keys and values."
            # Optionally clear the lists to prevent partial filtering
            metadata_keys = []
            metadata_values = []
        else:
            for key, value in zip(metadata_keys, metadata_values, strict=False):
                if (
                    key and value is not None
                ):  # Allow empty string value, but require key
                    metadata_filters_list.append(MetadataFilter(key=key, value=value))
                elif key and value is None:
                    logger.warning(
                        f"Metadata key '{key}' provided without a value. Ignoring this filter."
                    )
                # No warning needed if key is empty, as it's likely from an empty template row

        # --- Create Query Object ---
        # Validation is handled by the dataclass __post_init__
        query_obj = VectorSearchQuery(
            search_type=search_type,
            semantic_query=semantic_query,
            keywords=effective_keywords,  # Use the potentially defaulted keywords
            embedding_model=embedding_model,
            embedding_types=embedding_types,
            source_types=source_types,
            created_after=created_after_dt,
            created_before=created_before_dt,
            title_like=title_like,
            metadata_filters=metadata_filters_list,  # Pass the list
            limit=limit,
            rrf_k=rrf_k,
        )

        # --- Generate Embedding ---
        if query_obj.search_type in ["semantic", "hybrid"]:
            # Basic check, might need more robust model matching/selection
            embedding_result = await embedding_generator.generate_embeddings(
                [query_obj.semantic_query]
            )  # Pass as list
            if not embedding_result.embeddings or len(embedding_result.embeddings) == 0:
                raise ValueError("Failed to generate embedding for the semantic query.")
            query_embedding = embedding_result.embeddings[0]
            # Optional: Check dimension if needed by query_vector_store

        # --- Execute Search ---
        raw_results = await query_vector_store(
            db_context=db_context,
            query=query_obj,
            query_embedding=query_embedding,
        )
        # Convert raw results (dicts) to Pydantic models for consistency (optional)
        results = [SearchResultItem.parse_obj(row) for row in raw_results]

    except ValueError as ve:
        logger.warning(f"Validation error during vector search: {ve}")
        error = str(ve)
    except NotImplementedError as nie:
        logger.error(f"Configuration error: {nie}")
        error = f"Server configuration error: {nie}"
    except Exception as e:
        logger.error(f"Error during vector search: {e}", exc_info=True)
        error = f"An unexpected error occurred: {e}"

    # Fetch distinct values again for rendering the form correctly
    # TODO: Consider caching these or passing via state if performance is an issue
    try:
        q_models = text(
            "SELECT DISTINCT embedding_model FROM document_embeddings ORDER BY embedding_model;"
        )
        q_types = text(
            "SELECT DISTINCT embedding_type FROM document_embeddings ORDER BY embedding_type;"
        )
        q_source_types = text(
            "SELECT DISTINCT source_type FROM documents ORDER BY source_type;"
        )
        q_meta_keys = text(
            "SELECT DISTINCT key FROM documents, jsonb_object_keys(doc_metadata) AS keys(key) ORDER BY key;"
        )

        # Use asyncio.gather for concurrent fetching
        (
            models_result,
            types_result,
            source_types_result,
            meta_keys_result,
        ) = await asyncio.gather(
            db_context.fetch_all(q_models),
            db_context.fetch_all(q_types),
            db_context.fetch_all(q_source_types),
            db_context.fetch_all(q_meta_keys),
        )

        distinct_models = [row["embedding_model"] for row in models_result]
        distinct_types = [row["embedding_type"] for row in types_result]
        distinct_source_types = [row["source_type"] for row in source_types_result]
        distinct_metadata_keys = [row["key"] for row in meta_keys_result]  # Get keys

    except Exception as e:
        logger.error(
            f"Failed to fetch distinct values for search form render: {e}",
            exc_info=True,
        )
        # Don't overwrite previous error, but log this one
        if not error:
            error = "Could not load filter options from database."

    return templates.TemplateResponse(
        "vector_search.html",
        {
            "request": request,
            "results": results,
            "search_params": search_params,  # Pass back params
            "error": error,
            "distinct_models": distinct_models,
            "distinct_types": distinct_types,
            "distinct_source_types": distinct_source_types,
            "distinct_metadata_keys": distinct_metadata_keys,  # Pass keys
            "user": request.session.get("user"),
            "auth_enabled": AUTH_ENABLED,
        },
    )


# --- Tool Execution API ---
class ToolExecutionRequest(BaseModel):
    arguments: dict[str, Any]


@app.post("/api/tools/execute/{tool_name}", response_class=JSONResponse)
async def execute_tool_api(
    tool_name: str,
    request: Request,  # Keep request for potential context later
    payload: ToolExecutionRequest,
    tools_provider: Annotated[ToolsProvider, Depends(get_tools_provider_dependency)],
    db_context: Annotated[
        DatabaseContext, Depends(get_db)
    ],  # Inject DB context if tools need it
) -> JSONResponse:
    """Executes a specified tool with the given arguments."""
    logger.info(
        f"Received execution request for tool: {tool_name} with args: {payload.arguments}"
    )

    # --- Retrieve necessary config from app state ---
    app_config = getattr(
        request.app.state, "config", {}
    )  # Assuming config is stored in state
    if not app_config:
        logger.error("Main application configuration not found in app state.")
        # Fallback to empty dicts/defaults, but log error
        calendar_config = {}
        timezone_str = "UTC"
    else:
        calendar_config = app_config.get("calendar_config", {})
        timezone_str = app_config.get("timezone", "UTC")

    # --- Create Execution Context ---
    # We need some context, minimum placeholders for now
    # Generate a unique ID for this specific API call context
    # This isn't a persistent conversation like Telegram
    execution_context = ToolExecutionContext(
        interface_type="api",  # Identify interface
        conversation_id=f"api_call_{uuid.uuid4()}",
        db_context=db_context,
        calendar_config=calendar_config,  # Pass fetched calendar config
        timezone_str=timezone_str,  # Pass fetched timezone string
        application=None,  # No Telegram app here
        request_confirmation_callback=None,  # No confirmation from API for now
        processing_service=None,  # API endpoint doesn't have access to this
    )

    try:
        result = await tools_provider.execute_tool(
            name=tool_name, arguments=payload.arguments, context=execution_context
        )
        logger.info(f"Tool '{tool_name}' executed successfully.")

        # Attempt to parse result if it's a JSON string
        final_result = result
        if isinstance(result, str):
            with contextlib.suppress(json.JSONDecodeError):
                final_result = json.loads(result)
        return JSONResponse(
            content={"success": True, "result": final_result}, status_code=200
        )
    except ToolNotFoundError:
        logger.warning(f"Tool '{tool_name}' not found for execution request.")
        raise HTTPException(
            status_code=404, detail=f"Tool '{tool_name}' not found."
        ) from None
    except (
        ValidationError
    ) as ve:  # Catch Pydantic validation errors if execute_tool raises them
        logger.warning(f"Argument validation error for tool '{tool_name}': {ve}")
        raise HTTPException(
            status_code=400, detail=f"Invalid arguments for tool '{tool_name}': {ve}"
        ) from ve
    except (
        TypeError
    ) as te:  # Catch potential argument mismatches within the tool function
        logger.error(
            f"Type error during execution of tool '{tool_name}': {te}", exc_info=True
        )
        raise HTTPException(
            status_code=400,
            detail=f"Argument mismatch or type error in tool '{tool_name}': {te}",
        ) from te
    except Exception as e:
        logger.error(f"Error executing tool '{tool_name}': {e}", exc_info=True)
        # Avoid leaking internal error details unless intended
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred while executing tool '{tool_name}'.",
        ) from e


# --- API Routes ---


@app.post(
    "/api/documents/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload and index a document",
    description="Accepts document metadata and content parts via multipart/form-data, "
    "stores the document record, and enqueues a background task for embedding generation.",
)
async def upload_document(
    # Required fields
    source_type: Annotated[
        str,
        Form(
            description="Type of the source (e.g., 'manual_upload', 'scanned_receipt')."
        ),
    ] = ...,
    source_id: Annotated[
        str,
        Form(description="Unique identifier for the document within its source type."),
    ] = ...,
    content_parts_json: Annotated[
        str,
        Form(
            alias="content_parts",
            description='JSON string representing a dictionary of content parts to be indexed. Keys determine embedding type (e.g., {"title": "Doc Title", "content_chunk_0": "First paragraph..."}).',
        ),
    ] = ...,
    # Optional fields
    source_uri: Annotated[
        str | None, Form(description="Canonical URI/URL of the original document.")
    ] = None,
    title: Annotated[
        str | None,
        Form(
            description="Primary title for the document (can also be in content_parts)."
        ),
    ] = None,
    created_at_str: Annotated[
        str | None,
        Form(
            alias="created_at",
            description="Original creation timestamp (ISO 8601 format string, e.g., 'YYYY-MM-DDTHH:MM:SSZ' or 'YYYY-MM-DD'). Timezone assumed UTC if missing.",
        ),
    ] = None,
    metadata_json: Annotated[
        str | None,
        Form(
            alias="metadata",
            description="JSON string representing a dictionary of additional metadata.",
        ),
    ] = None,
    # Dependencies
    db_context: Annotated[DatabaseContext, Depends(get_db)] = None,  # noqa: B008
) -> DocumentUploadResponse:
    """
    API endpoint to upload document metadata and content parts for indexing.
    """
    logger.info(
        f"Received document upload request for source_id: {source_id} (type: {source_type})"
    )

    # --- 1. Parse and Validate Inputs ---
    try:
        # Parse JSON strings
        content_parts: dict[str, str] = json.loads(content_parts_json)
        if not isinstance(content_parts, dict) or not content_parts:
            raise ValueError("'content_parts' must be a non-empty JSON object string.")
        # Validate content parts values are strings
        for key, value in content_parts.items():
            if not isinstance(value, str):
                raise ValueError(f"Value for content part '{key}' must be a string.")

        doc_metadata: dict[str, Any] = {}
        if metadata_json:
            doc_metadata = json.loads(metadata_json)
            if not isinstance(doc_metadata, dict):
                raise ValueError(
                    "'metadata' must be a valid JSON object string if provided."
                )

        # Parse date string (handle date vs datetime)
        created_at_dt: datetime | None = None
        if created_at_str:
            try:
                # Try parsing as full ISO 8601 datetime first
                created_at_dt = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
                # Ensure timezone-aware (assume UTC if naive)
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                # Try parsing as YYYY-MM-DD date
                try:
                    created_date = date.fromisoformat(created_at_str)
                    # Convert date to datetime (start of day, UTC)
                    created_at_dt = datetime.combine(
                        created_date, datetime.min.time(), tzinfo=timezone.utc
                    )
                except ValueError:
                    raise ValueError(
                        "Invalid 'created_at' format. Use ISO 8601 datetime (YYYY-MM-DDTHH:MM:SSZ) or date (YYYY-MM-DD)."
                    ) from None

    except json.JSONDecodeError as json_err:
        logger.error(f"JSON parsing error for document upload {source_id}: {json_err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON format: {json_err}",
        ) from json_err
    except ValueError as val_err:
        logger.error(f"Validation error for document upload {source_id}: {val_err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(val_err)
        ) from val_err
    except Exception as e:
        logger.error(
            f"Unexpected parsing error for document upload {source_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing request data.",
        ) from e

    # --- 2. Create Document Record in DB ---
    # Create a dictionary conforming to the Document protocol structure
    # Use provided title if available, otherwise None
    document_data = {
        "_source_type": source_type,
        "_source_id": source_id,
        "_source_uri": source_uri,
        "_title": title,  # Use the dedicated title field if provided
        "_created_at": created_at_dt,
        "_base_metadata": doc_metadata,
        # These properties are part of the protocol definition
        "source_type": property(lambda self: self["_source_type"]),
        "source_id": property(lambda self: self["_source_id"]),
        "source_uri": property(lambda self: self["_source_uri"]),
        "title": property(lambda self: self["_title"]),
        "created_at": property(lambda self: self["_created_at"]),
        "metadata": property(lambda self: self["_base_metadata"]),
    }

    # Define a simple class on the fly that behaves like the Document protocol
    # This avoids needing a direct import of a specific Document implementation
    class UploadedDocument:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        @property
        def source_type(self) -> str:
            return self._data["_source_type"]

        @property
        def source_id(self) -> str:
            return self._data["_source_id"]

        @property
        def source_uri(self) -> str | None:
            return self._data["_source_uri"]

        @property
        def title(self) -> str | None:
            return self._data["_title"]

        @property
        def created_at(self) -> datetime | None:
            return self._data["_created_at"]

        @property
        def metadata(self) -> dict[str, Any] | None:
            return self._data["_base_metadata"]

    doc_for_storage = UploadedDocument(document_data)

    try:
        document_id: int = await storage.add_document(
            db_context=db_context,
            doc=doc_for_storage,
            # No separate enriched metadata here, it's already merged
        )
        logger.info(f"Stored document record for {source_id}, got DB ID: {document_id}")
    except Exception as db_err:
        logger.error(
            f"Database error storing document record for {source_id}: {db_err}",
            exc_info=True,
        )
        # Check for unique constraint violation (source_id already exists)
        # This check might be dialect-specific or require inspecting the exception details
        if "UNIQUE constraint failed" in str(
            db_err
        ) or "duplicate key value violates unique constraint" in str(db_err):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Document with source_type '{source_type}' and source_id '{source_id}' already exists.",
            ) from db_err
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error storing document.",
        ) from db_err

    # --- 3. Enqueue Background Task for Embedding ---
    task_payload = {
        "document_id": document_id,
        "content_parts": content_parts,  # Pass the parsed dictionary
    }
    task_id = f"index-doc-{document_id}-{datetime.now(timezone.utc).isoformat()}"  # Unique task ID
    task_enqueued = False
    try:
        await storage.enqueue_task(
            db_context=db_context,
            task_id=task_id,
            task_type="process_uploaded_document",  # Matches the handler registration
            payload=task_payload,
        )
        task_enqueued = True
        logger.info(f"Enqueued task '{task_id}' to process document ID {document_id}")
    except Exception as task_err:
        logger.error(
            f"Failed to enqueue indexing task for document ID {document_id}: {task_err}",
            exc_info=True,
        )
        # Document record exists, but indexing won't happen automatically.
        # Return success but indicate task failure? Or return an error?
        # Let's return success for the upload but log the task error clearly.
        # The response model will indicate task_enqueued=False.

    # --- 4. Return Response ---
    return DocumentUploadResponse(
        message="Document received and accepted for processing.",
        document_id=document_id,
        task_enqueued=task_enqueued,
    )


# --- Documentation Route ---


@app.get("/docs/")
async def redirect_to_user_guide() -> RedirectResponse:
    """Redirects the base /docs/ path to the main user guide."""
    return RedirectResponse(
        url="/docs/USER_GUIDE.md", status_code=status.HTTP_302_FOUND
    )


@app.get("/docs/{filename:path}", response_class=HTMLResponse)
async def serve_documentation(request: Request, filename: str) -> HTMLResponse:
    """Serves rendered Markdown documentation files from the docs/user directory."""
    allowed_extensions = {".md"}
    doc_path = (docs_user_dir / filename).resolve()

    # Security Checks
    if docs_user_dir not in doc_path.parents:
        logger.warning(f"Attempted directory traversal access to: {doc_path}")
        raise HTTPException(
            status_code=404, detail="Document not found (invalid path)."
        )

    if doc_path.suffix not in allowed_extensions:
        logger.warning(f"Attempted access to non-markdown file: {doc_path}")
        raise HTTPException(
            status_code=404, detail="Document not found (invalid file type)."
        )

    if not doc_path.is_file():
        logger.warning(f"Documentation file not found: {doc_path}")
        raise HTTPException(status_code=404, detail="Document not found.")

    try:
        async with aiofiles.open(doc_path, encoding="utf-8") as f:
            content_md = await f.read()

        # --- Replace placeholder with actual SERVER_URL ---
        content_md_processed = content_md.replace("{{ SERVER_URL }}", SERVER_URL)

        # Render Markdown to HTML
        content_html = md_renderer.render(content_md_processed)

        # Scan for other available docs for navigation
        available_docs = _scan_user_docs()

        return templates.TemplateResponse(
            "doc_page.html",
            {
                "request": request,
                "content": content_html,
                "title": filename,
                "available_docs": available_docs,
                "server_url": SERVER_URL,
            },
            {
                "request": request,
                "content": content_html,
                "title": filename,
                "available_docs": available_docs,
                "server_url": SERVER_URL,
                "user": request.session.get("user"),
                "auth_enabled": AUTH_ENABLED,
            },
        )
    except Exception as e:
        logger.error(
            f"Error serving documentation file '{filename}': {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Error rendering documentation."
        ) from e


# --- Uvicorn Runner (for standalone testing) ---
if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Uvicorn server for testing...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


# --- Main Entry Point Setup (for __main__.py) ---
# Keep this function definition if __main__.py imports and uses it
# Otherwise, it can be removed if __main__.py directly uses the 'app' instance
# def get_web_app():
#     """Returns the configured FastAPI application instance."""
