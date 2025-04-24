# Style Guide and Conventions

This document outlines the coding style, commenting philosophy, and conventions used in this project.

## General Principles

*   Follow PEP 8 for Python code.
*   Aim for clarity and readability.
*   Use type hints consistently.
*   Keep functions and methods focused and concise.

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
*   Be mindful of sensitive information in logs.
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
