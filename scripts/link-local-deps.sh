#!/bin/bash
# Put this checkout's .venv and frontend/node_modules on the local APFS volume
# when the checkout itself lives on a filesystem that cannot clone files.
#
# The macOS dev VM keeps checkouts on a virtiofs share, while uv's cache is on
# the VM's own APFS disk. Across that boundary uv cannot clone or hardlink, so
# every worktree pays a full copy: ~1.9GB of venv plus ~540MB of node_modules.
# On the APFS side the same venv costs ~20MB (uv clones from its cache) and
# node_modules ~15MB (cloned from a template keyed by package-lock.json).
#
# Each checkout gets its own venv and node_modules, never a shared one: a
# shared venv's editable install points at whichever checkout synced last.
#
# Idempotent. Does nothing off macOS, when the checkout is already on the same
# filesystem as the deps directory, or for a dependency that already exists.
# scripts/prune-worktrees.sh removes the deps of checkouts that are gone.
set -euo pipefail

[ "$(uname)" = "Darwin" ] || exit 0

root="$(git rev-parse --show-toplevel)"
deps_root="${FA_LOCAL_DEPS_DIR:-$HOME/.cache/fa-worktree-deps}"
mkdir -p "$deps_root/checkouts" "$deps_root/npm-templates"

if [ "$(stat -f %d "$root")" = "$(stat -f %d "$deps_root")" ]; then
    exit 0
fi

checkout_id="$(basename "$root")-$(printf '%s' "$root" | shasum -a 256 | cut -c1-8)"
dir="$deps_root/checkouts/$checkout_id"
mkdir -p "$dir"
printf '%s\n' "$root" >"$dir/checkout-path"

if [ ! -e "$root/.venv" ] && [ ! -L "$root/.venv" ]; then
    echo "📦 Creating venv on the local disk: $dir/venv"
    rm -rf "$dir/venv"
    (cd "$root" && uv venv -q "$dir/venv")
    ln -s "$dir/venv" "$root/.venv"
    (cd "$root" && uv sync -q --frozen --extra dev --extra pgserver)
fi

lockfile="$root/frontend/package-lock.json"
if [ -f "$lockfile" ]; then
    lock_key="$(shasum -a 256 "$lockfile" | cut -c1-16)"
    link="$root/frontend/node_modules"
    # The target must itself be named node_modules: Node resolves a package's
    # dependencies by walking up to the nearest directory with that name.
    target="$dir/node_modules"
    stale=false
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ] &&
        [ "$(cat "$dir/node_modules.lock-key" 2>/dev/null)" != "$lock_key" ]; then
        stale=true
    fi
    if { [ ! -e "$link" ] && [ ! -L "$link" ]; } || [ "$stale" = true ]; then
        template="$deps_root/npm-templates/$lock_key"
        if [ ! -f "$template/.complete" ]; then
            echo "🎨 Building node_modules template for lockfile $lock_key"
            building="$(mktemp -d "$deps_root/npm-templates/.building.XXXXXX")"
            cp "$root/frontend/package.json" "$lockfile" "$building/"
            (cd "$building" && npm ci --no-audit --no-fund --loglevel=error)
            touch "$building/.complete"
            if [ -f "$template/.complete" ]; then
                # A concurrent session finished the same template first.
                rm -rf "$building"
            else
                rm -rf "$template"
                mv "$building" "$template"
            fi
        fi
        echo "🎨 Cloning node_modules to the local disk: $target"
        rm -rf "$target"
        cp -c -R "$template/node_modules" "$target"
        printf '%s\n' "$lock_key" >"$dir/node_modules.lock-key"
        rm -f "$link"
        ln -s "$target" "$link"
    fi
fi
