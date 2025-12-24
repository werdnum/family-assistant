# Multi-stage build for the development container.
# Stage 1: Base image setup (from the old Dockerfile.base)

# Build stage for shpool
FROM rust:latest AS shpool-builder
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/usr/local/cargo/git,sharing=locked \
    curl -L --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/cargo-bins/cargo-binstall/main/install-from-binstall-release.sh | bash \
    && cargo binstall -y shpool

# Build stage for gh-token
FROM golang:1.24 AS gh-token-builder
RUN go install github.com/Link-/gh-token@latest

FROM ubuntu:24.04 AS base

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# System packages and dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y \
        curl git wget gnupg ca-certificates lsb-release \
        ripgrep fd-find micro unzip build-essential \
        python3 python3-pip python3-venv \
        ffmpeg libturbojpeg0-dev \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && ln -s /usr/bin/fdfind /usr/bin/fd

# PostgreSQL repository and client
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    wget -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgres.gpg && \
    sh -c 'echo "deb [signed-by=/etc/apt/keyrings/postgres.gpg] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list' && \
    apt-get update && \
    apt-get install -y \
        jq \
        postgresql-client-17 \
        postgresql-17 \
        postgresql-17-pgvector

# Node.js
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Binary tools
COPY --from=shpool-builder /usr/local/cargo/bin/shpool /usr/local/bin/shpool
COPY --from=gh-token-builder /go/bin/gh-token /usr/local/bin/gh-token
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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
    install -m 0755 /tmp/ast-grep /usr/local/bin/ast-grep && \
    install -m 0755 /tmp/sg /usr/local/bin/sg && \
    rm /tmp/ast-grep /tmp/sg /tmp/ast-grep.zip && \
    YQ_VERSION="v4.47.1" && \
    curl -sSL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${ARCH}" -o /usr/local/bin/yq && \
    chmod +x /usr/local/bin/yq

# User setup
RUN useradd -m -s /bin/bash -u 1001 claude && \
    mkdir -p /home/claude/.npm-global \
             /home/claude/.cache/deno \
             /home/claude/.deno \
             /home/claude/.cache/playwright-browsers \
             /opt && \
    chown -R claude:claude /home/claude /opt

# Environment variables
ENV NPM_CONFIG_PREFIX=/home/claude/.npm-global
ENV PATH="/usr/local/bin:/home/claude/.npm-global/bin:/home/claude/.deno/bin:/home/claude/.local/bin:/usr/lib/postgresql/17/bin:$PATH"
ENV PLAYWRIGHT_BROWSERS_PATH=/home/claude/.cache/playwright-browsers
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# User-specific installations
USER claude
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/home/claude/.deno sh
RUN --mount=type=cache,target=/home/claude/.npm,sharing=locked,uid=1001,gid=1001 \
    npm install -g playwright@1.55.0

# System-level Playwright setup
USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/root/.npm,sharing=locked \
    npx --yes playwright@1.55.0 install-deps chromium && \
    npx --yes playwright@1.55.0 install chromium

# Python venv with shared Playwright browsers
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv venv /venv && \
    uv pip install -p /venv/bin/python --system "playwright==1.55.0" "markitdown[html]>=0.1.0" "jsonmerge>=1.9.0" && \
    chown -R claude:claude /venv && \
    chmod -R 755 /venv

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /workspace
USER claude

# Stage 2: Development tools and application-specific setup
FROM base

# Switch to claude user for dev tool installations
USER claude

# Install development Node.js packages
RUN --mount=type=cache,target=/home/claude/.npm,sharing=locked,uid=1001,gid=1001 \
    npm install -g @anthropic-ai/claude-code @google/gemini-cli@nightly

# Install Python development tools
RUN --mount=type=cache,target=/home/claude/.cache/uv,sharing=locked,uid=1001,gid=1001 \
    uv tool install --with llm-gemini --with llm-openrouter --with llm-fragments-github llm

# Install happy-coder
RUN --mount=type=cache,target=/home/claude/.npm,sharing=locked,uid=1001,gid=1001 \
    npm install -g happy-coder

# Application-specific setup
USER root
RUN mkdir -p /opt/claude-settings
COPY .devcontainer/settings.local.json /opt/claude-settings/
COPY .devcontainer/oneshot-config /opt/oneshot-config/
COPY .devcontainer/setup-workspace.sh /usr/local/bin/
COPY .devcontainer/setup-git-auth.sh /usr/local/bin/
COPY .devcontainer/gh-wrapper /usr/local/bin/gh
COPY .devcontainer/claude-wrapper.sh /usr/local/bin/claude-wrapper
COPY .devcontainer/throttle_backspaces.py /usr/local/bin/
COPY .devcontainer/merge-settings.py /usr/local/bin/
RUN chmod +x /usr/local/bin/setup-workspace.sh \
               /usr/local/bin/setup-git-auth.sh \
               /usr/local/bin/gh \
               /usr/local/bin/claude-wrapper \
               /usr/local/bin/throttle_backspaces.py \
               /usr/local/bin/merge-settings.py && \
    ln -sf /usr/local/bin/claude-wrapper /usr/local/bin/claude

# Final user and entrypoint
USER claude
ENTRYPOINT ["/usr/local/bin/setup-workspace.sh"]
CMD ["sh", "-c", "happy daemon start-sync"]
