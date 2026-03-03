# Monty 0.0.7 Improvements Plan

Two improvements to adopt from the Monty 0.0.7 upgrade:

1. **Simplify engine with `run_monty_async()`** - Replace manual start/resume loop
2. **Add static type checking** - Validate scripts before execution

## 1. Simplify Engine with `run_monty_async()`

### Current State

`MontyEngine._evaluate_async_impl()` (lines 102-202) manually manages the start/resume loop:

- Calls `m.start()` in a thread pool
- Loops on `MontySnapshot`, dispatching external function calls
- Distinguishes sync vs async functions
- Handles exceptions by resuming with the error
- Runs `progress.resume()` in thread pool for each step

This is ~80 lines of loop management code.

### Proposed Change

Replace the manual loop with `pydantic_monty.run_monty_async()`, which:

- Handles the entire start/resume loop internally
- Automatically detects sync vs async external functions
- Runs CPU-bound Monty execution in a `ThreadPoolExecutor`
- Handles `MontyFutureSnapshot` for parallel async (enabling `asyncio.gather()` in scripts)
- Properly cleans up tasks on failure

### What Changes

**`_evaluate_async_impl`** simplifies from:

```python
m = Monty(script, inputs=..., external_functions=names)
progress = await loop.run_in_executor(None, partial(m.start, ...))
while not isinstance(progress, MontyComplete):
    # 50+ lines of dispatch logic
```

To:

```python
m = Monty(script, inputs=..., external_functions=names)
result = await run_monty_async(m, inputs=..., external_functions=impls, ...)
return result
```

**`_build_execution_context_async`** no longer needs to return `ext_fn_names` separately - just the
names (for `Monty()` constructor) and the impls dict (for `run_monty_async()`).

### New Capability: `asyncio.gather()` in Scripts

The current manual loop doesn't handle `MontyFutureSnapshot` (parallel async). With
`run_monty_async()`, scripts can use:

```python
import asyncio
results = await asyncio.gather(
    search_notes(query="groceries"),
    get_calendar_events(days_ahead=7),
)
```

This runs both tool calls concurrently instead of sequentially.

### Error Handling

`run_monty_async()` propagates exceptions from Monty (syntax errors, runtime errors) directly, so
our existing try/except blocks for `MontySyntaxError`, `MontyRuntimeError`, etc. continue to work.

The one difference: `run_monty_async()` raises `KeyError` for unknown functions instead of our
current `NameError`. We should test this and adjust the error message if needed.

## 2. Add Static Type Checking for Scripts

### Motivation

Scripts currently validate by trial execution (running with sample data). This misses type errors
that only appear at runtime. Monty 0.0.7 includes a `type_check()` method using the bundled `ty`
type checker that can catch errors at parse time.

### How It Works

```python
m = Monty(script, external_functions=['search_notes', 'time_now'])

# Provide type stubs for external functions
prefix = '''
def search_notes(*, query: str) -> str: ...
def time_now() -> dict[str, int | str]: ...
'''

try:
    m.type_check(prefix_code=prefix)
except MontyTypingError as e:
    # e.g. "Argument to function `search_notes` is incorrect:
    #        Expected `str`, found `int`"
    print(f"Type error: {e}")
```

### Where to Add Type Checking

1. **`EventConditionEvaluator.validate_script()`** - Currently validates by executing with sample
   data. Add type checking as a first pass before execution validation.

2. **`MontyEngine.evaluate_async()`** - Optionally type-check before execution (configurable, since
   it adds latency). Most valuable for automation scripts that run repeatedly.

3. **Automation creation API** (`automations_api.py`) - Type-check action scripts and condition
   scripts when creating/updating automations.

### Generating Type Stubs

Need to generate prefix code with type signatures for all external functions:

- **Tool functions**: Generate from tool definitions (parameter schemas → type hints)
- **Time API**: Static stubs (signatures don't change)
- **JSON API**: Static stubs (`json_encode(obj: Any) -> str`, `json_decode(s: str) -> Any`)
- **Attachment API**: Static stubs
- **`wake_llm`**: Static stub

#### Tool stub generation example

From a tool definition:

```json
{
  "name": "search_notes",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "Search query"}
    },
    "required": ["query"]
  }
}
```

Generate:

```python
def search_notes(*, query: str) -> str: ...
```

JSON Schema type mapping: `string` → `str`, `integer` → `int`, `number` → `float`, `boolean` →
`bool`, `array` → `list`, `object` → `dict`. Optional parameters get `| None = None`.

### Implementation

Add a `TypeStubGenerator` class to `family_assistant.scripting` that:

1. Accepts tool definitions and API configurations
2. Generates Python type stub strings
3. Caches generated stubs (tool definitions rarely change)

Add a `type_check()` method to `MontyEngine` that:

1. Compiles the script
2. Generates type stubs for all registered external functions
3. Calls `monty.type_check(prefix_code=stubs)`
4. Returns errors or raises `ScriptTypingError`

### Validation Integration

Update `EventConditionEvaluator.validate_script()` to:

1. First: type-check (fast, no execution needed)
2. Then: execution validation with sample data (as today)

Update automation creation to call type checking on action scripts.

## Implementation Order

1. Simplify engine with `run_monty_async()` first (simpler, lower risk)
2. Add type stub generation
3. Add type checking to MontyEngine
4. Integrate into condition evaluator and automation creation
