# Family Assistant LLM Application - Specification

## 1. Introduction

### 1.1 Purpose
This document outlines the specification for an LLM-based family assistant application. The assistant aims to centralize family-related information, provide proactive updates, handle specific requests from family members, and automate certain information management tasks.

### 1.2 Inspiration
The design is inspired by Geoffrey Litt's ["Stevens" assistant](https://www.geoffreylitt.com/2025/04/12/how-i-made-a-useful-ai-assistant-with-one-sqlite-table-and-a-handful-of-cron-jobs), particularly its use of a central log/memory and cron-based interactions, but adapted for potentially more structured data storage and multi-interface access.

### 1.3 Scope
The system will provide a conversational interface primarily through Telegram, with additional access via Email and a Web interface. It will manage information like calendar events, reminders, and user-provided facts, and perform tasks based on direct requests, ingested information, or scheduled triggers. It will leverage the Model Context Protocol (MCP) for extensibility.

## 2. Users & Interfaces

### 2.1 Target Users
Family members who need a centralized way to manage shared information and receive timely updates.

### 2.2 Interfaces
*   **Telegram (Primary):** A Telegram bot will serve as the main interaction point for direct requests, receiving updates, and potentially some forms of information ingestion (e.g., forwarding messages).
*   **Email (Secondary):**
    *   Users can forward emails (e.g., confirmations, invites) to a dedicated address for ingestion.
    *   The assistant might send certain notifications or summaries via email (TBD).
*   **Web (Secondary):** A web interface (implemented using FastAPI and Jinja2) provides:
    *   A UI to view, add, edit, and delete notes (`/`, `/notes/add`, `/notes/edit/{title}`).
    *   A view of recent message history (`/history`).
    *   A view of recent background tasks (`/tasks`), with the ability to manually retry failed tasks.
    *   A vector search interface (`/vector-search`) to query indexed documents, view results grouped by document, and access a detailed document view (`/vector-search/document/{document_id}`).
    *   A UI for document upload (`/documents/upload`).
    *   A UI for API Token Management (`/settings/tokens`).
    *   (Future) A dashboard view of upcoming events, reminders, etc.
    *   (Future) An alternative way to interact with the assistant (chat interface).
    *   (Future) Configuration options.
*   **Email (Webhook):** Receives emails via a webhook (e.g., from Mailgun) at `/webhook/mail`. Parsed email data and attachments are stored, and an `index_email` task is enqueued for further processing by the indexing pipeline, which includes LLM-based primary link extraction.

## 3. Architecture Overview

```mermaid
graph TD
    subgraph User Interfaces
        UI_TG[Telegram Bot]
        UI_Web[Web UI (FastAPI)]
        UI_Email[Email (Webhook/Future Send)]
    end

    subgraph Core Application
        Interaction[Interaction Layer]
        Processing[Processing Layer (LLM, Tools)]
        DataStore[Data Store (SQLAlchemy + DB)]
        MCP[MCP Integration Layer]
        TaskWorker[Task Worker (DB Queue)]
        Config[Configuration (.env, .yaml, .json)]
    end

    subgraph External Services
        Ext_LLM[LLM Service (OpenRouter)]
        Ext_DB[Database (PostgreSQL/SQLite)]
        Ext_MCP[MCP Servers (Time, Fetch, etc.)]
        Ext_Calendar[Calendars (CalDAV/iCal)]
        Ext_EmailProvider[Email Provider (e.g., Mailgun)]
    end

    UI_TG --> Interaction
    UI_Web --> Interaction
    UI_Email --> Interaction

    Interaction -- User Input --> Processing
    Processing -- Responses --> Interaction

    Processing -- Uses Tools --> MCP
    Processing -- Uses Tools --> DataStore
    Processing -- Calls --> Ext_LLM
    Processing -- Reads/Writes --> Ext_Calendar

    TaskWorker -- Reads/Writes --> DataStore
    TaskWorker -- Triggers --> Processing

    MCP -- Connects To --> Ext_MCP

    DataStore -- Connects To --> Ext_DB

    UI_Web -- Reads/Writes --> DataStore

    UI_Email -- Webhook --> Ext_EmailProvider
    Ext_EmailProvider -- Webhook --> Interaction

    Config -- Loaded By --> Core Application

    style Core Application fill:#f9f,stroke:#333,stroke-width:2px
    style User Interfaces fill:#ccf,stroke:#333,stroke-width:2px
    style External Services fill:#cfc,stroke:#333,stroke-width:2px
```

*   **Interaction Layer:** Manages communication across Telegram, Email, and Web interfaces. It receives user input, forwards it for processing, and delivers responses/updates back to the user via the appropriate channel.
*   **Processing Layer:**
    *   Utilizes a Large Language Model (LLM) (e.g., Claude, GPT) via LiteLLM to understand natural language requests, extract information from ingested data, generate summaries/briefs, and formulate responses.
    *   Manages the definition and execution logic for tools (like `add_or_update_note`) that the LLM can use.
    *   Leverages MCP tools provided by connected servers to perform actions or retrieve external context (Future).
    *   Includes specific logic for parsing structured data where possible (e.g., calendar invites, specific email formats) to complement LLM extraction (Future).
*   **Data Store:** A central repository (a structured database, e.g., PostgreSQL or SQLite, accessed via **SQLAlchemy**) storing:
    *   Events (calendar items, deadlines)
    *   Facts/Memories (user-provided info, ingested details)
    *   User profiles and preferences
    *   System configuration
    *   Logs of interactions (`message_history` table).
    *   Notes (`notes` table).
    *   *Note: Reminders are intended to be stored on a dedicated calendar, not in this database.*
*   **MCP Integration Layer:** Connects to MCP servers defined in `mcp_config.json` (e.g., Time, Browser, Fetch, Brave Search). Discovers available tools, provides them to the LLM, and executes requested MCP tool calls.
*   **Task Scheduler (Future):** Manages cron jobs or scheduled tasks for:
    *   Periodic data ingestion (e.g., checking main calendars, weather APIs).
    *   Checking the reminder calendar for due items.
    *   Proactive updates (e.g., generating and sending the daily brief).
    *   Maintenance tasks (e.g., data cleanup).

### 3.1 Data Flow Example (Daily Brief)
1.  **Task Scheduler** triggers the "Daily Brief" task.
2.  **Processing Layer** queries the **Data Store** for relevant facts/memories and external sources (e.g., main calendar API, reminder calendar API, weather API via MCP or direct call).
3.  **Processing Layer** sends the collected information and a prompt to the **LLM** to generate a formatted, natural language brief.
4.  **LLM** returns the generated brief text.
5.  **Processing Layer** sends the brief to the **Interaction Layer**.
6.  **Interaction Layer** delivers the brief to designated users via **Telegram** (and potentially other configured interfaces).

## 4. Interaction Types

### 4.1 Direct Interaction
*   **Trigger:** User sends a message/command via Telegram, Web, or potentially Email.
*   **Process:**
    1.  **Interaction Layer** receives the request.
    2.  Request is passed to the **Processing Layer**.
    3.  **LLM** interprets the request. May involve querying the **Data Store**, querying/updating calendars, or invoking an **MCP tool** (e.g., "Turn on the lights").
    4.  If action involves data modification (Data Store or Calendar), **Processing Layer** performs the update.
    5.  **LLM** generates a response (potentially using results from data queries or tool calls).
    6.  **Interaction Layer** delivers the response.
*   **Examples:**
    *   "What's happening tomorrow?"
    *   "Remind me to call the plumber at 5 PM today." (Adds event to reminder calendar)
    *   "Add dentist appointment on June 5th at 10 AM." (Adds event to main calendar)
    *   "What was the flight number for the trip we booked?" (Requires prior ingestion into memories)
    *   "Turn on the living room lights." (Requires MCP integration with Home Assistant)
    *   Sending a photo with "What plant is this?"

### 4.2 Ingestion Interaction
*   **Trigger:** User forwards an email, shares a calendar event, uploads a file (via Web?), sends specific formatted info via chat, or sends a photo/document intended purely for logging/memory.
*   **Process:**
    1.  **Interaction Layer** (or dedicated ingestion service) receives the data. Note: Image attachments in regular messages are handled by Direct Interaction.
    2.  **Processing Layer** attempts to parse structured data first (if applicable format is known, e.g., `.ics` files).
    3.  If parsing fails or data is unstructured (e.g., plain email body), the **LLM** (Future) could be used to extract key information (dates, times, event names, confirmation numbers, etc.). Currently, raw email data is stored in the `received_emails` table.
    4.  Extracted/parsed information is structured and saved to the `memories` table in the **Data Store**, linked to the source and timestamp. If it's clearly a calendar event, it might also be added directly to the main calendar.
    5.  Optional: Assistant confirms successful ingestion via the **Interaction Layer**.
*   **Examples:**
    *   Forwarding a flight confirmation email.
    *   Forwarding an evite or calendar invite email.
    *   Sharing a `.ics` calendar file.
    *   Sending a message like "Log: Bought concert tickets for July 10th, order #12345".

### 4.3 Cron-based Interaction
*   **Trigger:** **Task Scheduler** initiates a predefined task.
*   **Process:**
    1.  **Processing Layer** executes the task logic.
    2.  Often involves querying the **Data Store** for relevant facts/memories and external sources (Calendars, Weather API via MCP or direct call).
    3.  **LLM** may be used to synthesize information into a user-friendly format (e.g., the daily brief).
    4.  **Interaction Layer** delivers the output (if any) to users.
*   **Examples:**
    *   Send daily morning brief (main calendar, reminder calendar, weather, specific memories).
    *   Send weekly summary of upcoming events.
    *   Check reminder calendar for due reminders and send notifications.
    *   Fetch weather forecast data and store it (or make it available via an internal MCP resource).

## 5. Key Features

*   **Daily Brief:** Customizable morning update including main calendar, reminder calendar, weather, and potentially package/mail info.
*   **Information Storage & Retrieval (Notes):**
    *   Store notes provided by users via the `add_or_update_note` tool or the Web UI into the `notes` table.
    *   Provide notes as context to the LLM.
    *   Answer questions based on stored notes.
*   **Message History:** Store conversation history per chat in the `message_history` table and use recent history as context for the LLM.
*   **MCP Tool Integration:** Leverage connected MCP servers (Time, Browser, Fetch, Brave Search) to perform actions requested by the LLM based on user prompts.
*   **Calendar Integration (Read-Only):**
    *   Reads upcoming events (today, tomorrow, next 14 days) from configured calendar sources.
    *   Supports **CalDAV** calendars via direct URLs. Configuration via `.env`: Requires `CALDAV_USERNAME`, `CALDAV_PASSWORD`, and `CALDAV_CALENDAR_URLS` (comma-separated list of direct URLs). `CALDAV_URL` is optional.
    *   Supports **iCalendar** URLs (`.ics`). Configuration via `.env`: Requires `ICAL_URLS` (comma-separated list of URLs).
    *   Provides a combined, sorted list of events as context within the system prompt to the LLM.
*   **Task Queue:** Uses the database (`tasks` table) for background processing. Supports scheduled tasks, immediate notification via `asyncio.Event`, and task retries with exponential backoff. Handles `log_message`, `llm_callback`, `index_email`, and `embed_and_store_batch` task types. Users can manually retry failed tasks via the Web UI. (See Section 10 for details).
*   **Document Ingestion and Vector Search:**
    *   Upload documents (including PDFs and web URLs) via API or Web UI.
    *   Indexing pipeline extracts text (e.g., from PDFs, fetched web pages), chunks content, generates embeddings, and stores them.
    *   Automatic title extraction for documents ingested from URLs if no title is provided.
    *   Vector search UI (`/vector-search`) allows querying the indexed documents, with results grouped by document and linking to a detailed document view.
*   **Email Ingestion and Processing:**
    *   Emails received via webhook are parsed, attachments stored, and an `index_email` task is enqueued.
    *   The indexing pipeline for emails includes an `LLMPrimaryLinkExtractorProcessor` to identify and extract primary URLs from email content, which can then be fetched and processed.
*   **API Token Management:**
    *   Users can create, view, and revoke API tokens through the Web UI (`/settings/tokens`).
    *   The system supports API authentication using these tokens.
*   **(Future) Calendar Integration (Write):**
    *   Introduce tools allowing the LLM to add or update events on specific calendars.
    *   This will require a more robust configuration system for calendars, allowing administrators to define multiple calendars with distinct purposes (e.g., "Main Family Calendar", "Work Calendar", "Kids Activities", "Reminders").
    *   The LLM will need context about these available calendars (names, descriptions of purpose) to choose the correct one when a user requests adding an event (e.g., "Add dentist appointment..." vs. "Remind me to...").
*   **(Future) Reminders:**
    *   Set reminders via natural language (stored on a dedicated 'Reminders' calendar, requiring write access and configuration as described above).
    *   Receive notifications for due reminders (likely requires a scheduled task to check the calendar).
*   **(Future) Email Ingestion:** Process information from forwarded emails.
*   **Email Storage:** Incoming emails received via webhook are parsed and stored in the database (basic storage, deeper processing/ingestion is Future).
*   **(Future) External Data Integration:** Fetch data like weather forecasts directly or via MCP.
## 6. Data Store Design Considerations

*   A structured relational database (e.g., SQLite, PostgreSQL) is recommended for easier querying and management.
*   Database interactions will be managed using **SQLAlchemy** as the ORM.
*   Implemented Tables:
    *   `notes`: Stores user-created notes (id, title, content, created\_at, updated\_at). Title is unique.
    *   `message_history`: Logs user and assistant messages per chat (chat\_id, message\_id, timestamp, role, content, tool\_calls\_info).
*   **Current `message_history` Schema (Post-Refactoring):**
    *   `internal_id`: BigInteger, primary key, auto-incrementing. A unique internal ID for every recorded message fragment.
    *   `interface_type`: String(50), non-nullable, indexed. Identifies the source interface (e.g., 'telegram', 'web', 'email').
    *   `conversation_id`: String(255), non-nullable, indexed. A generic identifier for the chat session (e.g., Telegram chat ID as string, web session UUID).
    *   `interface_message_id`: String(255), nullable, indexed. The message ID specific to the external interface (e.g., Telegram message ID, email Message-ID header). Null for intermediate agent messages.
    *   `turn_id`: String(36), nullable, indexed. A UUID linking all agent-generated messages (assistant requests, tool responses, final answer) within a single processing turn initiated by a user/system trigger. The trigger message itself would likely have `turn_id = NULL`.
    *   `thread_root_id`: BigInteger, nullable, indexed. Stores the `internal_id` of the very first message (typically the initial user prompt) that initiated the conversation thread. Allows linking multiple turns originating from the same starting point.
    *   `timestamp`: DateTime(timezone=True), non-nullable, indexed. Time the message was recorded.
    *   `role`: String(50), non-nullable. 'user', 'assistant', 'system', 'tool'.
    *   `content`: Text, nullable. Message content.

    *   `tool_calls`: JSONB, nullable. For 'assistant' role messages requesting tool execution (structured list of calls).
    *   `tool_call_id`: String(255), nullable, indexed. For 'tool' role messages, linking the response back to the specific `tool_calls` entry ID requested by the assistant.
    *   `reasoning_info`: JSONB, nullable. For 'assistant' role messages, storing LLM reasoning/usage data.
    *   `error_traceback`: Text, nullable. Stores error details if processing this message caused an error, or if this message represents an error itself.
*   **Other Implemented Tables:**
    *   `received_emails`: Stores details of emails received via webhook. Columns include (unchanged by history refactoring):
        *   `id`: Internal auto-incrementing ID.
        *   `message_id_header`: Unique identifier from the email's `Message-ID` header (indexed).
        *   `sender_address`: Envelope sender address (e.g., from Mailgun's `sender` field, indexed).
        *   `from_header`: Content of the `From` header.
        *   `recipient_address`: Envelope recipient address (e.g., from Mailgun's `recipient` field, indexed).
        *   `to_header`: Content of the `To` header.
        *   `cc_header`: Content of the `Cc` header (nullable).
        *   `subject`: Email subject (nullable).
        *   `body_plain`, `body_html`, `stripped_text`, `stripped_html`: Various versions of the email body content (nullable).
        *   `received_at`: Timestamp when the webhook was received (indexed).
        *   `email_date`: Timestamp from the email's `Date` header (parsed, timezone-aware, nullable, indexed).
        *   `headers_json`: Raw headers stored as JSONB (nullable).
        *   `attachment_info`: JSONB array containing metadata about attachments (filename, content_type, size, storage_path where the attachment is persistently stored). (nullable).
*   **Other Implemented Tables (continued):**
    *   `api_tokens`: Stores API tokens for authentication.
        *   `id`: Integer, primary key, auto-incrementing.
        *   `user_identifier`: String, non-nullable, indexed. Identifies the user (e.g., email or an ID from an auth system).
        *   `name`: String, non-nullable. User-friendly name for the token.
        *   `hashed_token`: String, non-nullable, unique, indexed. The securely hashed token.
        *   `prefix`: String(8), non-nullable, unique, indexed. A short, unique prefix of the token for quick lookups.
        *   `created_at`: DateTime(timezone=True), non-nullable. Timestamp of token creation.
        *   `expires_at`: DateTime(timezone=True), nullable. Timestamp of token expiration.
        *   `last_used_at`: DateTime(timezone=True), nullable. Timestamp of last token usage.
        *   `is_revoked`: Boolean, non-nullable, default False. Indicates if the token has been revoked.
*   **(Future) Potential Tables:**
    *   `events`: Could potentially cache calendar items or store locally managed events/deadlines.
    *   `users`: Family member details, preferences.
    *   `tasks`: Status of scheduled/background tasks. (Note: `tasks` table is already implemented for the task queue).
*   Entries store metadata like timestamps and roles (`message_history`). Source information is implicitly Telegram or Web UI for notes currently.

## 7. Data Sources & Actions

*   **CalDAV:** Used for reading events from configured calendars (e.g., iCloud, Nextcloud) via direct calendar URLs.
*   **iCalendar URLs:** Used for reading events from public or private `.ics` URLs.
*   **(Future) Calendar APIs:** Google Calendar, Microsoft Outlook Calendar via Graph API.
*   **Weather APIs:** e.g., OpenWeatherMap, NWS API (possibly accessed via MCP).
*   **(Future) Email Server:** IMAP for fetching, SMTP for sending, or using a service like Mailgun/SendGrid.
*   **Package Tracking APIs:** e.g., EasyPost, Shippo (possibly via MCP).
*   **MCP Servers:** Standardized interfaces to external tools and data sources (e.g., Home Assistant, Git repositories, custom databases, web search).
*   **Direct user input** via supported interfaces.

## 8. Technology Considerations (High-Level)

*   **LLM:** Configurable model via command-line argument, accessed through **LiteLLM** and **OpenRouter**. Supports tool use (function calling).
*   **Backend:** **Python** using `python-telegram-bot` for Telegram interaction, `FastAPI` and `uvicorn` for the web server.
*   **Database & ORM:** **SQLite** (default) or **PostgreSQL** (supported via connection string), accessed via **SQLAlchemy** (async).
*   **Configuration:** Environment variables (`.env`), YAML (`prompts.yaml`), JSON (`mcp_config.json`).
*   **Timezone:** Configurable via `TIMEZONE` environment variable, uses **pytz**.
*   **MCP:** Uses the `mcp` Python SDK to connect to and interact with MCP servers defined in `mcp_config.json`.
*   **Containerization:** **Docker** with `uv` for Python package management and `npm` for Node.js-based MCP tools.
*   **Calendar Libraries:** `caldav` for CalDAV interaction, `vobject` for parsing VCALENDAR data (used by both CalDAV and iCal), `httpx` for fetching iCal URLs.
*   **Formatting:** Uses `telegramify-markdown` for converting LLM output to Telegram MarkdownV2.
*   **Task Scheduling (Future):** `APScheduler` is included in requirements but not yet actively used.
*   **Utilities:** `uuid` for generating unique IDs (e.g., task IDs).

## 9. Current Implementation Status (as of 2025-05-21)

The following features from the specification are currently implemented:

*   **Telegram Interface:** Primary interaction point using `python-telegram-bot`.
*   **Processing Layer:**
    *   LLM interaction via **LiteLLM** and **OpenRouter**.
    *   LLM model configurable via command-line argument.
    *   Handles LLM tool calls (function calling).
*   **Configuration:**
    *   API keys, chat IDs, DB URL via environment variables (`.env`).
    *   Prompts via `prompts.yaml`.
    *   MCP server definitions via `mcp_config.json`.
    *   Timezone via `TIMEZONE` environment variable.
*   **Access Control:** Based on `ALLOWED_CHAT_IDS`.
*   **Error Handling:** Logging and optional notification to `DEVELOPER_CHAT_ID`.
*   **Lifecycle Management:** Graceful shutdown (`SIGINT`/`SIGTERM`), placeholder config reload (`SIGHUP`).
*   **Data Storage (SQLAlchemy with SQLite/PostgreSQL):** Database operations include retry logic.
    *   `notes` table for storing notes (id, title, content, timestamps).
    *   `message_history` table for storing conversation history (chat\_id, message\_id, timestamp, role, content, tool\_calls\_info JSON).
    *   `tasks` table for the background task queue (see Section 10), supporting `log_message`, `llm_callback`, `index_email`, and `embed_and_store_batch` task types.
    *   `received_emails` table for storing incoming email details. Emails received via webhook are parsed, attachments stored, and an `index_email` task is enqueued.
    *   `api_tokens` table for storing hashed API tokens, enabling API-based authentication.
*   **LLM Context:**
    *   System prompt includes:
        *   Current time (timezone-aware via `TIMEZONE` env var).
        *   Upcoming calendar events fetched from configured CalDAV and iCal sources (today, tomorrow, next 14 days).
        *   Context from the `notes` table.
    *   Recent message history (from `message_history`, including basic tool call info if available) is included.
    *   Replied-to messages (fetched from `message_history`) are included if the current message is a reply.
*   **Web UI:** Basic interface using **FastAPI** and **Jinja2** for viewing, adding, editing, and deleting notes.
    *   An interface to view message history grouped by conversation (`/history`).
    *   An interface to view recent tasks from the database task queue (`/tasks`), including a button to manually retry failed tasks.
    *   A vector search interface (`/vector-search`) that groups results by document and links to a document detail view (`/vector-search/document/{document_id}`).
    *   An interface for API Token Management (`/settings/tokens`) allowing users to create, view, and revoke API tokens.
    *   A document upload UI (`/documents/upload`) for ingesting files.
*   **Tools:**
    *   Local Tools:
        *   `add_or_update_note`: Saves/updates notes in the database. Accepts `title` and `content`.
        *   `schedule_future_callback`: Allows the LLM to schedule a task (`llm_callback`) to re-engage itself in the current chat at a future time with provided context. Accepts `callback_time` (ISO 8601 with timezone) and `context` (string). The `chat_id` (or equivalent interface/conversation ID) is automatically inferred from the conversation context. Task is created in the `tasks` table.
        *   `ingest_document_from_url`: Submits a URL for ingestion and indexing. Accepts `url`, an optional `title` (if not provided, title will be extracted automatically), `source_type`, `source_id`, and optional `metadata`.
    *   **MCP Integration:**
        *   Loads server configurations from `mcp_config.json` (resolves environment variables like `$API_KEY`).
        *   Connects to defined MCP servers (e.g., Time, Browser, Fetch, Brave Search) using the `mcp` library (connections established in parallel on startup).
        *   Discovers tools provided by connected MCP servers.
        *   Makes both local and MCP tools available to the LLM.
        *   Executes MCP tool calls requested by the LLM.
*   **Image Handling:** Processes the first photo attached to Telegram messages (in a batch) and sends it (base64 encoded) to the LLM along with the text.
*   **Markdown Formatting:** Uses `telegramify-markdown` to convert LLM responses to Telegram's MarkdownV2 format, with fallback to escaped text.
*   **Message Batching:** Buffers incoming messages received close together and processes them as a single batch to avoid overwhelming the LLM and ensure context.
*   **Containerization:** **Dockerfile** provided for building an image with all dependencies (Python via `uv`, Deno/npm for MCP tools, Playwright browser). Uses cache mounts for faster builds.
*   **Task Queue:** Implemented using the `tasks` database table, `asyncio.Event` for immediate notification, and a worker loop. Includes retry logic with exponential backoff and supports manual retry via the UI. (See Section 10).
*   **Email Ingestion & Processing:** Emails received via webhook are parsed, attachments stored, and an `index_email` task is enqueued. The indexing pipeline for emails includes an `LLMPrimaryLinkExtractorProcessor` to identify and extract primary URLs from email content for further processing (e.g., web fetching).
*   **Document Indexing & Vector Search:** Supports document uploads via API and UI. The indexing pipeline processes content (including PDF text extraction and URL fetching with automatic title extraction), chunks it, generates embeddings, and stores them. The vector search UI allows querying and displays results grouped by document, with links to a detailed document view.
*   **API Token Management:** Users can create, view, and revoke API tokens via the Web UI. The system supports API authentication using these tokens.

**Features Not Yet Implemented:**

*   Calendar Integration (writing events via CalDAV): Requires implementing write tools and enhanced configuration to specify target calendars.
*   Reminders (setting/notifying): Dependent on calendar write access and configuration for a dedicated reminders calendar.
*   Scheduled Tasks / Cron Jobs (e.g., daily brief, reminder checks): `APScheduler` is present but not integrated into the main loop for these types of user-facing scheduled tasks (recurring tasks for system operations like re-indexing are supported by the current task queue).
*   Advanced Web UI features (dashboard, chat).
*   User profiles/preferences table.

## 10. Database Task Queue

To handle background processing, asynchronous operations, and scheduled tasks without introducing an external message broker dependency, a simple task queue is implemented directly within the application's primary database (SQLite or PostgreSQL).

### 10.1 Design Goals
*   **Persistence:** Tasks survive application restarts.
*   **Atomicity:** Dequeuing a task should be an atomic operation to prevent multiple workers from processing the same task.
*   **Scheduled Delivery:** Support for tasks that should only be processed after a specific time.
*   **Typed Tasks:** Allow different types of tasks to be routed to specific handler logic.
*   **Simplicity:** Leverage existing database infrastructure.

### 10.2 Database Schema (`tasks` table)
The queue is managed via the `tasks` table with the following columns:

*   `id`: Integer, primary key, auto-incrementing internal ID.
*   `task_id`: String, **caller-provided unique ID** for the task. Ensures idempotency if a task is accidentally enqueued multiple times. Unique constraint enforced.
*   `task_type`: String, indicates the kind of task (e.g., `send_notification`, `process_email_ingestion`, `llm_callback`). Used to route the task to the correct handler function. Indexed.
*   `payload`: JSON (or Text), stores arbitrary data needed by the task handler. For `llm_callback`, this includes `chat_id` and `callback_context`.
*   `scheduled_at`: DateTime (timezone-aware), optional timestamp indicating the earliest time the task should be processed. If NULL, the task can be processed immediately. Indexed.
*   `created_at`: DateTime (timezone-aware), timestamp when the task was enqueued.
*   `status`: String, current state of the task (e.g., `pending`, `processing`, `done`, `failed`). Indexed. Default is `pending`.
*   `locked_by`: String, identifier of the worker currently processing the task. NULL if not being processed.
*   `locked_at`: DateTime (timezone-aware), timestamp when the worker acquired the lock. NULL if not locked.
*   `error`: Text, stores error details if the task status becomes `failed` or during retries.
*   `retry_count`: Integer, number of times this task has been attempted. Default is 0.
*   `max_retries`: Integer, maximum number of retries allowed for this task. Default is 3.

### 10.3 Operations
Core operations are provided in `storage.py`:

*   `enqueue_task(task_id, task_type, payload=None, scheduled_at=None, max_retries_override=None, recurrence_rule=None, original_task_id=None, notify_event=None)`: Adds a new task with status `pending`. Requires a unique `task_id`. Can optionally override `max_retries`, set a `recurrence_rule` (RRULE string), link to an `original_task_id` for recurring tasks, and trigger an `asyncio.Event` for immediate tasks. Includes retry logic for the database operation itself.

*   `dequeue_task(worker_id, task_types)`: Attempts to atomically retrieve and lock the oldest, ready (`status='pending'`, `scheduled_at` is past or NULL, `retry_count <= max_retries`) task matching one of the provided `task_types`, prioritizing tasks with fewer retries.
    *   It uses `SELECT ... FOR UPDATE SKIP LOCKED` logic (via SQLAlchemy's `with_for_update(skip_locked=True)`). This provides good concurrency on **PostgreSQL**.
    *   **Note:** On **SQLite**, `SKIP LOCKED` is not natively supported. SQLAlchemy's implementation might result in table-level locking during the transaction, potentially limiting concurrency if multiple workers access the same SQLite database file (which is generally discouraged).
    *   If successful, it updates the task's status to `processing`, sets `locked_by` and `locked_at`, and returns the task details. Returns `None` if no suitable task is available or lock acquisition fails. Includes retry logic for the database operation itself.
*   `update_task_status(task_id, status, error=None)`: Updates the status of a task (typically to `done` or `failed`), clears the lock information, and includes retry logic for the database operation.
*   `reschedule_task_for_retry(task_id, next_scheduled_at, new_retry_count, error)`: Updates a task for a retry attempt: increments retry count, sets new schedule time, updates last error, resets status to `pending`, and clears lock info. Includes retry logic for the database operation.

### 10.5 Recurring Tasks
The task system supports recurring tasks using RRULE strings and a duplication approach.

*   **Schema Extension:** The `tasks` table includes:
    *   `recurrence_rule`: (String, nullable) Stores the RRULE string (e.g., `FREQ=DAILY;INTERVAL=1;BYHOUR=8`).
    *   `original_task_id`: (String, nullable, indexed) Links subsequent recurring instances back to the ID of the *first* instance that defined the recurrence. For the first instance, this field is populated with its own `task_id`.
*   **Scheduling:**
    *   A new recurring task is initiated by calling `storage.enqueue_task` with the initial unique `task_id`, `task_type`, `payload`, `initial_schedule_time`, and the `recurrence_rule`.
*   **Processing Logic (Duplication):**
    *   When the `TaskWorker` successfully processes a task (marks it `done`):
        *   It checks if the task has a non-null `recurrence_rule`.
        *   If a rule exists, it uses `dateutil.rrule` to calculate the next occurrence time based on the rule and the *scheduled_at* time of the task that just completed.
        *   It generates a *new unique task_id* for the next instance (e.g., `{original_task_id}_recur_{next_iso_timestamp}`).
        *   It calls `storage.enqueue_task` to create this *next* task instance, copying the `task_type`, `payload`, `recurrence_rule`, `max_retries`, and `original_task_id` from the completed task, and setting the new `task_id` and `scheduled_at`.
        *   The *current* task remains marked as `done`.
*   **Considerations:**
    *   **Task ID:** Each instance of a recurring task has its own unique `task_id`. The `original_task_id` links them logically.
    *   **History:** Each instance is treated as a separate task run with its own potential retries and status history.
    *   **Missed Runs / Self-Healing:** This duplication approach *does not* automatically handle runs missed while the application was down. If the worker is offline when an instance was due, that instance will simply not be created. A separate mechanism (e.g., a periodic check scanning for original recurring tasks whose next expected run is in the past) would be needed for robust self-healing, but is not currently implemented.
    *   **Modification/Deletion:** Stopping a recurring task requires deleting or marking future *pending* instances and potentially preventing the creation of new ones (e.g., by nullifying the `recurrence_rule` on the *last completed* or *currently pending* instance linked to the `original_task_id`). This requires specific logic not yet implemented. Modifying the schedule requires similar careful handling.

### 10.6 Processing Model
*   **Polling & Notification:** The primary mechanism is polling (`task_worker_loop` in `main.py`). An `asyncio.Event` (`new_task_event`) allows `task_storage.enqueue_task` to wake the worker immediately for non-scheduled tasks, reducing latency.
*   **Worker Loop:** A background task (`asyncio` task) periodically calls `dequeue_task` or wakes up via the event.
*   **Handler Registry:** A dictionary (`TASK_HANDLERS` in `main.py`) maps `task_type` strings to corresponding asynchronous handler functions.
*   **Execution:** When a task is dequeued, the worker looks up the handler based on `task_type` and executes it with the task's `payload`.
*   **Implemented Handlers:**
    *   `handle_log_message`: Logs the task payload (example).
    *   `handle_llm_callback`: Extracts `interface_type`, `conversation_id`, and `callback_context` from payload, constructs a trigger message ("System Callback Trigger:..."), sends it to the LLM via `generate_llm_response_for_chat`, sends the LLM's response back to the specified chat *as the bot*, and stores both the trigger and response in message history.
    *   `handle_index_email`: Processes a stored email for indexing using the `IndexingPipeline`.
    *   `handle_embed_and_store_batch`: Generates and stores embeddings for a batch of text content.
*   **Completion/Failure/Retry:** Based on the handler's outcome:
    *   Success: Worker calls `update_task_status` to mark task as `done`. If the task has a `recurrence_rule`, a new task instance is enqueued for the next occurrence.
    *   Failure: Worker checks `retry_count` against `max_retries`.
        *   If retries remain, worker calls `reschedule_task_for_retry` with exponential backoff and jitter.
        *   If max retries are reached, worker calls `update_task_status` to mark task as `failed`. Error details are captured.
*   **Lock Timeout (Implicit):** While not explicitly implemented, long-running tasks could potentially be retried by other workers if a worker crashes. The retry mechanism handles failures within the handler, but crashes might require manual intervention or a separate stale lock detection mechanism (not currently implemented). See also Section 10.5 regarding self-healing for recurring tasks.

## 11. Security Considerations

### 11.1 Prompt Injection Mitigation
When the LLM's context includes information derived from external, potentially untrusted sources (e.g., ingested emails, results from web searches via MCP tools), there's a risk of prompt injection. Malicious content within these sources could trick the LLM into performing unintended actions, especially if the LLM has access to powerful tools.

Strategies to mitigate this include:

1.  **Tool Sensitivity Classification:** Categorize tools available to the LLM based on their potential impact:
    *   **Read-only:** Tools that only retrieve information (e.g., `get_note`, `get_time`, read-only web searches).
    *   **Mutating (Internal):** Tools that modify the application's internal state (e.g., `add_or_update_note`, `schedule_future_callback`).
    *   **Mutating (External):** Tools that affect external systems or have real-world consequences (e.g., future tools for adding calendar events, controlling smart home devices via MCP, sending emails/messages). These are considered the most sensitive.

2.  **Context Tainting:** Track whether the current processing context for an LLM request includes data derived from potentially untrusted external sources.
    *   A direct user message via Telegram is generally considered trusted.
    *   Data fetched via MCP tools (like web search results) or ingested from emails should mark the context as "tainted".

3.  **Conditional Tool Access / Confirmation:** When the LLM requests to use a tool, especially a *Mutating (Internal)* or *Mutating (External)* one:
    *   **Check Taint Status:** If the context is tainted:
        *   **Option A (Strict):** Disallow the use of all mutating tools. The LLM can report it cannot perform the action due to the context.
        *   **Option B (Confirmation):** For sensitive tools (especially *Mutating (External)*), do not execute the tool call immediately. Instead:
            *   The application's code (outside the LLM generation loop) sends a confirmation message to the user via a secure channel (e.g., Telegram message with an Inline Keyboard: "Allow [Tool Name] with params [Params]? [Yes] [No]").
            *   The tool call is only executed if the user explicitly confirms via the interface element (e.g., button press). The LLM is *not* involved in generating or processing this confirmation request/response, preventing it from bypassing the check.
    *   **If the context is *not* tainted** (e.g., a direct user request without external data lookups involved in the *same* turn), sensitive tool calls might be allowed directly, depending on the tool's nature and configured policy.

This approach describes *potential strategies* to balance functionality with security. Currently, **these mitigation strategies (context tainting, conditional tool access based on taint, user confirmation flows) are NOT implemented.** The system currently allows the LLM to call any available tool (local or MCP) based on its interpretation of the prompt and context, without explicit checks for context origin or user confirmation steps beyond the initial prompt.

## 12. Testing Strategy

The project employs a multi-layered testing strategy focusing on realistic functional and integration tests over exhaustive, heavily mocked unit tests. The primary goal is to ensure components work together correctly and the application behaves as expected from an end-user perspective.

### 12.1 Layers
*   **Integration Tests:** Verify interactions between components (e.g., Processing Layer <> Data Store, Task Worker <> Data Store).
*   **Functional / End-to-End (E2E) Tests:** Simulate user flows (e.g., sending a message, checking DB state and response; using Web UI endpoints).
*   **Unit Tests:** Used for specific, isolated, complex logic (e.g., HashingWordEmbeddingGenerator, text chunking).

### 12.2 Tools
*   **Runner:** `pytest` with `pytest-asyncio`.
*   **Database:** `testcontainers-python` manages a real PostgreSQL instance in Docker for high-fidelity testing. SQLite is used for faster tests where PostgreSQL-specific features are not required.
*   **LLM:** Uses `RuleBasedMockLLMClient` for deterministic testing of LLM interactions, allowing tests to define specific input-output rules.
*   **Telegram:** Core logic is refactored for direct testing. `python-telegram-bot` handlers are thin wrappers.
*   **Web Server:** `httpx` is used to test FastAPI endpoints.
*   **MCP:** Interactions are tested by mocking the MCP session state within the application logic. Direct testing of MCP server processes is deferred.
*   **Mocking:** Standard `unittest.mock` for targeted mocking.

### 12.3 Refactoring for Testability
A key prerequisite for testing is refactoring the codebase to use **Dependency Injection**. This involves:
*   Passing dependencies like database engines/sessions, configuration objects, LLM clients, and MCP state explicitly as arguments to functions and classes.
*   Eliminating reliance on global variables and direct imports of dependencies within core logic modules.
*   Decoupling core application logic (e.g., message processing) from interface-specific code (e.g., Telegram handlers).

### 12.4 Structure
Tests reside in a top-level `tests/` directory, organized into `integration/`, `functional/`, and potentially `unit/` subdirectories. Fixtures are managed in `tests/conftest.py`.
