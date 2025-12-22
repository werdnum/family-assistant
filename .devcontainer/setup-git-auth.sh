#!/bin/bash
set -e

# Configure GitHub App authentication if environment variables are present
HAS_VALID_KEY=false
if [ -n "$GITHUB_APP_PRIVATE_KEY_FILE" ] && [ -f "$GITHUB_APP_PRIVATE_KEY_FILE" ]; then
    # Verify the file looks like a private key to avoid using the dummy mount
    if grep -q "PRIVATE KEY" "$GITHUB_APP_PRIVATE_KEY_FILE"; then
        HAS_VALID_KEY=true
    fi
fi

if [ "$HAS_VALID_KEY" = "true" ] && [ -n "$GITHUB_APP_ID" ] && [ -n "$GITHUB_APP_INSTALLATION_ID" ]; then
    echo "Configuring GitHub App authentication..."

    # Ensure .local/bin exists
    mkdir -p /home/claude/.local/bin

    # Configure git credential helper
    # We use a shell function in the config to execute gh-token
    # We bake the environment variable values into the config because tools like 'claude code'
    # may strip the environment variables when running git commands
    git config --global credential.helper "!f() { \
        TOKEN=\$(/usr/local/bin/gh-token generate -k \"$GITHUB_APP_PRIVATE_KEY_FILE\" -i \"$GITHUB_APP_ID\" -n \"$GITHUB_APP_INSTALLATION_ID\"); \
        echo \"username=x-access-token\"; \
        echo \"password=\$TOKEN\"; \
    }; f"

    # Configure git to use HTTPS instead of SSH for GitHub
    # This ensures that tools trying to use SSH URLs (like claude-code)
    # will be redirected to HTTPS where our credential helper works
    git config --global url."https://github.com/".insteadOf "git@github.com:"

    echo "Git credential helper configured."
else
    echo "GitHub App authentication skipped (missing configuration or valid key file)."
fi
