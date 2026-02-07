# Evaluation: Pydantic Monty as Starlark Replacement

## Executive Summary

[Monty](https://github.com/pydantic/monty) is a minimal, secure Python interpreter written in Rust
by the Pydantic team. It's designed specifically for AI agents to execute Python code safely, using
an external-function model where the host application controls all I/O. This document assesses
whether Monty could replace `starlark-pyo3` as the scripting engine in Family Assistant.

**Bottom line**: Monty is architecturally superior for our use case and would eliminate the most
painful aspects of the current Starlark integration (the sync/async bridge, Python syntax
differences). However, it's at version 0.0.3 / alpha status (as of Feb 2026), so adoption carries
maturity risk. The migration path is clean and incremental.

## Current Pain Points with Starlark

These are the problems a replacement would need to solve:

1. **Sync/async bridge complexity**: Starlark is synchronous. Our tools are async. The `ToolsAPI`
   class (`apis/tools.py`, 737 lines) maintains a background event loop in a separate thread,
   schedules coroutines cross-thread via `run_coroutine_threadsafe`, and has intricate logic to
   avoid deadlocks and "Future attached to a different loop" errors. This is the single most complex
   and fragile part of the scripting system.

2. **Python-but-not-Python syntax**: Starlark looks like Python but isn't. No `try/except`, no
   classes, no `import`, different `for` loop semantics, no `in` operator for strings, no `is`
   operator, no `*args/**kwargs` in script-defined functions. The LLM frequently generates invalid
   Starlark because it thinks it's writing Python.

3. **Flat function namespace**: Because Starlark doesn't support custom objects well, all APIs are
   exposed as flat functions (`time_now`, `time_add`, `tools_execute`, `attachment_get`). This is
   workable but verbose.

4. **No exception handling**: Scripts cannot catch errors. Any tool failure terminates the entire
   script. This limits the complexity of automations that can be expressed.

## What Monty Offers

### Core Architecture

Monty is a Rust tree-walking interpreter with PyO3 bindings (`pydantic-monty` on PyPI). It executes
a practical subset of Python with these key properties:

- **No CPython dependency**: Self-contained Rust binary
- **~0.06ms startup**: vs ~1.7ms for starlark-pyo3 (not meaningful for us, but nice)
- **Releases the GIL** during execution
- **Serializable state**: Parsed code and mid-execution snapshots can be serialized

### The Pause/Resume Model (Key Feature)

This is the most important architectural difference. Instead of Starlark's synchronous execution
where we have to bridge sync→async ourselves, Monty uses cooperative multitasking:

```python
import pydantic_monty

code = """
emails = search_emails(query="grocery list")
note = add_or_update_note(title="Groceries", content=emails[0]["body"])
note
"""

m = pydantic_monty.Monty(
    code,
    external_functions=['search_emails', 'add_or_update_note']
)

# Start execution - pauses at first external call
progress = m.start()
# progress.function_name == 'search_emails'
# progress.kwargs == {'query': 'grocery list'}

# We fulfill the call asynchronously, then resume
result = await search_emails_async(query="grocery list")
progress = progress.resume(return_value=result)

# progress.function_name == 'add_or_update_note'
# ... continue until MontyComplete
```

This **eliminates the entire sync/async bridge**. No background event loops, no
`run_coroutine_threadsafe`, no thread-safety concerns. The host drives execution from async code
naturally.

### Python Subset Support

| Feature                  | Starlark                | Monty                                               |
| ------------------------ | ----------------------- | --------------------------------------------------- |
| Variables, assignments   | Yes                     | Yes                                                 |
| Functions (`def`)        | Yes (with dialect flag) | Yes                                                 |
| Lambdas                  | Yes (with dialect flag) | Yes                                                 |
| f-strings                | Yes (with dialect flag) | Yes                                                 |
| `for`/`while` loops      | Yes                     | Yes                                                 |
| List comprehensions      | Yes                     | Yes                                                 |
| `try`/`except`/`finally` | **No**                  | **Yes**                                             |
| `async`/`await`          | No                      | **Yes**                                             |
| `asyncio.gather()`       | No                      | **Yes**                                             |
| Classes                  | No                      | **No** (planned)                                    |
| `match` statements       | No                      | **No** (planned)                                    |
| Type hints               | No                      | **Yes**                                             |
| `import` (stdlib subset) | No                      | **Yes** (`sys`, `typing`, `asyncio`, `dataclasses`) |
| NamedTuples              | No                      | **Yes**                                             |
| Dataclasses              | No                      | **Yes** (via registration)                          |

The `try`/`except` support alone is a significant upgrade — scripts could handle tool failures
gracefully instead of terminating.

### Security Model

Monty's sandboxing is comparable to Starlark's and arguably more principled:

- **No filesystem access** (unless explicitly provided via `OSAccess`)
- **No network access**
- **No environment variables**
- **No access to host Python runtime**
- **No arbitrary imports**
- **Type coercion**: Custom Python subclasses are flattened to base types, preventing type confusion

**Resource limits** (configurable per execution):

- `max_allocations` — heap allocation limit
- `max_duration_secs` — execution timeout
- `max_memory` — heap memory cap
- `max_recursion_depth` — stack depth (default 1000)
- `gc_interval` — garbage collection frequency

This maps directly to our `StarlarkConfig` but with finer-grained control (memory limits, allocation
limits are new).

## Migration Mapping

### Engine Layer (`engine.py`)

**Current**: `StarlarkEngine` creates a `starlark.Module`, registers all APIs as callables,
configures the dialect, parses and evaluates synchronously, then wraps in `run_in_executor` +
`wait_for` for async.

**With Monty**:

```python
class MontyEngine:
    def __init__(self, tools_provider, config):
        self.tools_provider = tools_provider
        self.config = config

    async def evaluate_async(self, script, globals_dict, execution_context):
        # Declare external functions from tool names
        tool_names = [t.name for t in self.tools_provider.get_tools()]
        api_functions = ['json_encode', 'json_decode', 'time_now', ...]

        m = pydantic_monty.Monty(
            script,
            inputs=list(globals_dict.keys()) if globals_dict else [],
            external_functions=tool_names + api_functions,
        )

        limits = pydantic_monty.ResourceLimits(
            max_duration_secs=self.config.max_execution_time,
            max_memory=self.config.max_memory,  # new capability
        )

        # Use run_monty_async which handles the pause/resume loop
        result = await pydantic_monty.run_monty_async(
            m,
            inputs=globals_dict or {},
            external_functions=self._build_function_map(execution_context),
            limits=limits,
        )
        return result.output
```

The key insight: `run_monty_async` handles the pause/resume loop internally when given async
callables. Each external function call pauses execution, the host fulfills it (awaiting if async),
and resumes. **No thread pool, no background event loop, no sync/async bridge.**

### Tools API (`apis/tools.py` — 737 lines)

This is where the biggest simplification happens.

**Current complexity**: `ToolsAPI` class manages a background `Thread` with its own event loop, uses
`run_coroutine_threadsafe` to schedule tool calls from sync Starlark context, handles deadlock
prevention between main thread and background thread, deals with "Future attached to a different
loop" errors with asyncpg.

**With Monty**: The entire `ToolsAPI` class and its thread management can be replaced with a dict of
async callables:

```python
def _build_function_map(self, ctx):
    functions = {}

    # Each tool becomes a direct async callable
    for tool_def in self.tools_provider.get_tools():
        async def tool_fn(*args, _name=tool_def.name, **kwargs):
            return await self.tools_provider.execute(_name, kwargs, ctx)
        functions[tool_def.name] = tool_fn

    # API functions
    functions['json_encode'] = json.dumps  # sync is fine
    functions['json_decode'] = json.loads
    functions['time_now'] = time_api.time_now
    # ... etc

    return functions
```

**Estimated reduction**: ~500-600 lines of thread management code eliminated.

### Time API (`apis/time.py` — 638 lines)

**No change needed.** The time API functions are pure Python functions that return dicts. They work
identically as Monty external functions. The only difference is they'd be declared in
`external_functions` at parse time.

### Attachment API (`apis/attachments.py` — 609 lines)

**Similar simplification to tools.** The attachment API currently has its own sync/async bridging.
With Monty, `attachment_get`, `attachment_read`, and `attachment_create` become direct async
callables with no bridging needed.

### Script Syntax Changes

Scripts written for the LLM would look **more natural**:

```python
# Current Starlark
emails = search_emails(query="grocery")
if len(emails) > 0:
    # Can't catch errors — if this fails, script dies
    add_or_update_note(title="Groceries", content=emails[0]["body"])

# With Monty
emails = search_emails(query="grocery")
if emails:  # Truthiness works naturally
    try:
        add_or_update_note(title="Groceries", content=emails[0]["body"])
    except Exception as e:
        print(f"Failed to save note: {e}")
        # Can still continue with fallback logic
```

### wake_llm

**Straightforward mapping.** `wake_llm` becomes an external function. The engine collects
invocations and returns them alongside the script result.

### Configuration Mapping

| StarlarkConfig       | MontyEngine equivalent                        |
| -------------------- | --------------------------------------------- |
| `max_execution_time` | `ResourceLimits.max_duration_secs`            |
| `enable_print`       | `print_callback` parameter                    |
| `enable_debug`       | Same, via print_callback                      |
| `allowed_tools`      | Filter `external_functions` dict              |
| `deny_all_tools`     | Omit tool functions from `external_functions` |
| `disable_apis`       | Omit API functions from `external_functions`  |
| *(new)*              | `ResourceLimits.max_memory`                   |
| *(new)*              | `ResourceLimits.max_allocations`              |
| *(new)*              | `ResourceLimits.max_recursion_depth`          |

## Concerns and Risks

### 1. Maturity (High Risk)

Monty is at **version 0.0.3**, classified as **alpha**. The API is explicitly unstable and breaking
changes should be expected. For a hobby project this is probably acceptable, but it means:

- Updating Monty could require code changes
- Edge cases and bugs are more likely
- Documentation is sparse

**Mitigation**: The integration surface is small (one engine class + function registration). Changes
in the Monty API would be contained to `engine.py`.

### 2. No Class Definitions (Medium Risk)

Monty doesn't support `class` definitions yet (planned). This isn't a problem for our current
scripts, which are imperative tool-calling code, but limits future expressiveness.

**Mitigation**: We don't use classes in Starlark either. The LLM-generated scripts are typically
short imperative sequences.

### 3. Limited stdlib (Low Risk)

Only `sys`, `typing`, `asyncio`, and partial `dataclasses`/`json` support. No `re`, `datetime`,
`collections`, etc.

**Mitigation**: We already provide all complex functionality via external functions (our time API,
JSON API, tools). Scripts don't need stdlib access.

### 4. Platform Support (Low Risk)

Pre-built wheels for Linux and macOS only. No Windows.

**Mitigation**: We run in Linux containers. Not an issue.

### 5. Single-Resume Snapshots (Low Risk)

A `MontySnapshot` can only be `resume()`d once. This is a design constraint, not a limitation for
our use case where execution is linear.

## What We'd Gain

1. **~500-600 fewer lines** of sync/async bridge code (the most fragile part of the system)
2. **`try`/`except`** in scripts — graceful error handling for tool failures
3. **Actual Python syntax** — LLM generates correct code more often
4. **`async`/`await` + `asyncio.gather()`** — parallel tool calls from scripts
5. **Finer resource limits** — memory caps, allocation limits, GC control
6. **Serializable execution state** — could pause/resume scripts across restarts
7. **Type checking at parse time** — catch errors before execution via bundled `ty`

## What We'd Lose

1. **Maturity and stability** — starlark-pyo3 is battle-tested; Monty is alpha
2. **Guaranteed hermeticity** — Starlark's design prevents side effects by construction; Monty
   relies on the host not exposing dangerous functions (same practical outcome, different guarantee
   level)

## Recommendation

Monty is a strong candidate that addresses real pain points in the current implementation. The
migration is clean and would significantly simplify the codebase. The main question is timing:

**Option A — Adopt now**: Accept the alpha-quality risk for a hobby project. Pin to a specific
version, write good tests, and accept that Monty updates may require code changes. The integration
surface is small enough that this is manageable.

**Option B — Wait for 0.1.0+**: Monitor the project for a stable release. Monty is backed by
Pydantic/Samuel Colvin and is the execution engine for PydanticAI's "code-mode", so it has strong
incentives to mature. When it reaches beta/stable, migrate.

**Option C — Prototype on a branch**: Build a working `MontyEngine` alongside the existing
`StarlarkEngine`, run the test suite against both, and evaluate real-world behavior before
committing to the switch.

I'd lean toward **Option C** as the pragmatic choice — it gives concrete data on compatibility and
correctness without committing to a production switch on alpha software.

## Migration Steps (if proceeding)

01. `uv add pydantic-monty` alongside existing `starlark-pyo3`
02. Create `MontyEngine` class implementing the same interface as `StarlarkEngine`
03. Implement the external function registry (tools, time API, JSON, attachments, wake_llm)
04. Update `StarlarkConfig` → `ScriptConfig` with Monty-specific resource limits
05. Run existing scripting test suite against `MontyEngine` (parameterize tests for both engines)
06. Identify and fix any Starlark-specific syntax in test scripts
07. Update the `execute_script` tool docstring (Python syntax guidance instead of Starlark caveats)
08. Update `docs/user/scripting.md` and `prompts.yaml` tool descriptions
09. Remove `starlark-pyo3` dependency and `StarlarkEngine` once confident
10. Delete the sync/async bridge code in `apis/tools.py`
