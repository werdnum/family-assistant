#!/bin/bash
set -e

# Configure GitHub App authentication if environment variables are present
if [ -n "$GITHUB_APP_PRIVATE_KEY" ] && [ -n "$GITHUB_APP_ID" ] && [ -n "$GITHUB_APP_INSTALLATION_ID" ]; then
    echo "Configuring GitHub App authentication..."

    # Ensure .local/bin exists
    mkdir -p /home/claude/.local/bin

    # Configure git credential helper
    # We use a shell function in the config to execute gh-token
    # We hardcode the values from current environment
    git config --global credential.helper "!f() { \
        TOKEN=\$(gh-token generate -k \"$GITHUB_APP_PRIVATE_KEY\" -i \"$GITHUB_APP_ID\" -n \"$GITHUB_APP_INSTALLATION_ID\"); \
        echo \"username=x-access-token\"; \
        echo \"password=\$TOKEN\"; \
    }; f"

    echo "Git credential helper configured."

    # Create gh wrapper
    cat > /home/claude/.local/bin/gh <<EOF
#!/bin/bash
export GH_TOKEN=\$(gh-token generate -k "$GITHUB_APP_PRIVATE_KEY" -i "$GITHUB_APP_ID" -n "$GITHUB_APP_INSTALLATION_ID")
exec /usr/bin/gh "\$@"
EOF
    chmod +x /home/claude/.local/bin/gh

    echo "gh CLI wrapper configured at /home/claude/.local/bin/gh"
else
    echo "GitHub App authentication skipped (missing environment variables)."
fi
