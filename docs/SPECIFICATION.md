# Family Assistant LLM Application - Specification

## 1. Introduction

### 1.1 Purpose
This document outlines the specification for an LLM-based family assistant application. The assistant aims to centralize family-related information, provide proactive updates, handle specific requests from family members, and automate certain information management tasks.

### 1.2 Inspiration
The design is inspired by Geoffrey Litt's ["Stevens" assistant](https://www.geoffreylitt.com/2025/04/12/how-i-made-a-useful-ai-assistant-with-one-sqlite-table-and-a-handful-of-cron-jobs), particularly its use of a central log/memory and cron-based interactions, but adapted for potentially more structured data storage and multi-interface access.

### 1.3 Scope
The system will provide a conversational interface primarily through Telegram, with additional access via Email and a Web interface. It will manage information like calendar events, reminders, and user-provided facts, and perform tasks based on direct requests, ingested information, or scheduled triggers.

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
    *   Includes specific logic for parsing structured data where possible (e.g., calendar invites, specific email formats) to complement LLM extraction.
*   **Data Store:** A central repository (likely a structured database, e.g., PostgreSQL or SQLite) storing:
    *   Events (calendar items, deadlines)
    *   Reminders
    *   Facts/Memories (user-provided info, ingested details)
    *   User profiles and preferences
    *   System configuration
    *   Logs of interactions and ingested data (including source, timestamps).
*   **Task Scheduler:** Manages cron jobs or scheduled tasks for:
    *   Periodic data ingestion (e.g., checking calendars, weather APIs).
    *   Proactive updates (e.g., generating and sending the daily brief).
    *   Maintenance tasks (e.g., data cleanup).

### 3.1 Data Flow Example (Daily Brief)
1.  **Task Scheduler** triggers the "Daily Brief" task.
2.  **Processing Layer** queries the **Data Store** for relevant information for the day (e.g., calendar events, reminders, weather forecast data fetched earlier).
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
    3.  **LLM** interprets the request. May involve querying the **Data Store** (e.g., "What's on my schedule?") or performing an action (e.g., "Remind me...").
    4.  If action involves data modification, **Processing Layer** updates the **Data Store**.
    5.  **LLM** generates a response.
    6.  **Interaction Layer** delivers the response.
*   **Examples:**
    *   "What's happening tomorrow?"
    *   "Remind me to call the plumber at 5 PM today."
    *   "Add dentist appointment on June 5th at 10 AM."
    *   "What was the flight number for the trip we booked?" (requires prior ingestion)

### 4.2 Ingestion Interaction
*   **Trigger:** User forwards an email, shares a calendar event, uploads a file (via Web?), or sends specific formatted info via chat.
*   **Process:**
    1.  **Interaction Layer** (or dedicated ingestion service) receives the data.
    2.  **Processing Layer** attempts to parse structured data first (if applicable format is known).
    3.  If parsing fails or data is unstructured (e.g., plain email body), the **LLM** is used to extract key information (dates, times, event names, confirmation numbers, etc.).
    4.  Extracted/parsed information is structured and saved to the **Data Store**, linked to the source and timestamp.
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
    2.  Often involves querying the **Data Store** for relevant, time-sensitive information.
    3.  May involve querying external APIs (e.g., Weather API).
    4.  **LLM** may be used to synthesize information into a user-friendly format (e.g., the daily brief).
    5.  **Interaction Layer** delivers the output (if any) to users.
*   **Examples:**
    *   Send daily morning brief (calendar, weather, reminders).
    *   Send weekly summary of upcoming events.
    *   Check for expiring reminders.
    *   Fetch weather forecast data and store it.

## 5. Key Features

*   **Daily Brief:** Customizable morning update including calendar, weather, reminders, and potentially package/mail info.
*   **Calendar Integration:**
    *   Read events from specified shared family calendars (e.g., Google Calendar, CalDAV).
    *   Add/update events based on user requests or ingested data.
*   **Reminders:**
    *   Set time-based or date-based reminders via natural language.
    *   Receive notifications for due reminders.
*   **Information Storage & Retrieval:**
    *   Store key facts, dates (birthdays, anniversaries), preferences, and notes provided by users or ingested.
    *   Answer questions based on this stored information ("When is Grandma's birthday?", "What's the wifi password?").
*   **Email Ingestion:** Process information from emails forwarded to a specific address.
*   **External Data Integration:** Fetch and incorporate data like weather forecasts.

## 6. Data Store Design Considerations

*   While inspired by Stevens' single table, a more structured relational database (e.g., SQLite, PostgreSQL) is recommended for easier querying and management.
*   Potential Tables:
    *   `events`: Calendar items, deadlines (title, start\_time, end\_time, description, source, user\_id, created\_at, updated\_at).
    *   `reminders`: Reminder details (text, due\_time, user\_id, recurring\_rule, created\_at, completed\_at).
    *   `memories`: Facts, notes, ingested data snippets (content, source, type, relevance\_date, user\_id, created\_at).
    *   `users`: Family member details, preferences, notification settings.
    *   `tasks`: Status of scheduled/background tasks.
*   Entries should store metadata: source (email, telegram, calendar API, user input), timestamp of creation/update, relevant dates/times.

## 7. Potential Data Sources

*   Calendar APIs (Google Calendar, Microsoft Outlook Calendar via Graph API, CalDAV).
*   Weather APIs (e.g., OpenWeatherMap, NWS API).
*   Email Server (IMAP for fetching, SMTP for sending, or using a service like Mailgun/SendGrid).
*   Package Tracking APIs (e.g., EasyPost, Shippo, or direct carrier APIs if feasible).
*   Direct user input via supported interfaces.

## 8. Technology Considerations (High-Level)

*   **LLM:** Model choice (e.g., Claude series, GPT series) will impact cost, performance, and capabilities (context window size, function calling/tool use). Consider using a library like `litellm` for flexibility.
*   **Backend:** Python is a strong candidate due to libraries like `python-telegram-bot`, `SQLAlchemy`, `APScheduler`, and numerous API clients. Node.js is also viable.
*   **Database:** SQLite for simplicity if self-hosted/single-instance, PostgreSQL for more robust features and scalability.
*   **Hosting:** Options range from self-hosting, cloud VMs (AWS EC2, GCP Compute Engine), container platforms (Docker, Kubernetes), or serverless platforms (potentially challenging for stateful components like the bot listener/scheduler), or specialized platforms like Val.town.
*   **Task Scheduling:** System cron, `APScheduler` (Python), node-cron (Node.js), or platform-specific schedulers (AWS EventBridge, Google Cloud Scheduler).

