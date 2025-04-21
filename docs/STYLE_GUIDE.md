# Style Guide and Conventions

This document outlines the coding style and conventions used in this project.

## General Principles

*   Follow PEP 8 for Python code.
*   Aim for clarity and readability.
*   Use type hints consistently.
*   Keep functions and methods focused and concise.

## Comments

*   **Purpose:** Use comments primarily to explain *why* something is being done, especially if the reason is not immediately obvious from the code itself. Avoid comments that merely explain *what* the code does, as the code should ideally be self-explanatory.
*   **Target Audience:** Write comments for future developers (including your future self) who might need to understand the reasoning behind a particular implementation choice or a non-obvious piece of logic.
*   **Avoid Redundancy:** Do not use comments to reiterate steps that are already clear from the code or that belong in commit messages (which explain the *change* being made). Commit messages and version history track the evolution of the code; comments explain the state of the code *as it is*.
*   **Docstrings:** Use docstrings (following PEP 257) for modules, classes, functions, and methods to explain their purpose, arguments, and return values. These are for documenting the public API and usage, distinct from implementation comments.

## Naming Conventions

*   Use `snake_case` for variables, functions, and methods.
*   Use `PascalCase` for classes.
*   Use `UPPER_SNAKE_CASE` for constants.

## Imports

*   Group imports in the following order:
    1.  Standard library imports
    2.  Third-party library imports
    3.  Local application/library specific imports
*   Separate groups with a blank line.
*   Use absolute imports where possible.

## Logging

*   Use the standard `logging` module.
*   Obtain loggers using `logging.getLogger(__name__)`.
*   Use appropriate log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL).

## Error Handling

*   Be specific about exceptions caught. Avoid bare `except:` clauses.
*   Log errors appropriately.

## Asynchronous Code

*   Use `async` and `await` consistently for I/O-bound operations.
*   Be mindful of blocking operations in asynchronous code.
