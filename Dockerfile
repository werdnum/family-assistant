# Use the official UV image as the base
FROM ghcr.io/astral-sh/uv:debian-slim AS base

# Install system dependencies: npm for Node.js MCP servers
# Using --mount for caching apt downloads
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends npm && \
    # Clean up apt cache to reduce image size
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# --- Install Python Dependencies ---
# Copy only the requirements file first to leverage Docker cache
COPY requirements.txt .
# Create a virtual environment
RUN uv venv /app/.venv
# Install Python dependencies into the virtual environment
# Note: uv pip install automatically detects and uses .venv in the current dir if it exists
# We don't need --system anymore.
# Using --mount for caching pip downloads/builds
RUN --mount=type=cache,target=/root/.cache/pip \
    uv pip install -r requirements.txt # Removed --no-cache

# --- Install MCP Tools ---
# Install Python MCP tools using uv tool install (these go into /uv/tools, separate from the venv)
# These will be available via `uvx` or directly if UV_TOOL_BIN_DIR is in PATH
RUN uv tool install mcp-server-time
RUN uv tool install mcp-server-fetch

# Install Node.js MCP tools globally using npm
RUN npm install -g @playwright/mcp@latest
RUN npm install -g @modelcontextprotocol/server-brave-search

# Install Playwright Chromium browser and its dependencies
# Using --with-deps is crucial for installing necessary OS libraries
# Running this after installing @playwright/mcp
RUN --mount=type=cache,target=/root/.cache/ms-playwright,sharing=locked \
    npx playwright install --with-deps chromium

# --- Configure Environment ---
# Set environment variables
# - PYTHONDONTWRITEBYTECODE: Prevents Python from writing .pyc files
# - PYTHONUNBUFFERED: Ensures Python output (like logs) is sent straight to terminal
# - UV_TOOL_BIN_DIR/UV_TOOL_DIR: Standard locations for uv tools
# - PATH: Ensure uv tool binaries and globally installed npm packages are findable
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_TOOL_BIN_DIR=/uv/bin \
    UV_TOOL_DIR=/uv/tools \
    # Add uv tool bin and default npm global bin to PATH
    PATH="${UV_TOOL_BIN_DIR}:/usr/local/bin:${PATH}"

# --- Copy Application Code ---
# Copy the rest of the application code and configuration
COPY main.py processing.py storage.py web_server.py ./
COPY prompts.yaml mcp_config.json .env ./
COPY templates/ ./templates/

# --- Runtime Configuration ---
# Expose the port the web server listens on
EXPOSE 8000

# Define the default command to run the application using the venv's python
CMD ["/app/.venv/bin/python", "main.py"]
