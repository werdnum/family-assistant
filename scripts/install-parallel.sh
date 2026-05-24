#!/bin/bash
# Portable GNU Parallel installer
# Downloads and installs the GNU Parallel script for the current platform
# GNU Parallel is a Perl script, so no compilation or platform detection is needed.

set -e

PARALLEL_VERSION="20231122"
INSTALL_DIR="${1:-.venv/bin}"

install_parallel() {
    local base_url="https://ftp.gnu.org/gnu/parallel"
    local filename="parallel-${PARALLEL_VERSION}.tar.bz2"
    local download_url="${base_url}/${filename}"

    echo "📥 Downloading GNU Parallel ${PARALLEL_VERSION} from ${download_url}..."

    mkdir -p "$INSTALL_DIR"
    local absolute_install_dir
    absolute_install_dir=$(cd "$INSTALL_DIR" && pwd)

    local temp_dir
    temp_dir=$(mktemp -d)
    trap 'rm -rf "$temp_dir"' EXIT

    if ! curl -sL "$download_url" -o "${temp_dir}/${filename}"; then
        echo "❌ Failed to download GNU Parallel"
        return 1
    fi

    echo "📦 Extracting GNU Parallel..."
    tar -xjf "${temp_dir}/${filename}" -C "$temp_dir"

    local parallel_script="${temp_dir}/parallel-${PARALLEL_VERSION}/src/parallel"
    if [ ! -f "$parallel_script" ]; then
        echo "❌ Failed to find parallel script in archive"
        return 1
    fi

    cp "$parallel_script" "$absolute_install_dir/parallel"
    chmod +x "$absolute_install_dir/parallel"

    if "$absolute_install_dir/parallel" --version >/dev/null 2>&1; then
        echo "✅ GNU Parallel installed to $absolute_install_dir/parallel"
    else
        echo "❌ GNU Parallel installation verification failed (is perl installed?)"
        rm -f "$absolute_install_dir/parallel"
        return 1
    fi
}

echo "📦 Installing GNU Parallel..."

if [ -x "$INSTALL_DIR/parallel" ]; then
    echo "✓ GNU Parallel already installed at $INSTALL_DIR/parallel"
    exit 0
fi

if ! command -v perl >/dev/null 2>&1; then
    echo "❌ Perl is required but not installed"
    exit 1
fi

install_parallel

