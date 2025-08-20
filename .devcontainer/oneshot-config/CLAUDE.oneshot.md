# ONE SHOT MODE INSTRUCTIONS

You are running in ONE SHOT MODE - NON-INTERACTIVE AUTONOMOUS EXECUTION. This means:

1. **Complete ALL work before stopping** - The stop hook will prevent you from exiting with
   incomplete work
2. **Work completely autonomously** - Do not ask for user input or confirmation
3. **Commit and push all changes** - You must commit your work and push to the remote repository
4. **Ensure tests pass** - Run `poe test` and fix any failures before finishing
5. **Create PRs if needed** - You have permission to push and create PRs without asking

## Required before stopping:

- ✅ All changes committed
- ✅ All commits pushed to remote
- ✅ Tests passing (`poe test` succeeds)
- ✅ Task fully completed

## If you cannot complete the task:

If you encounter blockers that make the task impossible to complete (missing permissions,
dependencies, external service failures, etc.), you can acknowledge this by writing:

```bash
echo "Cannot complete task: [reason]" > .claude/FAILURE_REASON
```

Examples:

- `echo "Missing API key for external service" > .claude/FAILURE_REASON`
- `echo "Required dependency not available in environment" > .claude/FAILURE_REASON`
- `echo "Tests require manual intervention that cannot be automated" > .claude/FAILURE_REASON`

This will allow the oneshot mode to exit gracefully while documenting why the task failed.

## Auto-approved tools in oneshot mode:

- `git push` - Push commits to remote
- `gh pr create` - Create pull requests
- `git commit -m` - Commit changes
- All standard approved tools from normal mode

Remember: The stop hook will BLOCK your exit unless requirements are met OR you acknowledge failure
with FAILURE_REASON.
