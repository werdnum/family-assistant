FROM ubuntu:24.04

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# System essentials
RUN apt-get update && apt-get install -y \
    curl git wget gnupg ca-certificates \
    ripgrep fd-find micro unzip \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create symbolic link for fd (fd-find installs as fdfind)
RUN ln -s /usr/bin/fdfind /usr/bin/fd

# Development tools not in pyproject.toml
# ast-grep is required by CLAUDE.md instructions
# Detect architecture and download appropriate binary
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        AST_GREP_ARCH="x86_64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        AST_GREP_ARCH="aarch64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -sSL "https://github.com/ast-grep/ast-grep/releases/latest/download/app-${AST_GREP_ARCH}-unknown-linux-gnu.zip" \
    -o /tmp/ast-grep.zip && \
    unzip /tmp/ast-grep.zip -d /tmp/ && \
    mv /tmp/sg /usr/local/bin/ast-grep && \
    chmod +x /usr/local/bin/ast-grep && \
    rm /tmp/ast-grep.zip

# Node.js 20 LTS for Claude Code and npm packages
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Python 3.11 (Ubuntu 24.04 has 3.12 by default, but project might need 3.11)
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install jq for JSON processing and PostgreSQL client
RUN apt-get update && apt-get install -y jq postgresql-client && rm -rf /var/lib/apt/lists/*

# Download and install claude-code-webui binary
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        WEBUI_ARCH="linux-amd64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        WEBUI_ARCH="linux-arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -sSL "https://github.com/sugyan/claude-code-webui/releases/latest/download/claude-code-webui-${WEBUI_ARCH}" \
    -o /usr/local/bin/claude-code-webui && \
    chmod +x /usr/local/bin/claude-code-webui

# Create claude user with UID 1001 to match Kubernetes deployment
RUN useradd -m -s /bin/bash -u 1001 claude && \
    mkdir -p /home/claude/.cache/uv && \
    mkdir -p /home/claude/.npm-global && \
    mkdir -p /home/claude/.cache/deno && \
    mkdir -p /home/claude/.deno && \
    chown -R claude:claude /home/claude

# Set up npm global directory for claude user
ENV NPM_CONFIG_PREFIX=/home/claude/.npm-global
ENV PATH="/usr/local/bin:/home/claude/.npm-global/bin:/home/claude/.deno/bin:$PATH"

# Switch to claude user for user-specific installations
USER claude

# Install Deno for MCP servers as claude user
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/home/claude/.deno sh

# Add uv tools to PATH before installing - maintaining /usr/local/bin first
ENV PATH="/usr/local/bin:/home/claude/.local/bin:$PATH"

# Install Claude Code CLI as claude user
RUN npm install -g @anthropic-ai/claude-code
RUN npm install -g @google/gemini-cli

# Install LLM tools globally using uv tool install with plugins
RUN uv tool install --with llm-gemini --with llm-openrouter --with llm-fragments-github llm

# Set Playwright browsers path to user's home to avoid permission issues
ENV PLAYWRIGHT_BROWSERS_PATH=/home/claude/.cache/playwright-browsers

# Switch back to root to install system dependencies first
USER root

# Install Playwright system dependencies (must be done as root before installing browsers)
RUN npx playwright install-deps chromium

# Switch back to claude user
USER claude

# Install Playwright globally as claude user (after system deps are installed)
RUN npm install -g playwright && \
    npx playwright install chromium

# Switch back to root for remaining system-level operations
USER root

# Install Python dependencies for scraper MCP (contrib/scrape_mcp.py)
RUN uv venv /venv && \
    uv pip install -p /venv/bin/python --system "playwright>=1.0" "markitdown[html]>=0.1.0" && \
    /venv/bin/playwright install chromium && \
    chown -R claude:claude /venv

# Ensure claude user can access the virtual environment
RUN chmod -R 755 /venv

# Create workspace directory
WORKDIR /workspace

# Copy default Claude settings
RUN mkdir -p /opt/claude-settings
COPY settings.local.json /opt/claude-settings/

# Create setup script and claude wrapper
COPY setup-workspace.sh /usr/local/bin/
COPY claude-wrapper.sh /usr/local/bin/claude-wrapper
COPY throttle_backspaces.py /usr/local/bin/
RUN chmod +x /usr/local/bin/setup-workspace.sh /usr/local/bin/claude-wrapper /usr/local/bin/throttle_backspaces.py

# Create symlink for claude command that uses the wrapper
RUN ln -sf /usr/local/bin/claude-wrapper /usr/local/bin/claude

# Switch to claude user
USER claude

# Set up entry point that handles both interactive and service modes
ENTRYPOINT ["/usr/local/bin/setup-workspace.sh"]
CMD ["claude"]