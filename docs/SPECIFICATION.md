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
*   **Web (Secondary):** A web interface could provide:
    *   A dashboard view of upcoming events, reminders, etc.
    *   An alternative way to interact with the assistant (chat interface).
    *   Configuration options (TBD).

## 3. Architecture Overview

The system will consist of the following core components:

*   **Interaction Layer:** Manages communication across Telegram, Email, and Web interfaces. It receives user input, forwards it for processing, and delivers responses/updates back to the user via the appropriate channel.
*   **Processing Layer:**
    *   Utilizes a Large Language Model (LLM) (e.g., Claude, GPT) to understand natural language requests, extract information from ingested data, generate summaries/briefs, and formulate responses.
    *   Leverages MCP tools provided by connected servers to perform actions or retrieve external context.
    *   Includes specific logic for parsing structured data where possible (e.g., calendar invites, specific email formats) to complement LLM extraction.
*   **Data Store:** A central repository (a structured database, e.g., PostgreSQL or SQLite, accessed via **SQLAlchemy**) storing:
    *   Events (calendar items, deadlines)
    *   Facts/Memories (user-provided info, ingested details)
    *   User profiles and preferences
    *   System configuration
    *   Logs of interactions and ingested data (including source, timestamps).
    *   *Note: Reminders are stored on a dedicated calendar, not in this database.*
*   **MCP Integration Layer:** Connects to various MCP servers (e.g., Home Assistant, Git, Databases) to provide extended context and actions (tools) to the LLM. This allows the assistant to interact with other systems in a standardized way.
*   **Task Scheduler:** Manages cron jobs or scheduled tasks for:
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

### 4.2 Ingestion Interaction
*   **Trigger:** User forwards an email, shares a calendar event, uploads a file (via Web?), or sends specific formatted info via chat.
*   **Process:**
    1.  **Interaction Layer** (or dedicated ingestion service) receives the data.
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
*   **Calendar Integration:**
    *   Read events from specified shared family calendars (e.g., Google Calendar, CalDAV).
    *   Add/update events on the main family calendar based on user requests or ingested data.
*   **Reminders:**
    *   Set time-based or date-based reminders via natural language, **which are stored on a dedicated family reminders calendar**.
    *   Receive notifications for due reminders (triggered by checking the calendar).
*   **Information Storage & Retrieval:**
    *   Store key facts, dates (birthdays, anniversaries), preferences, and notes provided by users or ingested into the `memories` table.
    *   Answer questions based on this stored information ("When is Grandma's birthday?", "What's the wifi password?").
*   **Email Ingestion:** Process information from emails forwarded to a specific address, storing relevant details in the `memories` table.
*   **External Data Integration:** Fetch and incorporate data like weather forecasts (potentially via MCP).
*   **MCP Integrations:** Leverage MCP servers to interact with other systems like Home Assistant (controlling lights, checking sensors), query specific databases, interact with version control, etc., based on user requests.

## 6. Data Store Design Considerations

*   A structured relational database (e.g., SQLite, PostgreSQL) is recommended for easier querying and management.
*   Database interactions will be managed using **SQLAlchemy** as the ORM.
*   Potential Tables:
    *   `events`: Calendar items, deadlines (title, start\_time, end\_time, description, source, user\_id, created\_at, updated\_at). *Note: This might primarily mirror an external calendar.*
    *   `memories`: Facts, notes, ingested data snippets (content, source, type, relevance\_date, user\_id, created\_at).
    *   `users`: Family member details, preferences, notification settings (e.g., Telegram chat ID).
    *   `tasks`: Status of scheduled/background tasks.
*   Entries should store metadata: source (email, telegram, calendar API, user input, MCP server), timestamp of creation/update, relevant dates/times.

## 7. Potential Data Sources & Actions

*   **Calendar APIs:** Google Calendar, Microsoft Outlook Calendar via Graph API, CalDAV (for both main events and the dedicated **Reminders Calendar**).
*   **Weather APIs:** e.g., OpenWeatherMap, NWS API (possibly accessed via MCP).
*   **Email Server:** IMAP for fetching, SMTP for sending, or using a service like Mailgun/SendGrid.
*   **Package Tracking APIs:** e.g., EasyPost, Shippo (possibly via MCP).
*   **MCP Servers:** Standardized interfaces to external tools and data sources (e.g., Home Assistant, Git repositories, custom databases, web search).
*   **Direct user input** via supported interfaces.

## 8. Technology Considerations (High-Level)

*   **LLM:** Model choice (e.g., Claude series, GPT series) will impact cost, performance, and capabilities (context window size, function calling/tool use). Consider using a library like `litellm` for flexibility.
*   **Backend:** Python is a strong candidate due to libraries like `python-telegram-bot`, `SQLAlchemy`, `APScheduler`, `mcp`, and numerous API clients. Node.js is also viable.
*   **Database & ORM:** SQLite or PostgreSQL, accessed via **SQLAlchemy**.
*   **Hosting:** Options range from self-hosting, cloud VMs (AWS EC2, GCP Compute Engine), container platforms (Docker, Kubernetes), or serverless platforms (potentially challenging for stateful components like the bot listener/scheduler), or specialized platforms like Val.town.
*   **Task Scheduling:** System cron, `APScheduler` (Python), node-cron (Node.js), or platform-specific schedulers (AWS EventBridge, Google Cloud Scheduler).
*   **MCP:** Utilize MCP SDKs (e.g., `mcp` for Python) to interact with MCP servers. Consider potentially exposing some of the assistant's own data (like memories) via a custom MCP server for other tools to use.

## 9. Initial Implementation (Phase 1)

*   Focus on the **Telegram Interface** as the primary interaction point.
*   Implement the **Processing Layer** with basic LLM forwarding using **LiteLLM** and **OpenRouter**.
*   Set up the core application structure using `python-telegram-bot`.
*   LLM model selection configurable via command-line arguments.
*   API keys and configuration (allowed chat IDs, developer chat ID) managed via environment variables (`.env` file).
*   Basic access control based on `ALLOWED_CHAT_IDS`.
*   Error handling with logging and optional notification to `DEVELOPER_CHAT_ID`.
*   Graceful shutdown on `SIGINT`/`SIGTERM`.
*   Placeholder for config reload on `SIGHUP`.
*   Basic key-value storage implemented using SQLAlchemy.
*   Key-value pairs are fetched and included in the context sent to the LLM.
*   Message history (user and assistant messages) is stored in the database using SQLAlchemy.
*   Recent message history is fetched from the database to provide context for LLM queries.
*   Replied-to messages are fetched from the database to provide specific context.
*   No calendar integration, reminders, or MCP features implemented initially.

