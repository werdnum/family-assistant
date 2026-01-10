# Contributing to Family Assistant

Thank you for your interest in contributing to Family Assistant! This document provides guidelines
and information to help you get started.

## Welcome

Family Assistant is an LLM-powered application for family information management and task
automation. Whether you're fixing a bug, adding a feature, or improving documentation, your
contributions are welcome.

This project values:

- **Pragmatic solutions** over theoretical perfection
- **Working code** that passes all tests and linters
- **Clear communication** about what you're changing and why

## Getting Started

### Development Setup

The quickest way to set up your development environment:

```bash
# Run the setup script
./scripts/setup-workspace.sh

# Activate the virtual environment
source .venv/bin/activate

# Verify your setup
poe test
```

For detailed setup instructions, dependency management, and development commands, see
[AGENTS.md](AGENTS.md).

### Understanding the Codebase

- **[docs/architecture-diagram.md](docs/architecture-diagram.md)** - Visual overview of system
  architecture
- **[AGENTS.md](AGENTS.md)** - Comprehensive development guide
- **Subdirectory CLAUDE.md files** - Context-specific guidance for different parts of the codebase

## How to Contribute

### Reporting Bugs

1. **Search existing issues** to avoid duplicates
2. **Create a new issue** with:
   - Clear description of the problem
   - Steps to reproduce
   - Expected vs. actual behavior
   - Environment details (Python version, OS, etc.)
   - Relevant log output or error messages

### Suggesting Features

1. **Open a discussion or issue** describing:
   - The problem you're trying to solve
   - Your proposed solution
   - Alternatives you've considered
2. **Wait for feedback** before investing significant time in implementation

### Submitting Code Changes

1. **Fork the repository** and create a branch for your work
2. **Make your changes** following the code standards below
3. **Ensure all tests pass**: Run `poe test` before submitting
4. **Submit a pull request** with a clear description

## Pull Request Process

### Before Submitting

1. **Run the full test suite**: `poe test` must pass
2. **Run linting**: `scripts/format-and-lint.sh` must pass with no errors
3. **Test with PostgreSQL** if touching database code: `poe test-postgres`

### PR Description

Include in your pull request:

- **Summary**: What does this change do and why?
- **Testing**: How did you verify the changes work?
- **Breaking changes**: Does this affect existing functionality?

### Commit Messages

Write clear, concise commit messages that explain the "why" rather than the "what":

- Good: "Fix calendar sync failing when event has no end time"
- Avoid: "Updated calendar.py"

### Code Review

- All PRs require review before merging
- Reviewers will check for:
  - Tests covering new functionality
  - Code following project patterns
  - No linting errors or type checking failures
  - Clear, maintainable code

### CI Requirements

All CI checks must pass:

- Linting (ruff, pylint, basedpyright)
- Unit tests with SQLite
- Unit tests with PostgreSQL
- Frontend tests (if applicable)
- Playwright end-to-end tests

See [tests/CLAUDE.md](tests/CLAUDE.md) for CI debugging guidance.

## Code Standards

### Quick Summary

- **Type hints** on all function parameters and return values
- **Imports** organized by isort rules (standard library, third-party, local)
- **No commented-out code** or self-evident comments
- **Fail fast** - let errors propagate, no silent failures
- **Repository pattern** for database access via `DatabaseContext`
- **Async/await** consistently for I/O operations

### Detailed Guidelines

- **[docs/STYLE_GUIDE.md](docs/STYLE_GUIDE.md)** - Coding style and conventions
- **[AGENTS.md](AGENTS.md)** - Architecture patterns and design principles

### Linting

```bash
# Lint entire codebase
scripts/format-and-lint.sh

# Lint specific files
scripts/format-and-lint.sh path/to/file.py
```

This runs ruff, pylint, basedpyright, and code conformance checks. All checks must pass before
committing.

## Testing Requirements

### Running Tests

```bash
# Run all tests (recommended before submitting PRs)
poe test

# Run tests with PostgreSQL
poe test-postgres

# Run specific test file
pytest tests/functional/test_specific.py -xq
```

### Test Guidelines

- Write tests for new functionality
- Each test should verify one independent behavior
- Prefer real or fake dependencies over mocks
- Use the existing fixtures documented in [tests/CLAUDE.md](tests/CLAUDE.md)
- Never use fixed time-based waits; wait for specific conditions instead

## First Contribution Ideas

New to the project? Here are some good places to start:

- **Documentation improvements** - Fix typos, clarify explanations, add examples
- **Bug fixes** - Look for issues labeled "good first issue" if available
- **Test coverage** - Add tests for untested functionality
- **Code cleanup** - Address linting warnings or simplify complex code

Start small to get familiar with the codebase and contribution process before tackling larger
features.

## Getting Help

If you have questions:

- Review the documentation in `docs/` and subdirectory CLAUDE.md files
- Open a GitHub issue for project-specific questions
- Check existing issues and discussions for answers

## License

By contributing to Family Assistant, you agree that your contributions will be licensed under the
same terms as the project.
