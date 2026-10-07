#!/bin/bash
# Optional command for the `claude` compose service, in place of
# `sleep infinity`: a long-running `claude remote-control` server for
# claude.ai/code and the Claude mobile app. See README.md.
#
# Claude Code lives in the persistent home volume and self-updates there, so
# the copy baked into the image is shadowed and an image refresh alone never
# updates it. A long-running server also keeps executing whatever version it
# started with. Updating here, before the server starts, means restarting the
# container is all it takes to move the server to the latest release.
#
# Restarting does not lose sessions: their transcripts are stored server-side,
# and a fresh `claude remote-control` in the same directory picks them back up
# for about four hours after the previous server stopped. If the previous
# server is still releasing this directory, the new one reports that the
# folder is already served and exits; the service's restart policy retries.

set -euo pipefail

# shellcheck disable=SC1091
source /usr/local/bin/wrapper-common.sh

if ! CLAUDE_BIN=$(wrapper_common_claude_bin); then
    echo "❌ No Claude Code binary found under ~/.local/bin or ~/.npm-global/bin" >&2
    exit 127
fi

# An update failure (e.g. the network is down) should not keep the server
# offline: serving on the installed version is better than not serving.
"$CLAUDE_BIN" update || echo "⚠️  claude update failed; starting the installed version" >&2

exec /usr/local/bin/claude-wrapper remote-control \
    --name "${REMOTE_CONTROL_NAME:-devcontainer-${WORKTREE_NAME:-main}}" \
    --permission-mode "${REMOTE_CONTROL_PERMISSION_MODE:-default}"

