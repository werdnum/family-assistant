# Use the official UV image as the base
FROM ghcr.io/astral-sh/uv:debian-slim AS base

# Install system dependencies: npm for Node.js MCP servers
# Using --mount for caching apt downloads
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \ # Add CA certificates
    curl \
    unzip \
    && \
    update-ca-certificates && \ # Ensure certificates are updated
    # Clean up apt cache to reduce image size
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# --- Install Python Dependencies ---
# Define the cache directory for uv
ENV UV_CACHE_DIR=/uv-cache
# Copy only the requirements file first to leverage Docker cache
COPY requirements.txt .
# Create a virtual environment
RUN uv venv /app/.venv
# Install Python dependencies into the virtual environment
# Note: uv pip install automatically detects and uses .venv in the current dir if it exists
# We don't need --system anymore.
# Using --mount with the explicit UV_CACHE_DIR for caching pip downloads/builds
RUN --mount=type=cache,target=${UV_CACHE_DIR} \
    uv pip install -r requirements.txt

# --- Install Deno ---
ARG DENO_VERSION=v2.2.11
ARG TARGETARCH=amd64 # Default target architecture, Docker buildx sets this automatically
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

# Install Node.js MCP tools globally using Deno
RUN deno install --global -A npm:@playwright/mcp@latest
RUN deno install --global -A npm:@modelcontextprotocol/server-brave-search

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
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_TOOL_BIN_DIR=/uv/bin \
    UV_TOOL_DIR=/uv/tools \
    UV_CACHE_DIR=/uv-cache \
    # Add uv tool bin, deno bin, and default path to PATH
    PATH="${UV_TOOL_BIN_DIR}:/root/.deno/bin:/usr/local/bin:${PATH}"

# --- Copy Application Code ---
# Copy the rest of the application code and configuration
COPY main.py processing.py storage.py web_server.py calendar_integration.py ./
COPY prompts.yaml mcp_config.json ./
COPY templates/ ./templates/

# --- Runtime Configuration ---
# Expose the port the web server listens on
EXPOSE 8000

# Define the default command to run the application using the venv's python
CMD ["/app/.venv/bin/python", "main.py"]
