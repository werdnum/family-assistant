# Use the official UV image as the base
FROM ghcr.io/astral-sh/uv:debian-slim AS base

# Install system dependencies: npm for Node.js MCP servers and frontend build
# Using --mount for caching apt downloads
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    # Add CA certificates for HTTPS communication
    ca-certificates \
    curl \
    unzip \
    gnupg \
    && \
    # Add NodeSource repository for newer Node.js
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -     && \
    apt-get install -y --no-install-recommends nodejs && \
    update-ca-certificates && \
    # Clean up apt cache to reduce image size
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# --- Install Python Dependencies ---
# Define the cache directory for uv
ENV UV_CACHE_DIR=/uv-cache

# Create virtual environment
RUN uv venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# --- Install Deno ---
ARG DENO_VERSION=v2.2.11
ARG TARGETARCH=amd64
# Select the correct Deno binary based on architecture
RUN ARCHITECTURE="" && \
    if [ "$TARGETARCH" = "amd64" ]; then \
        ARCHITECTURE="x86_64-unknown-linux-gnu"; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        ARCHITECTURE="aarch64-unknown-linux-gnu"; \
    else \
        echo "Unsupported architecture: $TARGETARCH" && exit 1; \
    fi && \
    curl -fsSL https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-${ARCHITECTURE}.zip -o deno.zip && \
    unzip deno.zip && \
    mv deno /usr/local/bin/ && \
    chmod +x /usr/local/bin/deno && \
    rm deno.zip && \
    # Verify installation
    deno --version

# --- Install MCP Tools ---
# Install Python MCP tools using uv tool install (these go into /uv/tools, separate from the venv)
# These will be available via `uvx` or directly if UV_TOOL_BIN_DIR is in PATH
RUN uv tool install mcp-server-time
RUN uv tool install mcp-server-fetch

# Install Node.js MCP tools globally using Deno, providing explicit names
RUN deno install --global -A --name playwright-mcp npm:@playwright/mcp@latest && \
    deno install --global -A --name brave-search-mcp-server npm:@modelcontextprotocol/server-brave-search

# Install Playwright Chromium browser and its dependencies using Deno
# Using --with-deps is crucial for installing necessary OS libraries
# Running this after installing @playwright/mcp
RUN --mount=type=cache,target=/root/.cache/ms-playwright,sharing=locked \
    deno run -A npm:playwright install --with-deps chromium

# --- Configure Environment ---
# Set environment variables
# - PYTHONDONTWRITEBYTECODE: Prevents Python from writing .pyc files
# - PYTHONUNBUFFERED: Ensures Python output (like logs) is sent straight to terminal
# - UV_TOOL_BIN_DIR/UV_TOOL_DIR: Standard locations for uv tools
# - UV_CACHE_DIR: Explicit cache location for uv operations
# - PATH: Ensure uv tool binaries and Deno bin directory are findable
# - DOCS_USER_DIR: Path to user documentation directory
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_TOOL_BIN_DIR=/uv/bin \
    UV_TOOL_DIR=/uv/tools \
    UV_CACHE_DIR=/uv-cache \
    UV_HTTP_TIMEOUT=300 \
    ALEMBIC_CONFIG=/app/alembic.ini \
    DOCS_USER_DIR=/app/docs/user

# Update PATH separately
ENV PATH="${UV_TOOL_BIN_DIR}:/root/.deno/bin:/usr/local/bin:${PATH}"

# --- Install Python dependencies for contrib/scrape_mcp.py ---
RUN --mount=type=cache,target=${UV_CACHE_DIR} \
    uv pip install "playwright>=1.0" "markitdown[html]>=0.1.0" && \
    playwright install --with-deps chromium

# Copy only pyproject.toml first to leverage Docker layer caching for dependencies
COPY pyproject.toml ./

# --- Frontend Build Stage ---
# Copy frontend package files first for layer caching
COPY frontend/package*.json ./frontend/

# Install frontend dependencies
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    cd frontend && npm ci

# Copy frontend source files
COPY frontend/ ./frontend/

RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    cd frontend && npm run build

# --- Copy Application Code ---
# Copy the source code into the image
COPY src/ /app/src/
COPY docs/ /app/docs/

# Copy configuration files, templates, and static assets to the WORKDIR
# These need to be accessible relative to the WORKDIR at runtime when running the app
COPY logging.conf alembic.ini config.yaml prompts.yaml mcp_config.json ./
COPY alembic /app/alembic/
COPY contrib /app/contrib

# --- Install the Package ---
# Install the package using uv from pyproject.toml. This ensures that the package
# is installed with all its source code.
RUN --mount=type=cache,target=${UV_CACHE_DIR} \
    uv pip install .

# --- Runtime Configuration ---
# Expose the port the web server listens on
EXPOSE 8000

# Define the default command to run the application using the installed entry point
# This uses the [project.scripts] defined in pyproject.toml
CMD ["family-assistant"]

# Alternatively, run using python -m:
# CMD ["python", "-m", "family_assistant"]
