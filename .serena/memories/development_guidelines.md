# Development Guidelines

## Planning and Approach

- ALWAYS make a plan before nontrivial changes
- ALWAYS ask user to approve plans before starting work
- Stop and ask for approval before major rearchitecture or technical decisions
- Write significant change plans to `docs/design/` for approval
- Break plans into meaningful milestones with incremental value
- Do NOT give timelines in weeks/days - this is a hobby project

## Problem Solving

- Consider both tactical pragmatic fixes and "proper" long-term solutions
- Ask user preference between quick fix vs proper solution
- Look for design smells and code smells
- Refactoring is relatively cheap - cheaper than leaving things broken

## Architecture Patterns

- **Repository Pattern**: All data access through DatabaseContext
- **Dependency Injection**: Services accept dependencies as constructor arguments
- **Protocol-based Interfaces**: Use Python protocols for loose coupling
- **Async/Await**: Fully asynchronous architecture
- **Context Managers**: For proper resource cleanup
- **Event-Driven**: Loosely coupled components via events

## Security Considerations

- Different profiles have different tool access for security
- Never introduce code that exposes or logs secrets
- Never commit secrets or keys to repository
- Tool confirmation requirements for sensitive operations

## Development Environment

- Project deployed to Kubernetes as `deploy/family-assistant` in namespace `ml-bot`
- Secrets in `secret/family-assistant`
- Local instance auto-restarts on file changes
- Local uses SQLite, production uses PostgreSQL
- Virtual environment is in `.venv`

## When Adding Features

- Update user guide: `docs/user/USER_GUIDE.md`
- Update assistant prompts: `prompts.yaml`
- Consider multi-profile implications
- Test with both database backends
- Ensure proper error handling and logging
