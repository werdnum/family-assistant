# Claude Conversation Repair Scripts

This directory contains utility scripts for maintaining and repairing Claude conversation history
files.

## fix_claude_conversation.py

A simple script to fix Claude conversation history files where tool_use and tool_result blocks are
mismatched or out of order, causing Claude API errors like:

```
API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"messages.26.content.1: unexpected `tool_use_id` found in `tool_result` blocks: toolu_017NfFndPGQnEHbNSKbFCSoK. Each `tool_result` block must have a corresponding `tool_use` block in the previous message."}}
```

### What it does

1. **Backs up** the original file automatically (creates `.backup` files)
2. **Identifies orphaned tool_use blocks** that have no matching tool_result
3. **Removes orphaned tool_use messages** to make conversations usable again
4. **Preserves all other conversation data** with minimal loss

### Usage

**Dry run (check for issues without making changes):**

```bash
python scripts/fix_claude_conversation.py conversation.jsonl
```

**Apply fixes:**

```bash
python scripts/fix_claude_conversation.py --fix conversation.jsonl
```

**Batch process multiple files:**

```bash
python scripts/fix_claude_conversation.py --fix ~/.claude/projects/*/*.jsonl
```

### Example Output

```
Mode: DRY RUN (analysis only)
Files to process: 1

Analyzing: /home/claude/.claude/projects/-workspace/conversation.jsonl
  Total messages: 434
  Tool uses: 108
  Tool results: 107
  Orphaned tool uses: 1
  Orphaned tool IDs: ['toolu_01JtkaWqCGZNVD9P2QdgYRU7']
  ⚠️  Issues found - run with --fix to repair

📊 Summary:
  Files processed: 1
  Files needing repair: 1
  💡 Run with --fix to apply repairs
```

### Safety Features

- **Automatic backups**: Creates `.backup` files before making any changes
- **Dry run by default**: Must use `--fix` flag to actually modify files
- **Minimal data loss**: Only removes problematic tool_use blocks
- **Clear reporting**: Shows exactly what will be changed

### When to use

Use this script when you encounter Claude API errors about mismatched tool_use and tool_result
blocks, which can happen when:

- Conversation history gets corrupted
- Tool executions are interrupted
- Messages get out of order due to system issues

The script prioritizes making conversations usable again over perfect data preservation.
