#!/bin/bash
# Wrapper script to ensure virtual environment is activated when running codex

# shellcheck disable=SC1091
source /usr/local/bin/wrapper-common.sh

wrapper_common_setup

exec /home/claude/.npm-global/bin/codex "$@"
