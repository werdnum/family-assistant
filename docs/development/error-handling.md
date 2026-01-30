# Error Handling Guidelines

This document defines the project's approach to error handling. The goal is to ensure errors are
visible, debuggable, and caught early rather than masked by "graceful" fallbacks.

## Core Principles

### 1. Prevention First

**First preference: Detect and prevent errors at development time.**

Before deciding how to handle an error at runtime, consider whether it can be prevented entirely:

- **Type checking**: Use type hints and static analysis to catch errors before runtime
- **Invariant tests**: Write tests that verify preconditions and postconditions
- **Input validation**: Validate at system boundaries (user input, external APIs)
- **Schema validation**: Use Pydantic or TypedDict for structured data

If an error can be caught by the type checker or a test, that's far better than handling it at
runtime.

### 2. Fatal vs. Recoverable Errors

When runtime handling is necessary, decide if the error is **fatal** or **recoverable**:

| Error Type      | Definition                                                                   | Example                                  | Handling                                   |
| --------------- | ---------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------ |
| **Fatal**       | Violates a fundamental invariant; continuing would produce incorrect results | Database connection lost mid-transaction | Let exception propagate; fail fast         |
| **Recoverable** | Expected failure case that can be handled without compromising correctness   | Network timeout on optional feature      | Handle explicitly with clear user feedback |

**Key question**: Is it **correct** to continue, not just **possible** to continue?

### 3. Handle at the Right Layer

Errors should be handled at the layer that has enough context to handle them correctly:

- **Too low**: Catches error but doesn't know what the user was trying to do
- **Too high**: Has to handle many different error types with generic fallbacks
- **Just right**: Knows the operation context and can provide meaningful recovery or messaging

When in doubt, let the error propagate up. A layer that cannot provide meaningful recovery should
not catch the error.

## Anti-Patterns to Avoid

### Silent Failures

**Never silently drop or ignore errors.** This is the most critical anti-pattern.

```python
# BAD: Error is silently swallowed
try:
    result = process_data(data)
except Exception:
    result = None  # Silent failure - caller has no idea something went wrong
```

```python
# BAD: Partial data loss is hidden
def process_user_input(messages: list[Message]) -> list[ProcessedMessage]:
    results = []
    for msg in messages:
        try:
            results.append(process(msg))
        except Exception:
            pass  # Message silently dropped - user will never know
    return results
```

### Cascading Silent Failures

The worst case: a silent failure at one layer causes a confusing error at another.

```python
# BAD: This pattern creates confusing errors
def extract_video(message: Message) -> Video | None:
    try:
        return message.video
    except Exception:
        return None  # "Graceful" fallback

def process_message(message: Message) -> str:
    video = extract_video(message)
    if video is None:
        # Now we can't tell: was there no video, or did extraction fail?
        return process_text_only(message)  # May be empty!

def handle_request(message: Message) -> Response:
    content = process_message(message)
    if not content:
        raise ValueError("Empty input")  # User sees "empty input" but they sent a video!
```

**The user sent a video, got an error saying "empty input", and has no idea why.**

### Catch-and-Return-Null

This pattern almost always masks bugs:

```python
# BAD: Masks bugs, makes debugging impossible
def get_user(user_id: int) -> User | None:
    try:
        return db.query(User).filter_by(id=user_id).one()
    except Exception:
        return None  # Could be: user not found, DB error, network error, bug in query...
```

```python
# GOOD: Let expected vs unexpected errors be distinguishable
def get_user(user_id: int) -> User | None:
    try:
        return db.query(User).filter_by(id=user_id).one()
    except NoResultFound:
        return None  # Expected case: user doesn't exist
    # Other exceptions propagate - they indicate bugs or infrastructure issues
```

### Overly Broad Exception Handling

```python
# BAD: Catches too much, hides bugs
try:
    result = complex_operation()
except Exception as e:
    logger.warning(f"Operation failed: {e}")
    return default_value
```

```python
# GOOD: Catch specific exceptions you know how to handle
try:
    result = complex_operation()
except NetworkTimeoutError:
    logger.warning("Network timeout, using cached value")
    return cached_value
except RateLimitError as e:
    logger.warning(f"Rate limited, retry after {e.retry_after}s")
    raise  # Let caller decide whether to retry
# Other exceptions propagate - they're bugs we need to fix
```

## Recommended Patterns

### Explicit Error Types

Define specific exception types for your domain:

```python
class VideoProcessingError(Exception):
    """Failed to process video content."""
    pass

class UnsupportedMediaTypeError(VideoProcessingError):
    """Media type is not supported for processing."""
    pass
```

### Fail Fast with Context

When you must fail, fail with useful context:

```python
def process_video(video: Video) -> ProcessedVideo:
    if video.format not in SUPPORTED_FORMATS:
        raise UnsupportedMediaTypeError(
            f"Video format '{video.format}' not supported. "
            f"Supported formats: {SUPPORTED_FORMATS}"
        )
    # ... processing
```

### Explicit "No Result" vs "Error"

Use the type system to distinguish between "no result" and "error":

```python
from typing import Optional

def find_user(email: str) -> Optional[User]:
    """Returns None if user doesn't exist. Raises on database errors."""
    return db.query(User).filter_by(email=email).one_or_none()
    # NoResultFound -> None
    # MultipleResultsFound -> propagates (bug: should be unique)
    # OperationalError -> propagates (infrastructure issue)
```

### Preserve Error Chain

When re-raising, preserve the original error:

```python
try:
    external_api.call()
except ExternalAPIError as e:
    raise ProcessingError(f"Failed to process request: {e}") from e
```

### Enforce Invariants, Don't Degrade

Prefer clear failures over "graceful degradation" that produces confusing results:

```python
# BAD: "Graceful" degradation produces confusing results
def get_user_display_name(user: User) -> str:
    return user.display_name or user.email or user.id or "Unknown"

# GOOD: Enforce the invariant that users have identifiable names
def get_user_display_name(user: User) -> str:
    if user.display_name:
        return user.display_name
    if user.email:
        return user.email
    raise ValueError(f"User {user.id} has no display name or email")
```

## Decision Checklist

When encountering an error condition, ask these questions in order:

1. **Can this be prevented at development time?**

   - Add type hints, validation, or tests → Done

2. **Is this a normal/expected case or an error?**

   - Normal case (e.g., user not found) → Handle with explicit return type
   - Error case → Continue to next question

3. **Is it correct to continue if this error occurs?**

   - No → Let exception propagate (fail fast)
   - Yes → Continue to next question

4. **Does this layer have enough context to handle it correctly?**

   - No → Let exception propagate
   - Yes → Handle with specific exception type and meaningful recovery

5. **Will the user understand what happened?**

   - If handling produces user-facing behavior, ensure error is communicated clearly
   - Never silently drop user input or produce confusing downstream errors

## Summary

| Do                                  | Don't                                           |
| ----------------------------------- | ----------------------------------------------- |
| Prevent errors with types and tests | Handle at runtime what can be caught statically |
| Catch specific exceptions           | Catch broad `Exception`                         |
| Propagate what you can't handle     | Swallow errors and return null                  |
| Fail fast with context              | "Gracefully degrade" into confusing states      |
| Preserve error chains               | Lose error context when re-raising              |
| Make errors visible                 | Silently drop data or input                     |
