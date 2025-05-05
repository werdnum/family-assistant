import logging
import os
import re
from fastapi import (
    FastAPI,
    Request,
    Form,
    HTTPException,
    Depends,
    Response, # Added Query for pagination parameters
    status,
)  # Added status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    JSONResponse,
)  # Added JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import Query # Added Query for pagination parameters
from typing import List, Dict, Optional, Any  # Added Any
from datetime import datetime, timezone, date  # Added date
import json
import uuid  # Added import
import pathlib  # Import pathlib for finding template/static dirs
import telegram.error  # Import telegram errors for specific checking in health check
import aiofiles  # For reading docs
from markdown_it import MarkdownIt  # For rendering docs
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError  # Import BaseModel for request body

# Import tool-related components
from family_assistant.tools import (
    ToolsProvider,
    ToolExecutionContext,
    ToolNotFoundError,
)

# Import storage functions using absolute package path
from family_assistant import storage
from sqlalchemy import text  # Added text import
from family_assistant.storage.context import (
    DatabaseContext,
    get_db_context,
)  # Import context
from family_assistant.storage import (
    get_all_notes,
    get_note_by_title,
    add_or_update_note,
    delete_note,
    store_incoming_email,
    get_grouped_message_history,
    get_all_tasks,
)

# Import protocol for type hinting when creating the dict for add_document
from family_assistant.storage.vector import (
    Document,
)

# Import vector search components
from family_assistant.storage.vector_search import (
    query_vector_store,
    VectorSearchQuery,
    MetadataFilter,
)

# Import embedding generator (adjust path based on actual location)
# Assuming it's accessible via a function or app state
from family_assistant.embeddings import (
    EmbeddingGenerator,
    LiteLLMEmbeddingGenerator,
)  # Example
from pydantic import BaseModel, Field  # For structuring results if needed, added Field

# Import tool functions directly from the tools package
from family_assistant.tools import (
    _scan_user_docs,  # Import the scanner function # Removed incorrect import of render_schema_as_html
)
from family_assistant.tools.schema import render_schema_as_html  # Correct import path

logger = logging.getLogger(__name__)

# Simple in-memory cache for rendered tool schema HTML, keyed by tool name
_tool_html_cache: Dict[str, str] = {}

# Directory to save raw webhook request bodies for debugging/replay
MAILBOX_RAW_DIR = "/mnt/data/mailbox/raw_requests"  # TODO: Consider making this configurable via env var

# Load server URL from environment variable (used in templates)
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")

app = FastAPI(
    title="Family Assistant Web Interface",
    docs_url="/api/docs",  # URL for Swagger UI
    redoc_url="/api/redoc",  # URL for ReDoc
)


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

    # Mount static files using the calculated path
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Templates directory set to: {templates_dir}")
    logger.info(f"Static files directory set to: {static_dir}")
    logger.info(f"User docs directory set to: {docs_user_dir}")

except NameError:
    # __file__ might not be defined in some execution contexts (e.g., interactive)
    logger.error(
        "Could not determine package path using __file__. Static/template files might not load."
    )
    # Provide fallback paths relative to CWD, although this might not work reliably
    templates = Jinja2Templates(directory="src/family_assistant/templates")
    app.mount(
        "/static", StaticFiles(directory="src/family_assistant/static"), name="static"
    )
    # Fallback docs path relative to CWD
    docs_user_dir = pathlib.Path("docs") / "user"
    logger.warning(f"Using fallback user docs directory: {docs_user_dir}")

# Markdown renderer instance
md_renderer = MarkdownIt("gfm-like")  # Use GitHub Flavored Markdown preset


# --- Dependency for Database Context ---
async def get_db() -> DatabaseContext:
    """FastAPI dependency to get a DatabaseContext."""
    async with await get_db_context() as db_context:
        yield db_context


# --- Placeholder for Embedding Generator Dependency ---
# NOTE: Replace this with your actual dependency injection logic.
# How is the embedding generator configured and made available?
# Example: If it's stored in app.state after creation in main.py:
# async def get_embedding_generator_dependency(request: Request) -> EmbeddingGenerator:
#     generator = request.app.state.embedding_generator
#     if not generator:
#         raise HTTPException(status_code=500, detail="Embedding generator not configured")
#     return generator
# Dependency function to retrieve the embedding generator from app state
async def get_embedding_generator_dependency(request: Request) -> EmbeddingGenerator:
    """Retrieves the configured EmbeddingGenerator instance from app state."""
    generator = getattr(request.app.state, "embedding_generator", None)
    if not generator:
        logger.error("Embedding generator not found in app state.")
        # Raise HTTPException so FastAPI returns a proper error response
        raise HTTPException(
            status_code=500, detail="Embedding generator not configured or available."
        )
    if not isinstance(generator, EmbeddingGenerator):
        logger.error(
            f"Object in app state is not an EmbeddingGenerator: {type(generator)}"
        )
        raise HTTPException(
            status_code=500, detail="Invalid embedding generator configuration."
        )
    return generator


# Dependency function to retrieve the ToolsProvider instance from app state
async def get_tools_provider_dependency(request: Request) -> ToolsProvider:
    """Retrieves the configured ToolsProvider instance from app state."""
    provider = getattr(request.app.state, "tools_provider", None)
    if not provider:
        logger.error("ToolsProvider not found in app state.")
        raise HTTPException(
            status_code=500, detail="ToolsProvider not configured or available."
        )
    # Optional: Check if it adheres to the protocol (runtime check might be complex)
    # if not isinstance(provider, ToolsProvider): # This check might fail with protocols
    #     logger.error(f"Object in app state is not a ToolsProvider: {type(provider)}")
    #     raise HTTPException(status_code=500, detail="Invalid ToolsProvider configuration.")
    return provider


# Markdown renderer instance
md_renderer = MarkdownIt("gfm-like")  # Use GitHub Flavored Markdown preset


# --- Pydantic model for search results (optional but good practice) ---
class SearchResultItem(BaseModel):
    embedding_id: int
    document_id: int
    title: Optional[str]
    source_type: str
    source_id: Optional[str] = None
    source_uri: Optional[str] = None
    created_at: Optional[datetime]
    embedding_type: str
    embedding_source_content: Optional[str]
    chunk_index: Optional[int] = None
    doc_metadata: Optional[Dict[str, Any]] = None
    distance: Optional[float] = None
    fts_score: Optional[float] = None
    rrf_score: Optional[float] = None

    class Config:
        orm_mode = True  # Allows creating from ORM-like objects (dict-like rows)


# --- Pydantic model for API response ---
class DocumentUploadResponse(BaseModel):
    message: str
    document_id: int
    task_enqueued: bool


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, db_context: DatabaseContext = Depends(get_db)):
    """Serves the main page listing all notes."""
    notes = await get_all_notes(db_context)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "notes": notes,
            "server_url": SERVER_URL,
        },  # Pass SERVER_URL
    )


@app.get("/notes/add", response_class=HTMLResponse)
async def add_note_form(request: Request):
    """Serves the form to add a new note."""
    return templates.TemplateResponse(
        "edit_note.html", {"request": request, "note": None, "is_new": True}
    )


@app.get("/notes/edit/{title}", response_class=HTMLResponse)
async def edit_note_form(
    request: Request, title: str, db_context: DatabaseContext = Depends(get_db)
):
    """Serves the form to edit an existing note."""
    note = await get_note_by_title(db_context, title)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return templates.TemplateResponse(
        "edit_note.html", {"request": request, "note": note, "is_new": False}
    )


@app.post("/notes/save")
async def save_note(
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    original_title: Optional[str] = Form(None),
    db_context: DatabaseContext = Depends(get_db),  # Add dependency
):
    """Handles saving a new or updated note."""
    try:
        if original_title and original_title != title:
            # Title changed - need to delete old and add new (or implement rename)
            # Simple approach: delete old, add new
            await delete_note(db_context, original_title)  # Pass context
            await add_or_update_note(db_context, title, content)  # Pass context
            logger.info(
                f"Renamed note '{original_title}' to '{title}' and updated content."
            )
        else:
            # New note or updating existing without title change
            await add_or_update_note(db_context, title, content)  # Pass context
            logger.info(f"Saved note: {title}")
        return RedirectResponse(url="/", status_code=303)  # Redirect back to list
    except Exception as e:
        logger.error(f"Error saving note '{title}': {e}", exc_info=True)
        # You might want to return an error page instead
        raise HTTPException(status_code=500, detail=f"Failed to save note: {e}")


@app.post("/notes/delete/{title}")
async def delete_note_post(
    request: Request, title: str, db_context: DatabaseContext = Depends(get_db)
):
    """Handles deleting a note."""
    deleted = await delete_note(db_context, title)
    if not deleted:
        raise HTTPException(status_code=404, detail="Note not found for deletion")
    return RedirectResponse(url="/", status_code=303)  # Redirect back to list


@app.post("/webhook/mail")
async def handle_mail_webhook(
    request: Request, db_context: DatabaseContext = Depends(get_db)
):
    """
    Receives incoming email via webhook (expects multipart/form-data).
    Logs the received form data for now.
    """
    logger.info("Received POST request on /webhook/mail")
    try:
        # --- Save raw request body for debugging/replay ---
        raw_body = await request.body()
        try:
            os.makedirs(MAILBOX_RAW_DIR, exist_ok=True)
            # Use timestamp for filename, as parsing form data might consume the body stream
            # depending on the framework version/internals. Reading body first is safer.
            now = datetime.now(timezone.utc)
            timestamp_str = now.strftime("%Y%m%d_%H%M%S_%f")
            # Sanitize content-type for filename part if available
            content_type = request.headers.get("content-type", "unknown_content_type")
            safe_content_type = (
                re.sub(r'[<>:"/\\|?*]', "_", content_type).split(";")[0].strip()
            )  # Get main type
            filename = f"{timestamp_str}_{safe_content_type}.raw"
            filepath = os.path.join(MAILBOX_RAW_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(raw_body)
            logger.info(
                f"Saved raw webhook request body ({len(raw_body)} bytes) to: {filepath}"
            )
        except Exception as e:
            # Log error but don't fail the request processing
            logger.error(f"Failed to save raw webhook request body: {e}", exc_info=True)
        # --- End raw request saving ---

        # Mailgun sends data as multipart/form-data
        form_data = await request.form()
        await store_incoming_email(
            db_context, dict(form_data)
        )  # Pass context and parsed form data
        # TODO: Add logic here to parse/store email content or trigger LLM processing
        # -----------------------------------------

        return Response(status_code=200, content="Email received.")
    except Exception as e:
        logger.error(f"Error processing mail webhook: {e}", exc_info=True)
        # Return 500, Mailgun might retry
        raise HTTPException(status_code=500, detail="Failed to process incoming email")


@app.get("/history", response_class=HTMLResponse)
async def view_message_history(
    request: Request,
    db_context: DatabaseContext = Depends(get_db),
    page: int = Query(1, ge=1, description="Page number for message history"),
    per_page: int = Query(
        10, ge=1, le=100, description="Number of conversations per page"
    ),
):
    """Serves the page displaying message history."""
    try:
        history_by_chat = await get_grouped_message_history(db_context)

        # --- Pagination Logic ---
        # Convert dict items to a list for slicing. Note: Dict order is not guaranteed
        # before Python 3.7, but generally insertion order from 3.7+.
        # If a specific order of *conversations* is needed (e.g., by most recent message),
        # more complex sorting would be required here *before* pagination.
        all_items = list(history_by_chat.items())
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
            {"request": request, "paged_items": paged_items, "pagination": pagination_info}, # Pass paginated items and info
        )
    except Exception as e:
        logger.error(f"Error fetching message history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch message history")


@app.get("/tools", response_class=HTMLResponse)
async def view_tools(request: Request):
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
            "tools.html", {"request": request, "tools": rendered_tools}
        )
    except Exception as e:
        logger.error(f"Error fetching tool definitions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch tool definitions")


@app.get("/tasks", response_class=HTMLResponse)
async def view_tasks(request: Request, db_context: DatabaseContext = Depends(get_db)):
    """Serves the page displaying scheduled tasks."""
    try:
        tasks = await get_all_tasks(db_context, limit=200)  # Pass context, fetch tasks
        return templates.TemplateResponse(
            "tasks.html",
            {
                "request": request,
                "tasks": tasks,
                # Add json filter to Jinja environment if not default
                # Pass 'tojson' filter if needed explicitly, or handle in template
                # jinja_env.filters['tojson'] = json.dumps # Example
            },
        )
    except Exception as e:
        logger.error(f"Error fetching tasks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch tasks")


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check(request: Request):
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
    request: Request, db_context: DatabaseContext = Depends(get_db)
):
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

        models_result, types_result, source_types_result, meta_keys_result = (
            await asyncio.gather(
                db_context.fetch_all(q_models),
                db_context.fetch_all(q_types),
                db_context.fetch_all(q_source_types),
                db_context.fetch_all(q_meta_keys),  # Fetch metadata keys
            )
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
        },
    )


@app.post("/vector-search", response_class=HTMLResponse)
async def handle_vector_search(
    request: Request,
    # --- Form Inputs ---
    semantic_query: Optional[str] = Form(None),
    keywords: Optional[str] = Form(None),
    search_type: str = Form("hybrid"),  # 'semantic', 'keyword', 'hybrid'
    embedding_model: Optional[str] = Form(None),  # CRUCIAL for vector search
    embedding_types: List[str] = Form([]),  # Allow multiple types
    source_types: List[str] = Form([]),  # Allow multiple source types
    created_after: Optional[str] = Form(None),  # Expect YYYY-MM-DD
    created_before: Optional[str] = Form(None),  # Expect YYYY-MM-DD
    title_like: Optional[str] = Form(None),
    # --- Metadata Filters (expect lists) ---
    metadata_keys: List[str] = Form([]),
    metadata_values: List[str] = Form([]),
    # --- Control Params ---
    limit: int = Form(10),
    rrf_k: int = Form(60),
    # --- Dependencies ---
    db_context: DatabaseContext = Depends(get_db),
    embedding_generator: EmbeddingGenerator = Depends(
        get_embedding_generator_dependency
    ),
):
    """Handles the vector search form submission."""
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
        created_after_dt: Optional[datetime] = None
        if created_after:
            try:
                # Assume YYYY-MM-DD, make it timezone-aware (start of day UTC)
                created_after_dt = datetime.strptime(created_after, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                raise ValueError("Invalid 'Created After' date format. Use YYYY-MM-DD.")

        created_before_dt: Optional[datetime] = None
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
                )

        # --- Build List of Metadata Filters ---
        metadata_filters_list: List[MetadataFilter] = []
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
            for key, value in zip(metadata_keys, metadata_values):
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
            # if embedding_generator.model_name != query_obj.embedding_model:
            #      logger.warning(f"Selected model '{query_obj.embedding_model}' might differ from generator '{embedding_generator.model_name}'. Ensure compatibility.")
            #      # Ideally, you'd select the generator based on the model chosen in the form.
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
        models_result, types_result, source_types_result, meta_keys_result = (
            await asyncio.gather(
                db_context.fetch_all(q_models),
                db_context.fetch_all(q_types),
                db_context.fetch_all(q_source_types),
                db_context.fetch_all(q_meta_keys),
            )
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
        },
    )


# Need asyncio for gather
import asyncio


# --- Tool Execution API ---
class ToolExecutionRequest(BaseModel):
    arguments: Dict[str, Any]


@app.post("/api/tools/execute/{tool_name}", response_class=JSONResponse)
async def execute_tool_api(
    tool_name: str,
    request: Request,  # Keep request for potential context later
    payload: ToolExecutionRequest,
    tools_provider: ToolsProvider = Depends(get_tools_provider_dependency),
    db_context: DatabaseContext = Depends(get_db),  # Inject DB context if tools need it
    # embedding_generator: EmbeddingGenerator = Depends(get_embedding_generator_dependency), # Inject if tools need it
):
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
            try:
                final_result = json.loads(result)
            except json.JSONDecodeError:
                pass  # Not a JSON string, keep original result
        return JSONResponse(
            content={"success": True, "result": final_result}, status_code=200
        )
    except ToolNotFoundError:
        logger.warning(f"Tool '{tool_name}' not found for execution request.")
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found.")
    except (
        ValidationError
    ) as ve:  # Catch Pydantic validation errors if execute_tool raises them
        logger.warning(f"Argument validation error for tool '{tool_name}': {ve}")
        raise HTTPException(
            status_code=400, detail=f"Invalid arguments for tool '{tool_name}': {ve}"
        )
    except (
        TypeError
    ) as te:  # Catch potential argument mismatches within the tool function
        logger.error(
            f"Type error during execution of tool '{tool_name}': {te}", exc_info=True
        )
        raise HTTPException(
            status_code=400,
            detail=f"Argument mismatch or type error in tool '{tool_name}': {te}",
        )
    except Exception as e:
        logger.error(f"Error executing tool '{tool_name}': {e}", exc_info=True)
        # Avoid leaking internal error details unless intended
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred while executing tool '{tool_name}'.",
        )


# --- API Routes ---


@app.post(
    "/api/documents/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload and index a document",
    description="Accepts document metadata and content parts via multipart/form-data, "
    "stores the document record, and enqueues a background task for embedding generation.",
)
async def upload_document(
    # Required fields
    source_type: str = Form(
        ...,
        description="Type of the source (e.g., 'manual_upload', 'scanned_receipt').",
    ),
    source_id: str = Form(
        ..., description="Unique identifier for the document within its source type."
    ),
    content_parts_json: str = Form(
        ...,
        alias="content_parts",  # Allow form field name 'content_parts'
        description='JSON string representing a dictionary of content parts to be indexed. Keys determine embedding type (e.g., {"title": "Doc Title", "content_chunk_0": "First paragraph..."}).',
    ),
    # Optional fields
    source_uri: Optional[str] = Form(
        None, description="Canonical URI/URL of the original document."
    ),
    title: Optional[str] = Form(
        None,
        description="Primary title for the document (can also be in content_parts).",
    ),
    created_at_str: Optional[str] = Form(
        None,
        alias="created_at",  # Allow form field name 'created_at'
        description="Original creation timestamp (ISO 8601 format string, e.g., 'YYYY-MM-DDTHH:MM:SSZ' or 'YYYY-MM-DD'). Timezone assumed UTC if missing.",
    ),
    metadata_json: Optional[str] = Form(
        None,
        alias="metadata",  # Allow form field name 'metadata'
        description="JSON string representing a dictionary of additional metadata.",
    ),
    # Dependencies
    db_context: DatabaseContext = Depends(get_db),
):
    """
    API endpoint to upload document metadata and content parts for indexing.
    """
    logger.info(
        f"Received document upload request for source_id: {source_id} (type: {source_type})"
    )

    # --- 1. Parse and Validate Inputs ---
    try:
        # Parse JSON strings
        content_parts: Dict[str, str] = json.loads(content_parts_json)
        if not isinstance(content_parts, dict) or not content_parts:
            raise ValueError("'content_parts' must be a non-empty JSON object string.")
        # Validate content parts values are strings
        for key, value in content_parts.items():
            if not isinstance(value, str):
                raise ValueError(f"Value for content part '{key}' must be a string.")

        doc_metadata: Dict[str, Any] = {}
        if metadata_json:
            doc_metadata = json.loads(metadata_json)
            if not isinstance(doc_metadata, dict):
                raise ValueError(
                    "'metadata' must be a valid JSON object string if provided."
                )

        # Parse date string (handle date vs datetime)
        created_at_dt: Optional[datetime] = None
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
                    )

    except json.JSONDecodeError as json_err:
        logger.error(f"JSON parsing error for document upload {source_id}: {json_err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON format: {json_err}",
        )
    except ValueError as val_err:
        logger.error(f"Validation error for document upload {source_id}: {val_err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(val_err)
        )
    except Exception as e:
        logger.error(
            f"Unexpected parsing error for document upload {source_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing request data.",
        )

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
        def __init__(self, data):
            self._data = data

        @property
        def source_type(self) -> str:
            return self._data["_source_type"]

        @property
        def source_id(self) -> str:
            return self._data["_source_id"]

        @property
        def source_uri(self) -> Optional[str]:
            return self._data["_source_uri"]

        @property
        def title(self) -> Optional[str]:
            return self._data["_title"]

        @property
        def created_at(self) -> Optional[datetime]:
            return self._data["_created_at"]

        @property
        def metadata(self) -> Optional[Dict[str, Any]]:
            return self._data["_base_metadata"]

    doc_for_storage = UploadedDocument(document_data)

    try:
        document_id = await storage.add_document(
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
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error storing document.",
        )

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
            # notify_event=new_task_event # Optional: trigger worker immediately if event is accessible
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
async def redirect_to_user_guide():
    """Redirects the base /docs/ path to the main user guide."""
    return RedirectResponse(
        url="/docs/USER_GUIDE.md", status_code=status.HTTP_302_FOUND
    )


@app.get("/docs/{filename:path}", response_class=HTMLResponse)
async def serve_documentation(request: Request, filename: str):
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
        async with aiofiles.open(doc_path, mode="r", encoding="utf-8") as f:
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
        )
    except Exception as e:
        logger.error(
            f"Error serving documentation file '{filename}': {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail="Error rendering documentation.")


# --- Uvicorn Runner (for standalone testing) ---
if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Uvicorn server for testing...")
    # Make sure embedding generator is available in app state if running standalone
    # Example placeholder:
    # app.state.embedding_generator = MockEmbeddingGenerator({}, default_embedding=[0.0]*10)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
