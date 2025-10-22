#!/bin/bash
# Portable shellcheck installer
# Downloads and installs shellcheck binary for the current platform

set -e

SHELLCHECK_VERSION="stable"
INSTALL_DIR="${1:-.venv/bin}"

# Detect OS and architecture
detect_platform() {
    local os
    local arch

    # Detect OS
    case "$(uname -s)" in
        Linux*)     os="linux";;
        Darwin*)    os="darwin";;
        CYGWIN*|MINGW*|MSYS*) os="windows";;
        *)
            echo "❌ Unsupported OS: $(uname -s)"
            exit 1
            ;;
    esac

    # Detect architecture
    case "$(uname -m)" in
        x86_64)     arch="x86_64";;
        aarch64|arm64) arch="aarch64";;
        armv6l|armv7l) arch="armv6hf";;
        *)
            echo "❌ Unsupported architecture: $(uname -m)"
            exit 1
            ;;
    esac

    echo "${os}.${arch}"
}

# Download and install shellcheck
install_shellcheck() {
    local platform
    platform=$(detect_platform)

    # Construct download URL and filename
    local base_url="https://github.com/koalaman/shellcheck/releases/download/${SHELLCHECK_VERSION}"
    local filename

    if [ "$platform" = "windows.x86_64" ]; then
        filename="shellcheck-${SHELLCHECK_VERSION}.zip"
    else
        filename="shellcheck-${SHELLCHECK_VERSION}.${platform}.tar.xz"
    fi

    local download_url="${base_url}/${filename}"

    echo "🔍 Detected platform: ${platform}"
    echo "📥 Downloading shellcheck from ${download_url}..."

    # Convert INSTALL_DIR to absolute path before changing directories
    local absolute_install_dir
    mkdir -p "$INSTALL_DIR"
    absolute_install_dir=$(cd "$INSTALL_DIR" && pwd)

    # Create temporary directory
    local temp_dir
    temp_dir=$(mktemp -d)
    trap 'rm -rf "$temp_dir"' EXIT

    # Download
    if ! curl -sL "$download_url" -o "${temp_dir}/${filename}"; then
        echo "❌ Failed to download shellcheck"
        exit 1
    fi

    # Extract
    echo "📦 Extracting shellcheck..."
    cd "$temp_dir"

    if [ "$platform" = "windows.x86_64" ]; then
        unzip -q "${filename}"
    else
        tar -xJf "${filename}"
    fi

    # Find the shellcheck binary
    local shellcheck_bin
    shellcheck_bin=$(find . -name shellcheck -type f | head -n 1)

    if [ -z "$shellcheck_bin" ]; then
        echo "❌ Failed to find shellcheck binary in archive"
        exit 1
    fi

    # Install to target directory (using absolute path)
    cp "$shellcheck_bin" "$absolute_install_dir/shellcheck"
    chmod +x "$absolute_install_dir/shellcheck"

    echo "✅ shellcheck installed to $absolute_install_dir/shellcheck"

    # Verify installation
    if "$absolute_install_dir/shellcheck" --version >/dev/null 2>&1; then
        echo "🎉 shellcheck installation verified"
        "$absolute_install_dir/shellcheck" --version
    else
        echo "❌ shellcheck installation verification failed"
        exit 1
    fi
}

# Main
echo "🐚 Installing shellcheck..."

# Check if shellcheck is already installed in the target directory
if [ -x "$INSTALL_DIR/shellcheck" ]; then
    echo "✓ shellcheck already installed at $INSTALL_DIR/shellcheck"
    echo "   Version: $("$INSTALL_DIR/shellcheck" --version | head -n 1)"
    echo "   Use --force to reinstall"
    exit 0
fi

install_shellcheck

