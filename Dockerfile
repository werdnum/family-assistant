# Use the official UV image as the base
FROM ghcr.io/astral-sh/uv:0.13.6-debian-slim@sha256:caa9f629fcb575ba2ed6ee160ec51e82e8444f6bdc3fc05c855b2636a5511eb8 AS base

# Create non-root user and group early in the build with specific UID/GID
RUN groupadd -r -g 1001 appuser && useradd -r -u 1001 -g appuser -m -d /home/appuser -s /bin/bash appuser

# Install system dependencies: npm for Node.js MCP servers and frontend build
# Using --mount for caching apt downloads
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    # Add CA certificates for HTTPS communication
    ca-certificates \
    curl \
    # git for MCP servers installed from GitHub
    git \
    # ripgrep for engineer profile source code search
    ripgrep \
    # FFmpeg for camera frame extraction from Reolink VOD streams
    ffmpeg \
    unzip \
    gnupg \
    && \
    # Add NodeSource repository for newer Node.js
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash -     && \
    apt-get install -y --no-install-recommends nodejs && \
    sed -i \
        -e 's|^mozilla/emSign_ECC_Root_CA_-_G3.crt|!&|' \
        -e 's|^mozilla/Comodo_AAA_Services_root.crt|!&|' \
        /etc/ca-certificates.conf && \
    update-ca-certificates && \
    # Clean up apt cache to reduce image size
    rm -rf /var/lib/apt/lists/*

# Install the first-party Brave Search MCP server, pinned for a reproducible build. This
# replaces @modelcontextprotocol/server-brave-search, which came from the archived MCP
# reference-server collection and has a monthly rate-limit counter that never resets,
# permanently failing "Rate limit exceeded" after ~15,000 calls until the process restarts.
ARG BRAVE_SEARCH_MCP_VERSION=2.1.3
RUN npm install --global "@brave/brave-search-mcp-server@${BRAVE_SEARCH_MCP_VERSION}"

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
ENV PATH="/app/.venv/bin:$PATH"

# --- Install Deno ---
ARG DENO_VERSION=v2.9.8
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
# Install Python MCP tools using uv tool install (these go into /uv/tools, separate from the venv).
# UV_TOOL_BIN_DIR is on PATH, so the entry points are invoked by name (see mcp_config in
# defaults.yaml). They are not reachable through `uvx`: the MCP SDK spawns stdio servers with a
# whitelisted environment (HOME, LOGNAME, PATH, SHELL, TERM, USER), so UV_TOOL_DIR never reaches
# the child and `uvx` would re-resolve the package from PyPI on every startup instead.
# Ensure cache directory is writable before switching to appuser
RUN rm -rf /home/appuser/.cache/uv && \
    mkdir -p /home/appuser/.cache/uv && \
    chown -R appuser:appuser /home/appuser/.cache

# Switch to appuser for UV tool installations
USER appuser
ENV HOME=/home/appuser
ENV UV_TOOL_BIN_DIR=/uv/bin
ENV UV_TOOL_DIR=/uv/tools
# Both servers declare `mcp>=1.23.0` with no upper bound but import `McpError` from
# `mcp.shared.exceptions`, which mcp 2.x removed — an unconstrained install resolves mcp 2.x and the
# server dies with an ImportError before it can speak MCP. Pin the SDK to the 1.x line the
# application itself resolves. scripts/check_mcp_servers.py fails the container smoke test if this
# regresses.
RUN uv tool install mcp-server-time --with "mcp<2" --with "certifi>=2026.10.19"
RUN uv tool install mcp-server-fetch --with "mcp<2" --with "certifi>=2026.10.19"

# Switch back to root for remaining system-level operations
USER root

# --- Configure Environment ---
# Set environment variables
# - PYTHONDONTWRITEBYTECODE: Prevents Python from writing .pyc files
# - PYTHONUNBUFFERED: Ensures Python output (like logs) is sent straight to terminal
# - UV_TOOL_BIN_DIR/UV_TOOL_DIR: Standard locations for uv tools
# - UV_CACHE_DIR: Explicit cache location for uv operations
# - PATH: Ensure uv tool binaries and Deno bin directory are findable
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
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

# Update PATH separately (use home directory agnostic path)
ENV PATH="${UV_TOOL_BIN_DIR}:/home/appuser/.deno/bin:/usr/local/bin:${PATH}"

# Install browser tooling from the same lock used by the application, before
# downloading the matching browser binaries.
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
USER appuser
RUN uv sync --locked --no-install-project --extra local-embeddings

# Install Playwright system dependencies and browser
# Note: We install deps manually to handle renamed packages in newer Debian
# (ttf-ubuntu-font-family -> fonts-ubuntu, ttf-unifont -> fonts-unifont)
USER root
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

# Switch to appuser for browser installation
USER appuser
RUN python -m rebrowser_playwright install chromium && \
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
RUN uv sync --locked --extra local-embeddings

# The production app serves prebuilt assets from src/family_assistant/static/dist.
# Keeping the frontend workspace in the runtime image also keeps npm lockfiles and
# build-only dependencies visible to vulnerability scanners.
USER root
RUN rm -rf /app/frontend
USER appuser

# --- Runtime Configuration ---
# Expose the port the web server listens on
EXPOSE 8000

# --- Versioning Information ---
# These are passed as build arguments and set as environment variables
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ENV GIT_COMMIT=$GIT_COMMIT
ENV BUILD_DATE=$BUILD_DATE

# Run as non-root user for security
# The application will run with reduced privileges
USER appuser

# Define the default command to run the application using zero-code OTel setup.
# Exporters default to none so a plain container launch does not try localhost.
ENV OTEL_SERVICE_NAME=family-assistant \
    OTEL_TRACES_EXPORTER=none \
    OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none
CMD ["opentelemetry-instrument", "family-assistant"]

# Alternatively, run using python -m:
# CMD ["python", "-m", "family_assistant"]
