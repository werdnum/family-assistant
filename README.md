# Family Assistant

Family Assistant is an LLM-powered application designed for family information management and task
automation. It provides multiple interfaces (Telegram, Web UI, Email webhooks) and uses a modular
architecture built with Python, FastAPI, and SQLAlchemy. The goal of this project is to create a
centralized, intelligent assistant that can help with a variety of tasks, from scheduling and
reminders to answering questions based on your personal documents.

![Family Assistant Web Interface](screenshots/desktop/landing-page.png) *The Family Assistant web
interface provides easy access to chat, notes, documents, and more*

## Features

- **Multiple Interfaces**: Interact with the assistant via Telegram, a responsive web UI, or by
  forwarding emails.
- **LLM-Powered**: Leverages the power of Large Language Models for natural language understanding
  and generation. You can configure different models for different tasks.
- **Task Automation**: Automate repetitive tasks with a powerful scheduling and callback system.
- **Document Indexing**: Ingest and search through your documents, including PDFs, text files, and
  web pages. The assistant can use this information to answer questions.
- **Calendar Integration**: Connects with CalDAV and iCal calendars to manage your family's
  schedule.
- **Extensible**: Add new tools and capabilities to the assistant. The project is designed to be
  modular and easy to extend.
- **Configurable**: Fine-tune the assistant's behavior with a flexible configuration system using
  `config.yaml` and environment variables.

### Key Interface Screenshots

<table>
  <tr>
    <td width="50%">
      <img src="screenshots/desktop/chat-empty.png" alt="Chat Interface">
      <p align="center"><em>Chat with real-time streaming responses</em></p>
    </td>
    <td width="50%">
      <img src="screenshots/desktop/notes-list.png" alt="Notes Management">
      <p align="center"><em>Organize and manage your notes</em></p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <img src="screenshots/desktop/documents.png" alt="Document Management">
      <p align="center"><em>Search and manage indexed documents</em></p>
    </td>
    <td width="50%">
      <img src="screenshots/desktop/automations.png" alt="Automations">
      <p align="center"><em>Create event and schedule-based automations</em></p>
    </td>
  </tr>
</table>

## Architecture

Family Assistant is built with a modern Python stack, featuring:

- **Backend**: FastAPI for the web server, SQLAlchemy for database interactions, and `uv` for
  dependency management.
- **Frontend**: A React application built with Vite, TypeScript, and Tailwind CSS.
- **Asynchronous**: Fully asynchronous architecture using `asyncio` for high performance.
- **Dependency Injection**: Promotes modular and testable code.
- **Repository Pattern**: Encapsulates data access logic for clean and maintainable code.
- **Event-Driven**: Loosely coupled components communicate via events.

For a detailed architecture overview, see
[docs/architecture-diagram.md](docs/architecture-diagram.md).

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js and npm
- Docker (recommended for the development environment)

### Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/your-username/family-assistant.git
   cd family-assistant
   ```

   git clone https://github.com/werdnum/family-assistant.git The easiest way to set up your
   development environment is to use the setup script:

   ```bash
   ./scripts/setup-workspace.sh
   ```

   This script will:

   - Create a virtual environment (`.venv`)
   - Install Python dependencies
   - Install frontend dependencies
   - Set up pre-commit hooks

2. **Activate the virtual environment:**

   ```bash
   source .venv/bin/activate
   ```

For manual installation instructions, see `AGENTS.md`.

## Configuration

The application is configured through `config.yaml` and environment variables.

- **`config.yaml`**: The main configuration file for the application. It defines service profiles,
  tool configurations, LLM parameters, and the document indexing pipeline.
- **`prompts.yaml`**: This file contains the prompts used by the LLM, allowing you to customize the
  assistant's personality and behavior.
- **`.env`**: Create a `.env` file in the root of the project to store your secrets, such as API
  keys and database URLs. You can use `.env.example` as a template.

### Environment Variables

The following environment variables are the most important for getting started:

- `TELEGRAM_BOT_TOKEN`: Your Telegram bot token.
- `OPENROUTER_API_KEY`: Your OpenRouter API key.
- `GEMINI_API_KEY`: Your Google Gemini API key.
- `DATABASE_URL`: The URL for your database (e.g., `sqlite+aiosqlite:///family_assistant.db` or
  `postgresql+asyncpg://user:password@host/dbname`).
- `ALLOWED_USER_IDS`: A comma-separated list of Telegram user IDs that are allowed to interact with
  the bot.

For a full list of environment variables, see the
[Configuration Reference](docs/operations/CONFIGURATION_REFERENCE.md).

## Usage

### Running the Application

To run the application in development mode with hot-reloading:

```bash
poe dev
```

This will start both the backend and frontend servers. You can access the web UI at
`http://localhost:5173`.

![Chat Interface in Action](screenshots/desktop/chat-with-messages.png) *Interactive chat with the
assistant showing tool usage and streaming responses*

### Development Commands

The project uses `poethepoet` as a task runner. Here are some of the most common commands:

- **`poe lint`**: Run the full linting suite.
- **`poe test`**: Run the test suite.
- **`poe format`**: Format the code.
- **`poe serve`**: Run the backend server only.
- **`poe serve-reload`**: Run the backend server with auto-reloading.
- **`poe ui-dev`**: Run the frontend development server.
- **`poe ui-build`**: Build the production frontend assets.

For a full list of commands, see the `[tool.poe.tasks]` section in `pyproject.toml`.

### Testing

To run the full test suite:

```bash
poe test
```

You can also run specific tests:

```bash
pytest tests/functional/test_specific.py
```

The test suite includes unit tests, functional tests, and integration tests. It uses `pytest` for
the backend and `vitest` for the frontend.

## Frontend Development

The frontend is a React application located in the `frontend/` directory. It uses Vite for the build
tooling, TypeScript for type safety, and Tailwind CSS for styling.

### Frontend Scripts

- **`npm run dev`**: Start the Vite development server.
- **`npm run build`**: Build the production assets.
- **`npm run lint`**: Lint the frontend code.
- **`npm run test`**: Run the frontend tests with `vitest`.
- **`npm run format`**: Format the frontend code with `biome`.

## Database Migrations

The project uses Alembic for database migrations.

- **Create a new migration:**

  When you make changes to the SQLAlchemy models, you'll need to generate a new migration script:

  ```bash
  alembic revision --autogenerate -m "Your migration message"
  ```

- **Apply migrations:**

  To apply the latest migrations to your database:

  ```bash
  alembic upgrade head
  ```

## Documentation

### For Users

- **[Quick Start Guide](docs/user/QUICK_START.md)** - Get started in 5 minutes
- **[Features Overview](docs/user/FEATURES.md)** - Complete feature catalog
- **[User Guide](docs/user/USER_GUIDE.md)** - Comprehensive usage documentation
- **[FAQ](docs/user/FAQ.md)** - Frequently asked questions
- **[Scheduling](docs/user/scheduling.md)** - Reminders and scheduled tasks
- **[Scripting](docs/user/scripting.md)** - Automation with scripts
- **[Camera Integration](docs/user/camera_integration.md)** - Security camera features
- **[Browser Automation](docs/user/browser_automation.md)** - Web browsing and automation
- **[Image Tools](docs/user/image_tools.md)** - Image generation and editing

### For Developers

- **[AGENTS.md](AGENTS.md)** - Development setup and guidelines
- **[Contributing](CONTRIBUTING.md)** - How to contribute
- **[Architecture](docs/architecture-diagram.md)** - System architecture
- **[Style Guide](docs/STYLE_GUIDE.md)** - Code style and conventions
- **[API Documentation](docs/api/README.md)** - REST API reference

### For Operators

- **[Operator Getting Started](docs/operations/GETTING_STARTED.md)** - **Start here for deployment**
- **[Production Deployment](docs/deployment/PRODUCTION_DEPLOYMENT.md)** - Advanced Kubernetes
  deployment
- **[Configuration Reference](docs/operations/CONFIGURATION_REFERENCE.md)** - All config options
- **[Monitoring](docs/operations/MONITORING.md)** - Logging and observability
- **[Backup & Recovery](docs/operations/BACKUP_RECOVERY.md)** - Data protection
- **[Runbooks](docs/operations/RUNBOOKS.md)** - Operational procedures
