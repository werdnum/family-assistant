#!/bin/bash
# The single pylint invocation for this repository.
#
# `scripts/format-and-lint.sh` (which `poe lint` and the CI lint job run) and
# `scripts/run-tests.sh` (which `poe test` runs) both go through here, so every
# entry point necessarily enforces the same result. They previously invoked
# pylint directly and drifted: one passed `--errors-only` and the other did not,
# so CI stayed green while `poe test` failed on the warning set `.pylintrc`
# deliberately enables.
#
# Callers pass paths only. Any option is refused, because an option is how the
# two entry points come apart again -- `--errors-only` and `--disable` narrow
# what is reported, `--rcfile` swaps the ruleset wholesale. Configure pylint in
# `.pylintrc`, which is the whole configuration (pylint reads only the first
# config file it finds, so `pyproject.toml` settings would be ignored).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [ $# -eq 0 ]; then
    echo "usage: $(basename "$0") <path> [path ...]" >&2
    exit 2
fi

for arg in "$@"; do
    case "$arg" in
        -*)
            echo "$(basename "$0"): refusing option '$arg'; this script takes paths only." >&2
            echo "Configure pylint in .pylintrc so every lint entry point agrees." >&2
            exit 2
            ;;
    esac
done

exec "${VIRTUAL_ENV:-$REPO_ROOT/.venv}/bin/pylint" "$@"
