# CI Workflow Guide

This file provides guidance for working with GitHub Actions workflows.

## Workflow Files

The `.github/workflows/` directory contains CI/CD workflow definitions:

- **`test.yml`**: Main test workflow that runs linting, type checking, and tests
- Other workflow files for deployment, releases, etc.

## When Working on CI Workflows

### Testing Changes

When modifying workflows:

1. **Test locally first** when possible:

   - Use `act` to run GitHub Actions locally
   - Run the actual commands locally to verify they work

2. **Make small, incremental changes**:

   - Test one change at a time
   - Use workflow_dispatch triggers for testing

3. **Check workflow syntax**:

   - GitHub validates YAML syntax on commit
   - Use a YAML linter for complex changes

### Common CI Patterns

**Conditional execution:**

```yaml
- name: Run tests
  if: success() && !cancelled()
  run: pytest tests/
```

**Matrix builds:**

```yaml
strategy:
  matrix:
    python-version: [3.11, 3.12]
    database: [sqlite, postgres]
```

**Artifact handling:**

```yaml
- uses: actions/upload-artifact@v4
  with:
    name: test-results
    path: test-results/
```

## Debugging CI Failures

When CI tests fail, see [tests/CLAUDE.md](../../tests/CLAUDE.md) for comprehensive CI debugging
guidance including:

- Monitoring CI runs with `gh` CLI
- Downloading and analyzing artifacts
- Interpreting test reports
- Common CI issues and solutions
