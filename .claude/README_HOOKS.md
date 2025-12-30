# Claude Code Plugins Configuration

This directory contains plugin configuration files for Claude Code. The project uses plugins from
the [werdnum-plugins](https://github.com/werdnum/claude-code-plugins) marketplace.

## Installed Plugins

### 1. **bash-guard** - Command Safety

Prevents dangerous shell commands and enforces best practices through PreToolUse hooks.

**Configuration**: `bash-guard.json`

**Features**:

- Block dangerous commands (rm -rf /, fork bombs, etc.)
- Enforce minimum timeouts for long-running commands
- Block commits to protected branches (main/master)
- Prevent commands from running in background when required
- Auto-rewrite commands to safer alternatives

**Project-Specific Rules** (see `bash-guard.json`):

- Container URL rewrites (localhost → devcontainer-backend-1)
- Dev server blocking (prevents duplicate instances)
- Command optimizations (npm --prefix, llm -f)
- Test timeout requirements (pytest: 5min, poe test: 15min)
- Background restrictions (poe test must run in foreground)

### 2. **format-and-lint** - Code Quality

Automatically formats and lints files after edits through PostToolUse hooks.

**Configuration**: `format-lint.json`

**Features**:

- **File Formatting**: Ensure newline at EOF, trim trailing whitespace
- **Python**: ruff format, ruff check, basedpyright, ast-grep
- **TypeScript**: prettier, eslint (for frontend/ directory)
- Informational only (doesn't block execution)

**Project Configuration**:

- Python linting enabled with venv at `.venv`
- TypeScript linting enabled for `frontend/` directory
- Extended file patterns for formatters

### 3. **guardian** - Quality Gates

Ensures code quality through test verification, pre-commit workflows, and stop validation.

**Configuration**: `guardian.json`

**Features**:

- **Test Verification**: Ensures tests run before commits
- **Pre-Commit Review**: Executes git adds, runs pre-commit hooks, optional code review
- **Stop Validation**: Validates work completeness before stopping session
- **Oneshot Mode**: Strict requirements for CI/CD environments

**Project Configuration**:

- **Code review**: Enabled using `scripts/review-changes.py`
- **Test verification**: Checks that tests have been run before commits
  - Triggers on `git commit`
  - Excludes documentation and config files
  - Accepts `poe test` and `.venv/bin/poe test` as valid test commands
  - Checks `.report.json` for test results (max age: 5 minutes)
- **Stop validation**: Validates completeness before stopping session
  - Checks for clean working directory
  - Verifies tests have been run
  - Allows failure acknowledgment via `.claude/FAILURE_REASON`

### 4. **development-agents** - Specialized Agents

Collection of specialized AI agents for focused development tasks.

**No Configuration Required**

**Included Agents**:

- **systematic-debugger**: Methodical bug investigation
- **focused-coder**: Self-contained implementation tasks
- **mechanical-coder**: Repetitive changes with ast-grep
- **codebase-researcher**: Code exploration and understanding
- **external-research-specialist**: Web research and documentation
- **playwright-qa-tester**: UI testing with Playwright
- **parallel-coder**: Coordinating parallel development

**Included Commands**:

- **/test**: Run tests and display output

## Configuration Files

Plugin configurations use a layered system:

```
Project Override (.claude/plugin-name.json)
           ↓
Global Override (~/.config/claude-code/plugin-name.json)
           ↓
Plugin Defaults (from plugin)
```

### bash-guard.json

Project-specific command rules, timeout requirements, and background restrictions.

Example:

```json
{
  "command_rules": [
    {
      "regexp": "\\blocalhost:5173\\b",
      "action": "replace",
      "replacement": "devcontainer-backend-1:5173",
      "explanation": "App runs in container"
    }
  ]
}
```

### format-lint.json

Formatting and linting configuration for different languages.

Example:

```json
{
  "linting": {
    "python": {
      "enabled": true
    },
    "typescript": {
      "enabled": true,
      "projectDir": "frontend"
    }
  }
}
```

### guardian.json

Quality gate configuration for testing and review workflows.

Example:

```json
{
  "preCommitReview": {
    "workflow": {
      "runCodeReview": {
        "enabled": true,
        "scriptPath": "scripts/review-changes.py"
      }
    }
  }
}
```

## Marketplace Configuration

The plugins are automatically installed from the configured marketplace in `settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "werdnum-plugins": {
      "source": {
        "source": "github",
        "repo": "werdnum/claude-code-plugins"
      }
    }
  },
  "enabledPlugins": {
    "bash-guard@werdnum-plugins": true,
    "format-and-lint@werdnum-plugins": true,
    "guardian@werdnum-plugins": true,
    "development-agents@werdnum-plugins": true
  }
}
```

## Updating Plugin Configurations

To customize plugin behavior:

1. **Edit project config**: Modify `.claude/bash-guard.json`, `.claude/format-lint.json`, or
   `.claude/guardian.json`
2. **Create global config**: Add `~/.config/claude-code/plugin-name.json` for user-wide settings
3. **Restart session**: Changes take effect on next Claude Code session

## Plugin Documentation

For detailed documentation on each plugin:

- [bash-guard README](https://github.com/werdnum/claude-code-plugins/blob/main/plugins/bash-guard/README.md)
- [format-and-lint README](https://github.com/werdnum/claude-code-plugins/blob/main/plugins/format-and-lint/README.md)
- [guardian README](https://github.com/werdnum/claude-code-plugins/blob/main/plugins/guardian/README.md)
- [development-agents README](https://github.com/werdnum/claude-code-plugins/blob/main/plugins/development-agents/README.md)

## Migration Notes

This project was migrated from custom hook scripts to plugins:

- ✅ `check_banned_commands.py` → **bash-guard** plugin
- ✅ `check_main_branch_commit.py` → **bash-guard** plugin
- ✅ `format-files.py` → **format-and-lint** plugin
- ✅ `lint-hook.py` → **format-and-lint** plugin
- ✅ `test-verification-hook.sh` → **guardian** plugin
- ✅ `test-verification-core.sh` → **guardian** plugin
- ✅ `review-hook.py` → **guardian** plugin
- ✅ `stop-feedback-hook.sh` → **guardian** plugin
- ✅ `.claude/agents/*` → **development-agents** plugin
- ✅ `.claude/commands/test.md` → **development-agents** plugin

**Kept Scripts**:

- `session-start-hook.sh`: Project-specific workspace setup (not replaced by plugins)
- `scripts/review-changes.py`: Integrated with guardian plugin's code review workflow
- `scripts/format-and-lint.sh`: Standalone script for manual use and pre-commit hooks
