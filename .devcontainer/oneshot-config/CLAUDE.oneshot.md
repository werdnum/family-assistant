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

## Auto-approved tools in oneshot mode:

- `git push` - Push commits to remote
- `gh pr create` - Create pull requests
- `git commit -m` - Commit changes
- All standard approved tools from normal mode

Remember: The stop hook will BLOCK your exit if these conditions aren't met.
