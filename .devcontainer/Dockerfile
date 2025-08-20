# Build stage for shpool
FROM rust:latest AS shpool-builder
RUN curl -L --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/cargo-bins/cargo-binstall/main/install-from-binstall-release.sh | bash \
    && cargo binstall -y shpool

FROM ubuntu:24.04

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# System essentials and Python runtime
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y \
        curl git wget gnupg ca-certificates lsb-release \
        ripgrep fd-find micro unzip build-essential \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Create symbolic link for fd (fd-find installs as fdfind)
RUN ln -s /usr/bin/fdfind /usr/bin/fd

# Copy shpool binary from builder stage
COPY --from=shpool-builder /usr/local/cargo/bin/shpool /usr/local/bin/shpool

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


# Install uv for fast Python package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install PostgreSQL 17 with pgvector extension and jq/yq for JSON/YAML processing
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    wget -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgres.gpg && \
    sh -c 'echo "deb [signed-by=/etc/apt/keyrings/postgres.gpg] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list' && \
    apt-get update && \
    apt-get install -y \
        jq \
        postgresql-client-17 \
        postgresql-17 \
        postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/* && \
    # Install yq for YAML/JSON processing
    ARCH=$(dpkg --print-architecture) && \
    YQ_VERSION="v4.47.1" && \
    curl -sSL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${ARCH}" -o /usr/local/bin/yq && \
    chmod +x /usr/local/bin/yq

# Create claude user with UID 1001 to match Kubernetes deployment
RUN useradd -m -s /bin/bash -u 1001 claude && \
    mkdir -p /home/claude/.cache/uv && \
    mkdir -p /home/claude/.npm-global && \
    mkdir -p /home/claude/.cache/deno && \
    mkdir -p /home/claude/.deno && \
    chown -R claude:claude /home/claude && \
    mkdir -p /opt && chown -R claude:claude /opt

# Set up npm global directory for claude user
ENV NPM_CONFIG_PREFIX=/home/claude/.npm-global
ENV PATH="/usr/local/bin:/home/claude/.npm-global/bin:/home/claude/.deno/bin:$PATH"

# Switch to claude user for user-specific installations
USER claude

# Install Deno for MCP servers as claude user
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/home/claude/.deno sh

# Add uv tools to PATH before installing - maintaining /usr/local/bin first
ENV PATH="/usr/local/bin:/home/claude/.local/bin:/usr/lib/postgresql/17/bin:$PATH"

# Install Claude Code CLI and LLM tools as claude user
RUN npm install -g @anthropic-ai/claude-code @google/gemini-cli && \
    uv tool install --with llm-gemini --with llm-openrouter --with llm-fragments-github llm

# Set Playwright browsers path to user's home to avoid permission issues
ENV PLAYWRIGHT_BROWSERS_PATH=/home/claude/.cache/playwright-browsers

# Download and install claudecodewui binary
RUN git clone --depth 1 --branch main https://github.com/siteboon/claudecodeui.git /opt/claude-code-webui && \
    (cd /opt/claude-code-webui && npm install && npm run build)

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

# Install Python dependencies for scraper MCP and jsonmerge for oneshot config
RUN uv venv /venv && \
    uv pip install -p /venv/bin/python --system "playwright>=1.0" "markitdown[html]>=0.1.0" "jsonmerge>=1.9.0" && \
    /venv/bin/playwright install chromium && \
    chown -R claude:claude /venv

# Ensure claude user can access the virtual environment
RUN chmod -R 755 /venv

# Create workspace directory
WORKDIR /workspace

# Copy default Claude settings
RUN mkdir -p /opt/claude-settings
COPY settings.local.json /opt/claude-settings/

# Copy oneshot configuration
COPY oneshot-config /opt/oneshot-config/

# Create setup script and claude wrapper
COPY setup-workspace.sh /usr/local/bin/
COPY claude-wrapper.sh /usr/local/bin/claude-wrapper
COPY throttle_backspaces.py /usr/local/bin/
COPY merge-settings.py /usr/local/bin/
RUN chmod +x /usr/local/bin/setup-workspace.sh /usr/local/bin/claude-wrapper /usr/local/bin/throttle_backspaces.py /usr/local/bin/merge-settings.py

# Create symlink for claude command that uses the wrapper
RUN ln -sf /usr/local/bin/claude-wrapper /usr/local/bin/claude

# Switch to claude user
USER claude

# Set up entry point that handles both interactive and service modes
ENTRYPOINT ["/usr/local/bin/setup-workspace.sh"]
CMD ["claude"]
