# Code Style and Conventions

## General Principles

- Comments are used to explain implementation when it's unclear
- Do NOT add comments that are self-evident from the code or explain history
- No comments like `# Removed db_context` - use git history instead

## Python Style

- Python 3.10+ with type hints throughout
- Async/await for all asynchronous operations
- Repository pattern for data access via DatabaseContext
- Protocol-based interfaces for loose coupling
- Proper error handling with logging

## Import Style

- Use `from sqlalchemy.sql import functions as func` to avoid pylint errors
- Add code that uses imports before adding the import itself

## Database Patterns

- Always use the repository pattern via DatabaseContext:

  ```python
  from family_assistant.storage.context import DatabaseContext

  async with DatabaseContext() as db:
      await db.notes.add_or_update(title, content)
      tasks = await db.tasks.get_pending_tasks()
  ```

- Use symbolic SQLAlchemy queries, avoid literal SQL

- Always use `.label("count")` with func.count() queries

## Testing Conventions

- Write tests as "end-to-end" as possible
- Minimal mocking - use real databases with fixtures
- Each test tests one independent behavior (Arrange, Act, Assert)
- Always run tests with `-xq` flag for concise output
- Use `--postgres` flag to test with PostgreSQL

## Tool Development

- All tools must return strings
- Tool implementations should be async functions
- Tools receive ToolExecutionContext with database access
- Tools must be registered in code AND config.yaml

## File Management

- Never create files unless absolutely necessary
- Always prefer editing existing files
- Never proactively create documentation files
- Put temporary files in `scratch/` directory
