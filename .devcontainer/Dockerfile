# Development container image with all dev tools
# Extends the base image with interactive development tools

ARG BASE_IMAGE=family-assistant-dev-base:latest
FROM ${BASE_IMAGE}

# Switch to claude user for dev tool installations
USER claude

# Install development Node.js packages (claude-code and gemini-cli only)
# Playwright is already installed in the base image
RUN --mount=type=cache,target=/home/claude/.npm,sharing=locked,uid=1001,gid=1001 \
    npm install -g @anthropic-ai/claude-code @google/gemini-cli@nightly

# Install Python development tools (llm with plugins)
RUN --mount=type=cache,target=/home/claude/.cache/uv,sharing=locked,uid=1001,gid=1001 \
    uv tool install --with llm-gemini --with llm-openrouter --with llm-fragments-github llm

# Build claudecodeui (pinned to specific commit for cache stability)
# Pinned commit: 3c9a4cab82 (latest as of 2025-10-01)
RUN --mount=type=cache,target=/home/claude/.npm,sharing=locked,uid=1001,gid=1001 \
    git clone --depth 1 --branch main https://github.com/siteboon/claudecodeui.git /opt/claude-code-webui && \
    cd /opt/claude-code-webui && \
    git fetch origin 533d589 && \
    git checkout 533d589 && \
    npm install && \
    npm run build

# Switch back to root for application-specific setup
USER root

# Application-specific files (from project root context)
# Copy default Claude settings
RUN mkdir -p /opt/claude-settings
COPY .devcontainer/settings.local.json /opt/claude-settings/

# Copy oneshot configuration (from project root context)
COPY .devcontainer/oneshot-config /opt/oneshot-config/

# Create setup script and claude wrapper (from project root context)
COPY .devcontainer/setup-workspace.sh /usr/local/bin/
COPY .devcontainer/claude-wrapper.sh /usr/local/bin/claude-wrapper
COPY .devcontainer/throttle_backspaces.py /usr/local/bin/
COPY .devcontainer/merge-settings.py /usr/local/bin/
RUN chmod +x /usr/local/bin/setup-workspace.sh /usr/local/bin/claude-wrapper /usr/local/bin/throttle_backspaces.py /usr/local/bin/merge-settings.py

# Create symlink for claude command that uses the wrapper
RUN ln -sf /usr/local/bin/claude-wrapper /usr/local/bin/claude

# Switch to claude user
USER claude

# Set up entry point that handles both interactive and service modes
ENTRYPOINT ["/usr/local/bin/setup-workspace.sh"]
CMD ["claude"]
