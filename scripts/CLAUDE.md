# Scripts Development Guide

This file provides guidance for working with project scripts.

## Overview

The `scripts/` directory contains utility scripts for development, testing, and deployment tasks.

## Key Scripts

### `format-and-lint.sh`

Primary code quality script that runs:

- `ruff check --fix` (linting with auto-fixes)
- `ruff format` (code formatting)
- `basedpyright` (type checking)
- `pylint` (additional linting in errors-only mode)

**Usage:**

```bash
# Lint entire codebase (src/ and tests/)
scripts/format-and-lint.sh

# Lint specific Python files only
scripts/format-and-lint.sh path/to/file.py path/to/another.py

# Lint only changed Python files (useful before committing)
scripts/format-and-lint.sh $(git diff --name-only --cached | grep '\.py$')
```

**Note**: This script is for Python files only. It will error if given non-Python files.

**IMPORTANT**: This script MUST pass before committing. NEVER use `git commit --no-verify` -- all
lint failures must be fixed or properly disabled.

## Script Development Best Practices

### General Guidelines

1. **Use bash for shell scripts**: Ensure compatibility across environments
2. **Add error handling**: Use `set -e` to exit on errors
3. **Make scripts executable**: `chmod +x script-name.sh`
4. **Document usage**: Add help text or comments at the top
5. **Test scripts**: Verify they work in clean environments

### Error Handling

```bash
#!/usr/bin/env bash
set -e  # Exit on error
set -u  # Exit on undefined variable
set -o pipefail  # Exit on pipe failure

# Your script here
```

### User Feedback

```bash
# Use clear output for user feedback
echo "Running linters..."
echo "✓ Linting completed successfully"
echo "✗ Error: Linting failed"
```

### Idempotency

Scripts should be safe to run multiple times:

- Check for existing state before making changes
- Use `--force` flags judiciously
- Clean up temporary files

## Adding New Scripts

When adding a new script:

1. **Place in appropriate directory**:

   - Development tools → `scripts/`
   - Deployment scripts → `scripts/deploy/` or `.devcontainer/`
   - Test utilities → `tests/`

2. **Follow naming conventions**:

   - Use lowercase with hyphens: `my-script.sh`
   - Be descriptive: `build-and-push-container.sh` not `build.sh`

3. **Add documentation**:

   - Update this file with script description
   - Add usage examples
   - Document any prerequisites

4. **Make it discoverable**:

   - Consider adding a poe task in `pyproject.toml`
   - Update README if user-facing
