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

# Install Deno for MCP servers
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:$PATH"

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code
RUN npm install -g @google/gemini-cli

# Install jq for JSON processing and PostgreSQL client
RUN apt-get update && apt-get install -y jq postgresql-client && rm -rf /var/lib/apt/lists/*

# Install Playwright globally and its system dependencies
# This ensures the dependencies are available for any Python environment
RUN npm install -g playwright && \
    npx playwright install --with-deps chromium

# Install Python dependencies for scraper MCP (contrib/scrape_mcp.py)
RUN uv venv /venv
RUN uv pip install -p /venv/bin/python --system "playwright>=1.0" "markitdown[html]>=0.1.0"
RUN /venv/bin/playwright install --with-deps chromium

# Create claude user with UID 1001 to match Kubernetes deployment
RUN useradd -m -s /bin/bash -u 1001 claude && \
    mkdir -p /home/claude/.cache/uv && \
    chown -R claude:claude /home/claude

# Create workspace directory
WORKDIR /workspace

# Create setup script
COPY setup-workspace.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/setup-workspace.sh

# Set up entry point that handles both interactive and service modes
ENTRYPOINT ["/usr/local/bin/setup-workspace.sh"]
CMD ["claude"]