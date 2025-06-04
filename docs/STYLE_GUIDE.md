# Style Guide and Conventions

This document outlines the coding style, commenting philosophy, and conventions used in this project.

## General Principles

*   Follow PEP 8 for Python code.
*   Aim for clarity and readability.
*   Use type hints consistently.
*   Keep functions and methods focused and concise.
*   **Prevent Runtime Errors:** Ensure all variables and names are defined before use and that imports are correct to prevent `NameError` or `ImportError` (related to frequent errors like F821, E0602, E0401). Careful attention to scope and spelling is crucial.
*   **Avoid Unused Code:** Remove unused variables, imports, and commented-out code to keep the codebase clean and reduce confusion (enforced by Pyflakes: F401, F841; eradicate: ERA001).
*   **Mutable Default Arguments:** Do not use mutable data structures (e.g., lists, dictionaries) as default arguments to functions. Initialize them to `None` and create the mutable object inside the function body (related to flake8-bugbear: B006).
*   **Function Calls in Defaults:** Avoid function calls in default argument values (e.g., `def func(arg=datetime.now()):`). The default is evaluated only once when the function is defined (related to flake8-bugbear: B008).
*   **Closures and Loop Variables:** Be mindful of late binding when defining functions (e.g., lambdas) inside loops that use the loop variable. If the current value of the loop variable is needed, capture it, for instance, by passing it as a default argument to the inner function (related to flake8-bugbear: B023).


## Appropriate use of comments

*   **Purpose:** Use comments primarily to explain *why* something is being done, especially if the reason is not immediately obvious from the code itself. Avoid comments that merely explain *what* the code does, as the code should ideally be self-explanatory.
*   **Target Audience:** Write comments for future developers (including your future self) who might need to understand the reasoning behind a particular implementation choice or a non-obvious piece of logic.
*   **Avoid Redundancy:** Do not use comments to reiterate steps that are already clear from the code. Commit messages explain the *change* being made, tracking the evolution; comments explain the state of the code *as it is*.
*   **Docstrings:** Use docstrings (following PEP 257) for modules, classes, functions, and methods to explain their purpose, arguments, and return values. These are for documenting the public API and usage, distinct from implementation comments.

Write code as it should appear in the repository. Do not comment out code or leave comments explaining your changes. That's what commit messages are for.

Examples

BAD:

```python
import yaml # Added YAML import for YAMLing
```

GOOD:

```python
import yaml
```

BAD:

```python
# func do_the_thing():
# ... moved to thing_doer.py
```

GOOD:

Nothing - just remove the function.

## Design and Testability

*   **Dependency Injection:** Design components to receive their dependencies (like database connections, configuration objects, external service clients) as arguments or through a framework's DI mechanism (e.g., FastAPI's `Depends`). Avoid reliance on global variables or direct imports of dependencies within core logic modules. This is crucial for replacing dependencies with mocks or test instances during testing.
*   **Modularity:** Create components with clear responsibilities and well-defined interfaces. This allows components to be tested more easily in isolation or replaced with fakes when testing other parts of the system.
*   **Test Strategy Focus:** Prioritize realistic integration and functional tests that verify the behavior of components working together. Use `testcontainers` for dependencies like databases where possible. While unit tests have their place for complex, isolated logic, the primary goal is to ensure the system works correctly end-to-end.
*   **Testable Core Logic:** Separate core application logic (e.g., processing a user request, handling a task) from interface-specific code (e.g., Telegram API interactions, FastAPI request/response handling). Test the core logic directly, making the interface layers thin wrappers.

## Naming Conventions

*   Use `snake_case` for variables, functions, and methods.
*   Use `PascalCase` for classes.
*   Use `UPPER_SNAKE_CASE` for module-level constants.

## Type Safety

Strongly prefer type safety at interface boundaries. In particular:

*   Create types for structures accepted by third party APIs (e.g. OpenAI API `{"role": "user", "content": ...}`).
*   Strongly prefer typed dictionaries (e.g., `TypedDict`) or `dataclasses`/`NamedTuple` over `Dict[str, Any]` for structured data.
*   **Comprehensive Annotations:** Functions and methods should have type hints for all arguments (including `self`, `cls`, `*args`, and `**kwargs`) and return values. This is enforced by `flake8-annotations` (ANN rules like ANN001, ANN002, ANN003, ANN201, ANN202, ANN204).
*   **`typing.Any` is Permitted:** While comprehensive typing is encouraged, the use of `typing.Any` is currently permitted (as rule category `ANN4` which includes `ANN401` disallowing `Any` for variables and arguments is ignored). Use it judiciously when a more specific type is not practical.
*   **Type-Only Imports:** For imports used exclusively for type annotations, place them inside a `if TYPE_CHECKING:` block to avoid runtime circular dependencies and improve startup time. This is managed by `flake8-type-checking` (TC rules).
    ```python
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from path.to.module import SomeTypeUsedForAnnotationOnly

    def my_function(param: "SomeTypeUsedForAnnotationOnly") -> None:
        # ...
    ```
*   **Forward References:** When forward-referencing types (e.g., to avoid circular imports or use a type not yet defined), use string literals (e.g., `param: "MyClass"`). This applies to function/method signatures, variable annotations, and also within runtime type constructs like `typing.cast("MyClass", value)` or string-based unions if the type is only known during static analysis (related to TC rules like TC004, TC006, TC010).

## Imports

*   Group imports in the following order:

    1.  Standard library imports
    2.  Third-party library imports
    3.  Local application/library specific imports
*   Separate groups with a blank line.
*   Use absolute imports where possible.
*   Module-level imports should generally be at the top of the file, after any module docstring and `__future__` imports, but before any other code (related to pycodestyle: E402).
*   Import statements are automatically sorted and formatted by our linting tools (enforced by isort: I rules).

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

## Linter-Driven Coding Standards

Beyond general PEP 8 and the principles outlined above, our automated linting (primarily via Ruff) enforces several specific coding standards. Adhering to these will ensure smoother contributions and fewer CI failures.
*   Our linting setup (Ruff) enforces many PEP 8 guidelines, including those related to whitespace, indentation, inline comments, and multiple statements on one line (e.g., E111, E117, E261, E701, E402). While the auto-formatter (Black) handles much of this, adherence to these rules ensures consistency and helps catch potential issues.
*   **No Commented-Out Code:** As stated in the "Appropriate use of comments" section, commented-out code is flagged by `eradicate` (ERA rules) and should be removed. Use version control to track historical code.
*   **`zip()` with `strict=True`:** When using `zip()` on iterables that are expected to be of the same length, use the `strict=True` argument (available in Python 3.10+). This helps catch bugs early by raising a `ValueError` if a_i_d_e iterables have different lengths. This is checked by `flake8-bugbear` (`B905`).
    ```python
    # Good
    for item1, item2 in zip(list1, list2, strict=True):
        # ...
    ```
*   **FastAPI Dependencies with `Annotated`:** For FastAPI applications, dependencies should be declared using `typing.Annotated`. This is a modern FastAPI practice enforced by `FAST002`.
    ```python
    from typing import Annotated
    from fastapi import Depends, FastAPI

    app = FastAPI()

    async def get_common_parameters(q: str | None = None):
        return {"q": q}

    @app.get("/")
    async def read_root(commons: Annotated[dict, Depends(get_common_parameters)]):
        return commons
    ```
*   **Line Length:** While PEP 8 suggests a line length limit, this project relies on an automatic code formatter (like Black, which is part of the pre-commit hooks) to handle line lengths. The linter rule for line length (`E501`) is therefore ignored. Focus on logical code structure, and let the formatter handle the visual layout.
*   **Modern Python Syntax:** `pyupgrade` (UP rules) is used to automatically upgrade code to use more modern Python syntax where appropriate (e.g., `X | Y` for union types instead of `typing.Union[X, Y]` in Python 3.10+).

## Code Simplification

Strive for simpler, more readable code where functionality is equivalent. Our linter helps enforce this (flake8-simplify: SIM rules):

*   Use ternary operators (`val = x if condition else y`) for concise conditional assignments instead of verbose if/else blocks (SIM108).
*   Combine multiple `with` statements for context managers where possible: `with ctx1() as c1, ctx2() as c2:` instead of nested `with` statements. (SIM117)
*   Check for dictionary key membership directly (e.g., `if key in my_dict:`) rather than `if key in my_dict.keys():`. (SIM118)
*   Simplify `if` statements that can be collapsed into a single condition (SIM102).
*   Use `contextlib.suppress` for concisely ignoring specific exceptions where appropriate, instead of a `try...except...pass` block (SIM105).
