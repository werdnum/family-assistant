import logging
import os
import re
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import List, Dict, Optional, Any # Added Any
from fastapi import Response  # Added Response
from datetime import datetime, timezone
import json
import pathlib  # Import pathlib for finding template/static dirs
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Import storage functions using absolute package path
from family_assistant import storage
from sqlalchemy import text # Added text import
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
# Import vector search components
from family_assistant.storage.vector_search import (
    query_vector_store,
    VectorSearchQuery,
    MetadataFilter,
)
# Import embedding generator (adjust path based on actual location)
# Assuming it's accessible via a function or app state
from family_assistant.embeddings import EmbeddingGenerator, LiteLLMEmbeddingGenerator # Example
from pydantic import BaseModel # For structuring results if needed

logger = logging.getLogger(__name__)

# Directory to save raw webhook request bodies for debugging/replay
MAILBOX_RAW_DIR = "/mnt/data/mailbox/raw_requests"  # TODO: Consider making this configurable via env var

app = FastAPI(title="Family Assistant Web Interface")

# --- Determine base path for templates and static files ---
# This assumes web_server.py is at src/family_assistant/web_server.py
# We want the paths relative to the 'family_assistant' package directory
try:
    # Get the directory containing the current file (web_server.py)
    current_file_dir = pathlib.Path(__file__).parent.resolve()
    # Go up one level to the package root (src/family_assistant/)
    package_root_dir = current_file_dir
    # Define template and static directories relative to the package root
    templates_dir = package_root_dir / "templates"
    static_dir = package_root_dir / "static"

    if not templates_dir.is_dir():
        logger.warning(
            f"Templates directory not found at expected location: {templates_dir}"
        )
        # Fallback or raise error? For now, log warning.
    if not static_dir.is_dir():
        logger.warning(f"Static directory not found at expected location: {static_dir}")
        # Fallback or raise error?

    # Configure templates using the calculated path
    templates = Jinja2Templates(directory=templates_dir)
    # Add the 'tojson' filter to the Jinja environment
    templates.env.filters['tojson'] = json.dumps

    # Mount static files using the calculated path
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Templates directory set to: {templates_dir}")
    logger.info(f"Static files directory set to: {static_dir}")

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
        raise HTTPException(status_code=500, detail="Embedding generator not configured or available.")
    if not isinstance(generator, EmbeddingGenerator):
         logger.error(f"Object in app state is not an EmbeddingGenerator: {type(generator)}")
         raise HTTPException(status_code=500, detail="Invalid embedding generator configuration.")
    return generator


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
        orm_mode = True # Allows creating from ORM-like objects (dict-like rows)


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, db_context: DatabaseContext = Depends(get_db)):
    """Serves the main page listing all notes."""
    notes = await get_all_notes(db_context)
    return templates.TemplateResponse(
        "index.html", {"request": request, "notes": notes}
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
    request: Request, db_context: DatabaseContext = Depends(get_db)
):
    """Serves the page displaying message history."""
    try:
        history_by_chat = await get_grouped_message_history(db_context)
        # Optional: Sort chats by ID if needed (DB query already sorts)
        # history_by_chat = dict(sorted(history_by_chat.items()))
        return templates.TemplateResponse(
            "message_history.html",
            {"request": request, "history_by_chat": history_by_chat},
        )
    except Exception as e:
        logger.error(f"Error fetching message history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch message history")


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


@app.get("/health", status_code=200)
async def health_check():
    """Basic health check endpoint."""
    return {"status": "ok"}


# --- Vector Search Routes ---

@app.get("/vector-search", response_class=HTMLResponse)
async def vector_search_form(request: Request, db_context: DatabaseContext = Depends(get_db)):
    """Serves the vector search form."""
    distinct_models = []
    distinct_types = []
    distinct_source_types = []
    distinct_metadata_keys = [] # Added for metadata keys
    error = None
    try:
        # Fetch distinct values for dropdowns/filters
        # Ensure table/column names match your actual schema
        q_models = text("SELECT DISTINCT embedding_model FROM document_embeddings ORDER BY embedding_model;")
        q_types = text("SELECT DISTINCT embedding_type FROM document_embeddings ORDER BY embedding_type;")
        q_source_types = text("SELECT DISTINCT source_type FROM documents ORDER BY source_type;")
        # Query to get distinct top-level keys from the JSONB metadata column
        q_meta_keys = text("SELECT DISTINCT key FROM documents, jsonb_object_keys(doc_metadata) AS keys(key) ORDER BY key;")


        models_result, types_result, source_types_result, meta_keys_result = await asyncio.gather(
            db_context.fetch_all(q_models),
            db_context.fetch_all(q_types),
            db_context.fetch_all(q_source_types),
            db_context.fetch_all(q_meta_keys) # Fetch metadata keys
        )

        distinct_models = [row["embedding_model"] for row in models_result]
        distinct_types = [row["embedding_type"] for row in types_result]
        distinct_source_types = [row["source_type"] for row in source_types_result]
        distinct_metadata_keys = [row["key"] for row in meta_keys_result] # Populate metadata keys

    except Exception as e:
        logger.error(f"Failed to fetch distinct values for search form: {e}", exc_info=True)
        error = "Could not load filter options from database."
        # Continue without pre-populated dropdowns

    return templates.TemplateResponse(
        "vector_search.html",
        {
            "request": request,
            "results": None,
            "search_params": {}, # Empty for initial GET
            "error": error,
            "distinct_models": distinct_models,
            "distinct_types": distinct_types,
            "distinct_source_types": distinct_source_types,
            "distinct_metadata_keys": distinct_metadata_keys, # Pass keys to template
        },
    )

@app.post("/vector-search", response_class=HTMLResponse)
async def handle_vector_search(
    request: Request,
    # --- Form Inputs ---
    semantic_query: Optional[str] = Form(None),
    keywords: Optional[str] = Form(None),
    search_type: str = Form("hybrid"), # 'semantic', 'keyword', 'hybrid'
    embedding_model: Optional[str] = Form(None), # CRUCIAL for vector search
    embedding_types: List[str] = Form([]), # Allow multiple types
    source_types: List[str] = Form([]), # Allow multiple source types
    created_after: Optional[str] = Form(None), # Expect YYYY-MM-DD
    created_before: Optional[str] = Form(None), # Expect YYYY-MM-DD
    title_like: Optional[str] = Form(None),
    # --- Metadata Filters (expect lists) ---
    metadata_keys: List[str] = Form([]),
    metadata_values: List[str] = Form([]),
    # --- Control Params ---
    limit: int = Form(10),
    rrf_k: int = Form(60),
    # --- Dependencies ---
    db_context: DatabaseContext = Depends(get_db),
    embedding_generator: EmbeddingGenerator = Depends(get_embedding_generator_dependency),
):
    """Handles the vector search form submission."""
    results = None
    error = None
    query_embedding = None

    # --- Default keywords to semantic query if keywords are empty ---
    effective_keywords = keywords
    if not keywords and semantic_query:
        effective_keywords = semantic_query
        logger.info(f"Keywords field was empty, defaulting to semantic query: '{semantic_query}'")

    search_params = { # Store params to repopulate form
        "semantic_query": semantic_query, "keywords": keywords, # Store original keywords for form
        "search_type": search_type,
        "embedding_model": embedding_model, "embedding_types": embedding_types,
        "source_types": source_types, "created_after": created_after,
        "created_before": created_before, "title_like": title_like,
        # Store lists for metadata filters
        "metadata_keys": metadata_keys, "metadata_values": metadata_values,
        "limit": limit, "rrf_k": rrf_k,
    }
    distinct_models = [] # Fetch again or pass from GET state if possible
    distinct_types = []
    distinct_source_types = []
    distinct_metadata_keys = [] # Fetch again

    try:
        # --- Parse Dates (handle potential errors) ---
        created_after_dt: Optional[datetime] = None
        if created_after:
            try:
                # Assume YYYY-MM-DD, make it timezone-aware (start of day UTC)
                created_after_dt = datetime.strptime(created_after, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                raise ValueError("Invalid 'Created After' date format. Use YYYY-MM-DD.")

        created_before_dt: Optional[datetime] = None
        if created_before:
            try:
                 # Assume YYYY-MM-DD, make it timezone-aware (end of day UTC)
                 # Add 1 day and subtract epsilon or use < comparison in SQL
                 # Simpler: use the date directly, SQL query uses <=
                created_before_dt = datetime.strptime(created_before, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                raise ValueError("Invalid 'Created Before' date format. Use YYYY-MM-DD.")

        # --- Build List of Metadata Filters ---
        metadata_filters_list: List[MetadataFilter] = []
        if len(metadata_keys) != len(metadata_values):
            # This indicates a potential issue with form submission or client-side JS
            logger.error(f"Mismatch between metadata keys ({len(metadata_keys)}) and values ({len(metadata_values)}). Ignoring metadata filters.")
            error = "Error: Mismatch in metadata filter keys and values."
            # Optionally clear the lists to prevent partial filtering
            metadata_keys = []
            metadata_values = []
        else:
            for key, value in zip(metadata_keys, metadata_values):
                if key and value is not None: # Allow empty string value, but require key
                    metadata_filters_list.append(MetadataFilter(key=key, value=value))
                elif key and value is None:
                     logger.warning(f"Metadata key '{key}' provided without a value. Ignoring this filter.")
                # No warning needed if key is empty, as it's likely from an empty template row


        # --- Create Query Object ---
        # Validation is handled by the dataclass __post_init__
        query_obj = VectorSearchQuery(
            search_type=search_type,
            semantic_query=semantic_query,
            keywords=effective_keywords, # Use the potentially defaulted keywords
            embedding_model=embedding_model,
            embedding_types=embedding_types,
            source_types=source_types,
            created_after=created_after_dt,
            created_before=created_before_dt,
            title_like=title_like,
            metadata_filters=metadata_filters_list, # Pass the list
            limit=limit,
            rrf_k=rrf_k,
        )

        # --- Generate Embedding ---
        if query_obj.search_type in ["semantic", "hybrid"]:
            # Basic check, might need more robust model matching/selection
            # if embedding_generator.model_name != query_obj.embedding_model:
            #      logger.warning(f"Selected model '{query_obj.embedding_model}' might differ from generator '{embedding_generator.model_name}'. Ensure compatibility.")
            #      # Ideally, you'd select the generator based on the model chosen in the form.
            embedding_result = await embedding_generator.generate_embeddings([query_obj.semantic_query]) # Pass as list
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
        q_models = text("SELECT DISTINCT embedding_model FROM document_embeddings ORDER BY embedding_model;")
        q_types = text("SELECT DISTINCT embedding_type FROM document_embeddings ORDER BY embedding_type;")
        q_source_types = text("SELECT DISTINCT source_type FROM documents ORDER BY source_type;")
        q_meta_keys = text("SELECT DISTINCT key FROM documents, jsonb_object_keys(doc_metadata) AS keys(key) ORDER BY key;")

        # Use asyncio.gather for concurrent fetching
        models_result, types_result, source_types_result, meta_keys_result = await asyncio.gather(
            db_context.fetch_all(q_models),
            db_context.fetch_all(q_types),
            db_context.fetch_all(q_source_types),
            db_context.fetch_all(q_meta_keys)
        )

        distinct_models = [row["embedding_model"] for row in models_result]
        distinct_types = [row["embedding_type"] for row in types_result]
        distinct_source_types = [row["source_type"] for row in source_types_result]
        distinct_metadata_keys = [row["key"] for row in meta_keys_result] # Get keys

    except Exception as e:
        logger.error(f"Failed to fetch distinct values for search form render: {e}", exc_info=True)
        # Don't overwrite previous error, but log this one
        if not error:
            error = "Could not load filter options from database."


    return templates.TemplateResponse(
        "vector_search.html",
        {
            "request": request,
            "results": results,
            "search_params": search_params, # Pass back params
            "error": error,
            "distinct_models": distinct_models,
            "distinct_types": distinct_types,
            "distinct_source_types": distinct_source_types,
            "distinct_metadata_keys": distinct_metadata_keys, # Pass keys
        },
    )

# Need asyncio for gather
import asyncio


# --- Uvicorn Runner (for standalone testing) ---
if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Uvicorn server for testing...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
