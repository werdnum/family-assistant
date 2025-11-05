# Claude Code Hooks Configuration

This directory contains hook scripts and configuration files for Claude Code.

## Bash Command Validation Hook

The `check_banned_commands.py` hook validates and auto-fixes Bash commands before execution.

### Configuration File: `banned_commands.json`

The hook uses a JSON configuration file with the following sections:

#### 1. `default_timeout_ms`

Default timeout in milliseconds when no timeout is specified (default: 120000 = 2 minutes).

```json
{
  "default_timeout_ms": 120000
}
```

#### 2. `command_rules`

Rules for blocking or automatically rewriting commands.

**Block Action**: Prevents dangerous or incorrect commands from running.

```json
{
  "regexp": "\\bgit\\s+commit\\b.*--no-verify",
  "action": "block",
  "explanation": "Using --no-verify is not permitted in this project."
}
```

**Replace Action**: Automatically rewrites commands to the correct form.

```json
{
  "regexp": "\\bpython\\s+-m\\s+pytest\\b",
  "action": "replace",
  "replacement": "pytest",
  "explanation": "Automatically rewriting to use 'pytest' directly."
}
```

Replacements support regex capture groups using `\1`, `\2`, etc.:

```json
{
  "regexp": "\\bcat\\s+([^|\\s]+)\\s*\\|\\s*llm\\b",
  "action": "replace",
  "replacement": "llm -f \\1",
  "explanation": "Automatically rewriting to use 'llm -f' for better file handling."
}
```

#### 3. `timeout_requirements`

Automatically sets or increases timeout for commands that need longer execution time.

```json
{
  "regexp": "^pytest\\b",
  "minimum_timeout_ms": 300000,
  "explanation": "pytest requires at least 5 minutes for comprehensive test execution"
}
```

If a command matches and has no timeout or insufficient timeout, the hook will automatically set the
proper timeout using the `updatedInput` feature.

#### 4. `background_restrictions`

Commands that must run in foreground (automatically fixes `run_in_background: true` → `false`).

```json
{
  "regexp": "^poe\\s+test\\b",
  "explanation": "poe test must run in foreground to ensure proper output capture"
}
```

### Hook Behavior

The hook uses Claude Code's `updatedInput` feature to automatically fix command issues:

**Auto-Fix Examples:**

1. **Command Replacement:**

   - Input: `python -m pytest tests/`
   - Output: `pytest tests/` (auto-rewritten)

2. **Timeout Auto-Set:**

   - Input: `pytest tests/` (no timeout)
   - Output: `pytest tests/` with `timeout: 300000` (auto-added)

3. **Timeout Auto-Increase:**

   - Input: `poe test` with `timeout: 120000`
   - Output: `poe test` with `timeout: 900000` (auto-increased)

4. **Background Auto-Fix:**

   - Input: `poe test` with `run_in_background: true`
   - Output: `poe test` with `run_in_background: false` (auto-fixed)

5. **Multiple Fixes:**

   - Input: `python -m pytest` with `run_in_background: true`
   - Output: `pytest` with `timeout: 300000` and `run_in_background: false`

**Block Examples:**

1. **Dangerous Commands:**

   - Input: `rm -rf /`
   - Output: Blocked with error message

2. **Policy Violations:**

   - Input: `git commit --no-verify`
   - Output: Blocked with explanation

### Testing

A test script is available at `scratch/test_hook.py`:

```bash
python scratch/test_hook.py
```

This runs comprehensive tests covering:

- Command replacements
- Timeout auto-fixing
- Background restrictions
- Blocking dangerous commands
- Multiple simultaneous fixes

### Adding New Rules

To add new rules, edit `banned_commands.json`:

1. **Block a dangerous pattern:**

   ```json
   {
     "regexp": "pattern_to_block",
     "action": "block",
     "explanation": "Why this is blocked"
   }
   ```

2. **Auto-fix a command:**

   ```json
   {
     "regexp": "pattern_to_match",
     "action": "replace",
     "replacement": "corrected_command",
     "explanation": "Why this is auto-fixed"
   }
   ```

3. **Require a minimum timeout:**

   ```json
   {
     "regexp": "^command_pattern\\b",
     "minimum_timeout_ms": 300000,
     "explanation": "Why this timeout is needed"
   }
   ```

4. **Prevent background execution:**

   ```json
   {
     "regexp": "^command_pattern\\b",
     "explanation": "Why this must run in foreground"
   }
   ```

### Exit Codes

- **0**: Command allowed (possibly with modifications via `updatedInput`)
- **2**: Command blocked (explanation sent to stderr)

### JSON Output Format

When modifications are applied, the hook outputs JSON to stdout:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "Auto-setting timeout to 5 minutes...",
    "updatedInput": {
      "timeout": 300000
    }
  }
}
```

Claude Code will automatically apply the `updatedInput` changes before executing the tool.
