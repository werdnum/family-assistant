# Task Completion Checklist

When completing any coding task in the Family Assistant project, follow these steps:

## 1. Code Quality Checks

- **Run linting**: `scripts/format-and-lint.sh` MUST pass before committing
- Never use `git commit --no-verify` - all lint failures must be fixed
- Ensure all type hints are proper and complete

## 2. Testing

- Run relevant tests: `pytest tests/functional/test_specific.py -xq`
- For database changes, test with PostgreSQL: `pytest --postgres -xq`
- Add tests for new functionality following the project's testing patterns
- Ensure tests follow the Arrange-Act-Assert pattern

## 3. Documentation Updates

- Update `docs/user/USER_GUIDE.md` for user-visible features
- Update system prompts in `prompts.yaml` if needed
- Update tool descriptions if functionality changes
- Do NOT create documentation files unless explicitly requested

## 4. Database Considerations

- Run migrations if schema changes: `alembic upgrade head`
- Create new migration if needed: `alembic revision --autogenerate -m "Description"`
- Test with both SQLite and PostgreSQL

## 5. Git Workflow

- Commit changes after each major step
- Prefer many small self-contained commits
- Each commit must pass lint checks
- Use conventional commit messages (feat:, fix:, docs:, etc.)

## 6. Special Considerations

- For new tools: Register in both code (`__init__.py`) and `config.yaml`
- For new UI endpoints: Add to `BASE_UI_ENDPOINTS` in tests
- For imports: Add code first, then add the import
- Check if local instance needs restart (usually auto-restarts)

## Important Reminders

- Always start with a clean working directory
- Never revert existing changes without user permission
- Consider both tactical fixes and long-term solutions
- Look for design smells and suggest refactoring when appropriate
