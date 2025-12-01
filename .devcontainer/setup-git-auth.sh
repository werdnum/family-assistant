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
    # We refer to the environment variables so they can be changed without re-running setup
    git config --global credential.helper "!f() { \
        TOKEN=\$(gh-token generate -k \"\$GITHUB_APP_PRIVATE_KEY_FILE\" -i \"\$GITHUB_APP_ID\" -n \"\$GITHUB_APP_INSTALLATION_ID\"); \
        echo \"username=x-access-token\"; \
        echo \"password=\$TOKEN\"; \
    }; f"

    echo "Git credential helper configured."
else
    echo "GitHub App authentication skipped (missing configuration or valid key file)."
fi
