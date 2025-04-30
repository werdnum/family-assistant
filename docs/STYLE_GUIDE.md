# Style Guide and Conventions

This document outlines the coding style, commenting philosophy, and conventions used in this project.

## General Principles

*   Follow PEP 8 for Python code.
*   Aim for clarity and readability.
*   Use type hints consistently.
*   Keep functions and methods focused and concise.
*   Write code as it should appear in the repository. Do not comment out code or leave comments explaining your changes. That's what commit messages are for.

## Design and Testability

*   **Dependency Injection:** Design components to receive their dependencies (like database connections, configuration objects, external service clients) as arguments or through a framework's DI mechanism (e.g., FastAPI's `Depends`). Avoid reliance on global variables or direct imports of dependencies within core logic modules. This is crucial for replacing dependencies with mocks or test instances during testing.
*   **Modularity:** Create components with clear responsibilities and well-defined interfaces. This allows components to be tested more easily in isolation or replaced with fakes when testing other parts of the system.
*   **Test Strategy Focus:** Prioritize realistic integration and functional tests that verify the behavior of components working together. Use `testcontainers` for dependencies like databases where possible. While unit tests have their place for complex, isolated logic, the primary goal is to ensure the system works correctly end-to-end.
*   **Testable Core Logic:** Separate core application logic (e.g., processing a user request, handling a task) from interface-specific code (e.g., Telegram API interactions, FastAPI request/response handling). Test the core logic directly, making the interface layers thin wrappers.

## Comments and Docstrings

*   **Purpose:** Use comments primarily to explain *why* something is being done, especially if the reason is not immediately obvious from the code itself. Avoid comments that merely explain *what* the code does, as the code should ideally be self-explanatory.
*   **Target Audience:** Write comments for future developers (including your future self) who might need to understand the reasoning behind a particular implementation choice or a non-obvious piece of logic.
*   **Avoid Redundancy:** Do not use comments to reiterate steps that are already clear from the code. Commit messages explain the *change* being made, tracking the evolution; comments explain the state of the code *as it is*.
*   **Docstrings:** Use docstrings (following PEP 257) for modules, classes, functions, and methods to explain their purpose, arguments, and return values. These are for documenting the public API and usage, distinct from implementation comments.

## Naming Conventions

*   Use `snake_case` for variables, functions, and methods.
*   Use `PascalCase` for classes.

*   Use `UPPER_SNAKE_CASE` for module-level constants.

## Imports

*   Group imports in the following order:

    1.  Standard library imports
    2.  Third-party library imports
    3.  Local application/library specific imports
*   Separate groups with a blank line.
*   Use absolute imports where possible.

## Logging

*   Use the standard `logging` module.
*   Obtain loggers using `logging.getLogger(__name__)` at the module level.
*   Use appropriate log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL).
*   Keep log messages concise but informative. Avoid overly verbose logging in production.

## Error Handling

*   Be specific about exceptions caught. Avoid bare `except:` clauses.
*   Log errors appropriately.
*   Consider using `try...except...finally` for cleanup actions.

## Asynchronous Code

*   Use `async` and `await` consistently for I/O-bound operations.
*   Be mindful of blocking operations in asynchronous code.
*   Use `asyncio` primitives (locks, events, queues) correctly.
*   Use `asyncio.TaskGroup` (Python 3.11+) or `asyncio.gather` for managing concurrent operations where appropriate. Prefer TaskGroup for better error handling and cancellation.
