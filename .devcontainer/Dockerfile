# Multi-stage build for the development container.
# Stage 1: Base image setup (from the old Dockerfile.base)

# Pinned tool versions. Bump these deliberately: an unpinned "latest" makes two
# builds of the same commit differ, which is how the image drifted away from
# what the project actually pins. PLAYWRIGHT_VERSION must match the playwright
# pin in pyproject.toml, which is itself tied to rebrowser-playwright so the two
# share browser binaries.
ARG UV_VERSION=0.12.15
ARG NODE_MAJOR=22
# gh-token tags releases v2.x but its go.mod never took the /v2 module path,
# so the v2 tags are unresolvable and the proxy only serves pseudo-versions.
ARG GH_TOKEN_VERSION=v0.0.0-20260325123437-08e7acb18e22
ARG AST_GREP_VERSION=0.45.3
ARG YQ_VERSION=v4.47.1
ARG PLAYWRIGHT_VERSION=1.52.0
ARG CODEX_VERSION=0.154.0

# COPY --from cannot expand a build arg, but FROM can: name the stage here.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# Build stage for gh-token
FROM golang:1.24 AS gh-token-builder
ARG GH_TOKEN_VERSION
RUN go install "github.com/Link-/gh-token@${GH_TOKEN_VERSION}"

FROM ubuntu:24.04 AS base

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# System packages and dependencies (cache-bust: add shellcheck 2026-01-06)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y \
        curl git wget gnupg ca-certificates lsb-release \
        ripgrep fd-find micro unzip build-essential \
        python3 python3-pip python3-venv \
        ffmpeg libturbojpeg0-dev shellcheck parallel \
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
ARG NODE_MAJOR
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y nodejs

# Binary tools
COPY --from=gh-token-builder /go/bin/gh-token /usr/local/bin/gh-token
COPY --from=uv /uv /uvx /bin/

ARG AST_GREP_VERSION
ARG YQ_VERSION
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        AST_GREP_ARCH="x86_64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        AST_GREP_ARCH="aarch64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -sSL "https://github.com/ast-grep/ast-grep/releases/download/${AST_GREP_VERSION}/app-${AST_GREP_ARCH}-unknown-linux-gnu.zip" \
    -o /tmp/ast-grep.zip && \
    unzip /tmp/ast-grep.zip -d /tmp/ && \
    install -m 0755 /tmp/ast-grep /usr/local/bin/ast-grep && \
    install -m 0755 /tmp/sg /usr/local/bin/sg && \
    rm /tmp/ast-grep /tmp/sg /tmp/ast-grep.zip && \
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

# System-level Playwright setup
USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/root/.npm,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libasound2t64 \
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
        fonts-unifont

# Python venv with rebrowser-playwright and playwright pinned to the same
# version so they share browser binaries. rebrowser-playwright is used by app
# code, playwright by pytest-playwright-asyncio for functional tests.
ARG PLAYWRIGHT_VERSION
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv venv /venv && \
    uv pip install -p /venv/bin/python --system \
        "rebrowser-playwright==${PLAYWRIGHT_VERSION}" \
        "playwright==${PLAYWRIGHT_VERSION}" \
        "markitdown[html]>=0.1.0" "jsonmerge>=1.9.0" && \
    chown -R claude:claude /venv && \
    chmod -R 755 /venv

# Install chromium as the user that owns the shared browser cache.
USER claude
RUN /venv/bin/python -m playwright install chromium
USER root

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /workspace
USER claude

# Stage 2: Development tools and application-specific setup
FROM base

# Switch to claude user for dev tool installations
USER claude

# Install Claude Code using native installer (recommended over deprecated npm install)
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install development Node.js packages
ARG CODEX_VERSION
RUN --mount=type=cache,target=/home/claude/.npm,sharing=locked,uid=1001,gid=1001 \
    npm install -g "@openai/codex@${CODEX_VERSION}"

# Install Antigravity CLI (agy) - installs to /home/claude/.local/bin/agy
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash

# Install Python development tools
RUN --mount=type=cache,target=/home/claude/.cache/uv,sharing=locked,uid=1001,gid=1001 \
    uv tool install --with llm-gemini --with llm-openrouter --with llm-fragments-github llm && \
    uv tool install poethepoet

# Application-specific setup
USER root
RUN mkdir -p /opt/claude-settings
COPY .devcontainer/settings.local.json /opt/claude-settings/
COPY .devcontainer/CLAUDE.devcontainer.md /opt/claude-settings/
COPY .devcontainer/oneshot-config /opt/oneshot-config/
COPY .devcontainer/setup-workspace.sh /usr/local/bin/
COPY .devcontainer/setup-git-auth.sh /usr/local/bin/
COPY .devcontainer/gh-wrapper /usr/local/bin/gh
COPY .devcontainer/wrapper-common.sh /usr/local/bin/wrapper-common.sh
COPY .devcontainer/claude-wrapper.sh /usr/local/bin/claude-wrapper
COPY .devcontainer/codex-wrapper.sh /usr/local/bin/codex-wrapper
COPY .devcontainer/throttle_backspaces.py /usr/local/bin/
COPY .devcontainer/merge-settings.py /usr/local/bin/
RUN chmod +x /usr/local/bin/setup-workspace.sh \
               /usr/local/bin/setup-git-auth.sh \
               /usr/local/bin/gh \
               /usr/local/bin/wrapper-common.sh \
               /usr/local/bin/claude-wrapper \
               /usr/local/bin/codex-wrapper \
               /usr/local/bin/throttle_backspaces.py \
               /usr/local/bin/merge-settings.py && \
    ln -sf /usr/local/bin/claude-wrapper /usr/local/bin/claude && \
    ln -sf /usr/local/bin/codex-wrapper /usr/local/bin/codex

# Final user and entrypoint
USER claude
ENTRYPOINT ["/usr/local/bin/setup-workspace.sh"]
CMD ["sleep", "infinity"]
