# Use the official UV image as the base
FROM ghcr.io/astral-sh/uv:debian-slim AS base

# Create non-root user and group early in the build with specific UID/GID
RUN groupadd -r -g 1001 appuser && useradd -r -u 1001 -g appuser -m -d /home/appuser -s /bin/bash appuser

# Install system dependencies: npm for Node.js MCP servers and frontend build
# Using --mount for caching apt downloads
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    # Add CA certificates for HTTPS communication
    ca-certificates \
    curl \
    # FFmpeg for camera frame extraction from Reolink VOD streams
    ffmpeg \
    unzip \
    gnupg \
    && \
    # Add NodeSource repository for newer Node.js
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -     && \
    apt-get install -y --no-install-recommends nodejs && \
    update-ca-certificates && \
    # Clean up apt cache to reduce image size
    rm -rf /var/lib/apt/lists/*

# Set working directory and prepare directories with proper permissions
WORKDIR /app

# Create necessary directories and set ownership
RUN mkdir -p /home/appuser/.cache/uv /uv/tools /uv/bin /opt/playwright-browsers && \
    chown -R appuser:appuser /app /home/appuser /uv /opt/playwright-browsers

# --- Install Python Dependencies ---
# Define the cache directory for uv - use user's home directory
ENV UV_CACHE_DIR=/home/appuser/.cache/uv

# Create virtual environment (still as root for system packages)
RUN uv venv /app/.venv && \
    chown -R appuser:appuser /app/.venv

# --- Install Deno ---
ARG DENO_VERSION=v2.2.11
ARG TARGETARCH
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
# Ensure cache directory is writable before switching to appuser
RUN rm -rf /home/appuser/.cache/uv && \
    mkdir -p /home/appuser/.cache/uv && \
    chown -R appuser:appuser /home/appuser/.cache

# Switch to appuser for UV tool installations
USER appuser
ENV HOME=/home/appuser
ENV UV_TOOL_BIN_DIR=/uv/bin
ENV UV_TOOL_DIR=/uv/tools
RUN uv tool install mcp-server-time
RUN uv tool install mcp-server-fetch

# Install Node.js MCP tools globally using Deno, providing explicit names
RUN deno install --global -A --name playwright-mcp npm:@playwright/mcp@latest && \
    deno install --global -A --name brave-search-mcp-server npm:@modelcontextprotocol/server-brave-search

# Switch back to root for remaining system-level operations
USER root

# --- Configure Environment ---
# Set environment variables
# - PYTHONDONTWRITEBYTECODE: Prevents Python from writing .pyc files
# - PYTHONUNBUFFERED: Ensures Python output (like logs) is sent straight to terminal
# - UV_TOOL_BIN_DIR/UV_TOOL_DIR: Standard locations for uv tools
# - UV_CACHE_DIR: Explicit cache location for uv operations
# - PATH: Ensure venv, uv tool binaries, and Deno bin directory are findable
# - DOCS_USER_DIR: Path to user documentation directory
# - PLAYWRIGHT_BROWSERS_PATH: System-wide location for Playwright browsers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_TOOL_BIN_DIR=/uv/bin \
    UV_TOOL_DIR=/uv/tools \
    UV_CACHE_DIR=/home/appuser/.cache/uv \
    UV_HTTP_TIMEOUT=300 \
    ALEMBIC_CONFIG=/app/alembic.ini \
    DOCS_USER_DIR=/app/docs/user \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    VIRTUAL_ENV=/app/.venv

# Update PATH (ensure venv and tools are at the front)
ENV PATH="/app/.venv/bin:${UV_TOOL_BIN_DIR}:/home/appuser/.deno/bin:/usr/local/bin:${PATH}"

# Copy dependency definition files
COPY --chown=appuser:appuser pyproject.toml uv.lock* ./

# Install Python dependencies (without installing the project itself yet)
# This includes rebrowser-playwright which provides the playwright module
USER appuser
RUN uv sync --no-install-project --extra local-embeddings

# --- Install Playwright browsers with system dependencies ---
# Install Playwright browsers with system dependencies
# Switch back to root for system dependencies installation
USER root
RUN uv run playwright install-deps chromium

# Switch to appuser for browser installation
USER appuser
RUN uv run playwright install chromium && \
    # Verify the browser was installed correctly - build should fail if this fails
    ls -la ${PLAYWRIGHT_BROWSERS_PATH}/

USER root

# --- Frontend Build Stage ---
# Copy frontend package files first for layer caching
COPY --chown=appuser:appuser frontend/package*.json ./frontend/

# Install frontend dependencies as appuser
RUN --mount=type=cache,target=/home/appuser/.npm,uid=1001,gid=1001,sharing=locked \
    cd frontend && npm ci

# Copy frontend source files
USER root
COPY --chown=appuser:appuser frontend/ ./frontend/

USER appuser
RUN --mount=type=cache,target=/home/appuser/.npm,uid=1001,gid=1001,sharing=locked \
    cd frontend && npm run build

USER root

# --- Copy Application Code ---
# Copy the source code into the image with proper ownership
COPY --chown=appuser:appuser . .

# --- Install the Package ---
# Install the package using uv sync to ensure uv.lock continues to apply.
# This completes the installation by adding the project itself.
USER appuser
RUN uv sync --extra local-embeddings

# --- Runtime Configuration ---
# Expose the port the web server listens on
EXPOSE 8000

# Run as non-root user for security
# The application will run with reduced privileges
USER appuser

# Define the default command to run the application using the installed entry point
# This uses the [project.scripts] defined in pyproject.toml
CMD ["family-assistant"]

# Alternatively, run using python -m:
# CMD ["python", "-m", "family_assistant"]
