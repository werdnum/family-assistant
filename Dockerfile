# Use the official UV image as the base
FROM ghcr.io/astral-sh/uv:debian-slim AS base

# Install system dependencies: npm for Node.js MCP servers
# Using --mount for caching apt downloads
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    # Add CA certificates for HTTPS communication
    ca-certificates \
    curl \
    unzip \
    && \
    # Ensure certificates are updated after installing the package
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
ENV PATH="/app/.venv/bin:$PATH" # Activate venv

# Copy only pyproject.toml first to leverage Docker layer caching for dependencies
COPY pyproject.toml ./

# Install dependencies using uv from pyproject.toml
# Using --mount with the explicit UV_CACHE_DIR for caching pip downloads/builds
# Note: Installing with '.' here installs dependencies AND the package in editable mode
# which might not be ideal for a final image, but useful for development builds.
# For a production image, consider two steps:
# 1. uv pip install --system --only-deps .
# 2. uv pip install --system --no-deps .
RUN --mount=type=cache,target=${UV_CACHE_DIR} \
    uv pip install . # Installs dependencies from pyproject.toml and the package

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

# Install Node.js MCP tools globally using Deno, providing explicit names
RUN deno install --global -A --name playwright-mcp npm:@playwright/mcp@latest
RUN deno install --global -A --name brave-search-mcp-server npm:@modelcontextprotocol/server-brave-search

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
    UV_CACHE_DIR=/uv-cache

# Update PATH separately
ENV PATH="${UV_TOOL_BIN_DIR}:/root/.deno/bin:/usr/local/bin:${PATH}"

# --- Copy Application Code ---
# Copy the source code into the image
COPY src/ /app/src/

# Copy configuration files, templates, and static assets to the WORKDIR
# These need to be accessible relative to the WORKDIR at runtime when running the app
COPY prompts.yaml mcp_config.json ./
# The templates/static files are now *inside* the package, so they don't need
# to be copied separately here if the web server loads them relative to the package path.
# If web_server.py fails to find them, uncomment these lines:
# COPY src/family_assistant/templates /app/src/family_assistant/templates
# COPY src/family_assistant/static /app/src/family_assistant/static

# --- Install the Package ---
# This step might be redundant if `uv pip install .` in the previous step
# already installed the package from the copied pyproject.toml.
# However, explicitly installing it after copying the 'src' ensures the code is included.
# Use --no-deps as dependencies should already be installed.
RUN --mount=type=cache,target=${UV_CACHE_DIR} \
    uv pip install . --no-deps # Install the package itself from the copied src

# --- Linting Step (Optional but recommended) ---
# Run linter (e.g., pylint) on the source code *after* copying it
# Ensure pylint is installed (add to [project.optional-dependencies]dev in pyproject.toml)
# RUN echo "Running pylint..." && \
#     pylint --errors-only src/family_assistant || \
#     (echo "Pylint found errors. Please fix them." && exit 1)


# --- Runtime Configuration ---
# Expose the port the web server listens on
EXPOSE 8000

# Define the default command to run the application using the installed entry point
# This uses the [project.scripts] defined in pyproject.toml
CMD ["family-assistant"]

# Alternatively, run using python -m:
# CMD ["python", "-m", "family_assistant"]
