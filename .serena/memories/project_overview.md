# Family Assistant Project Overview

## Purpose

Family Assistant is an LLM-powered application designed to centralize family information management
and automate tasks. It serves as a comprehensive personal assistant with multiple interfaces and
capabilities.

## Tech Stack

- **Language**: Python 3.10+
- **Framework**: FastAPI for web server, python-telegram-bot for Telegram interface
- **Database**: SQLAlchemy with support for both SQLite (development) and PostgreSQL (production)
- **LLM Integration**: litellm for multi-provider LLM support
- **Task Queue**: Custom database-backed async task queue
- **Deployment**: Kubernetes (namespace: ml-bot)
- **Package Manager**: uv (modern Python package manager)

## Key Features

- Multiple interfaces: Telegram bot, Web UI, Email webhooks
- Document indexing with vector search (pgvector)
- Calendar integration (CalDAV)
- Home Assistant integration
- Event-driven architecture
- Background task processing
- Multi-profile support with different LLM models and tools
- MCP (Model Context Protocol) integration for external tools

## Project Structure

- `src/family_assistant/`: Main application code
  - `tools/`: LLM-accessible tools
  - `storage/`: Database repositories
  - `web/`: FastAPI web interface
  - `events/`: Event system
  - `indexing/`: Document processing pipeline
- `tests/`: Comprehensive test suite
- `scripts/`: Development scripts
- `alembic/`: Database migrations
- `deploy/`: Kubernetes deployment files
