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
    *   A UI to view, add, edit, and delete notes stored in the database.
    *   (Future) A dashboard view of upcoming events, reminders, etc.
    *   (Future) An alternative way to interact with the assistant (chat interface).
    *   (Future) Configuration options.
*   **Email (Future):**
    *   Users can forward emails (e.g., confirmations, invites) to a dedicated address for ingestion.
    *   The assistant might send certain notifications or summaries via email.

## 3. Architecture Overview

The system will consist of the following core components:

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
    3.  If parsing fails or data is unstructured (e.g., plain email body), the **LLM** is used to extract key information (dates, times, event names, confirmation numbers, etc.).
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
*   **(Future) Calendar Integration (Write):**
    *   Add/update events on the main family calendar via CalDAV.
*   **(Future) Reminders:**
    *   Set reminders via natural language (stored on a dedicated calendar).
    *   Receive notifications for due reminders.
*   **(Future) Email Ingestion:** Process information from forwarded emails.
*   **(Future) External Data Integration:** Fetch data like weather forecasts directly or via MCP.

## 6. Data Store Design Considerations

*   A structured relational database (e.g., SQLite, PostgreSQL) is recommended for easier querying and management.
*   Database interactions will be managed using **SQLAlchemy** as the ORM.
*   Implemented Tables:
    *   `notes`: Stores user-created notes (id, title, content, created\_at, updated\_at). Title is unique.
    *   `message_history`: Logs user and assistant messages per chat (chat\_id, message\_id, timestamp, role, content).
*   External Data:
    *   Calendar events are fetched live via CalDAV and are *not* stored in the local database.
*   (Future) Potential Tables:
    *   `events`: Could potentially cache calendar items or store locally managed events/deadlines.
    *   `users`: Family member details, preferences.
    *   `tasks`: Status of scheduled/background tasks.
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
*   **MCP:** Uses the `mcp` Python SDK to connect to and interact with MCP servers defined in `mcp_config.json`.
*   **Containerization:** **Docker** with `uv` for Python package management and `npm` for Node.js-based MCP tools.
*   **Calendar Libraries:** `caldav` for CalDAV interaction, `vobject` for parsing VCALENDAR data (used by both CalDAV and iCal), `httpx` for fetching iCal URLs.
*   **Task Scheduling (Future):** `APScheduler` is included in requirements but not yet actively used.

## 9. Current Implementation Status (as of 2025-04-19)

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
*   **Access Control:** Based on `ALLOWED_CHAT_IDS`.
*   **Error Handling:** Logging and optional notification to `DEVELOPER_CHAT_ID`.
*   **Lifecycle Management:** Graceful shutdown (`SIGINT`/`SIGTERM`), placeholder config reload (`SIGHUP`).
*   **Data Storage (SQLAlchemy with SQLite/PostgreSQL):**
    *   `notes` table for storing notes (id, title, content, timestamps).
    *   `message_history` table for storing conversation history (chat\_id, message\_id, timestamp, role, content).
*   **LLM Context:**
    *   System prompt includes:
        *   Current time.
        *   Upcoming calendar events fetched from configured CalDAV and iCal sources (today, tomorrow, next 14 days).
        *   Context from the `notes` table.
    *   Recent message history (from `message_history`) is included.
    *   Replied-to messages (fetched from `message_history`) are included.
*   **Web UI:** Basic interface using **FastAPI** and **Jinja2** for viewing, adding, editing, and deleting notes.
*   **Tools:**
    *   Local tool `add_or_update_note` available to the LLM for saving notes to the database.
    *   **MCP Integration:**
        *   Loads server configurations from `mcp_config.json`.
        *   Connects to defined MCP servers (e.g., Time, Browser, Fetch, Brave Search) using the `mcp` library.
        *   Discovers tools provided by connected MCP servers.
        *   Makes both local and MCP tools available to the LLM.
        *   Executes MCP tool calls requested by the LLM.
*   **Image Handling:** Processes photos attached to Telegram messages and sends them to the LLM.
*   **Containerization:** **Dockerfile** provided for building an image with all dependencies (Python via `uv`, Node.js via `npm`, Playwright browser).

**Features Not Yet Implemented:**

*   Calendar Integration (writing events via CalDAV).
*   Reminders (setting/notifying - likely requires a dedicated reminder calendar and write access).
*   Email Ingestion.
*   Scheduled Tasks / Cron Jobs (e.g., daily brief, reminder checks).
*   Advanced Web UI features (dashboard, chat).
*   User profiles/preferences table.

