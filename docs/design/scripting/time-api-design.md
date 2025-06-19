# Time API Design for Starlark Scripts

## Overview

This document describes the design of a time API for Family Assistant's Starlark scripting engine. The goal is to provide time manipulation capabilities that feel natural to Starlark users while working within the constraints of starlark-pyo3.

## Background

### starlark-go's Time Module

The starlark-go implementation provides a comprehensive time module with the following features:

1. **Time Construction**:
   - `time(year?, month?, day?, hour?, minute?, second?, nanosecond?, location?)` - Create a Time object
   - `from_timestamp(sec, nsec?)` - Convert Unix timestamp to Time
   - `now()` - Get current local time
   - `parse_time(x, format?, location?)` - Parse time from string

2. **Time Object Methods**:
   - `in_location(location)` - Convert time to specified location
   - `format(format_string)` - Format time according to a string

3. **Time Object Attributes**:
   - `year`, `month`, `day`, `hour`, `minute`, `second`, `nanosecond`
   - `unix`, `unix_nano` - Unix timestamp representations

4. **Duration Support**:
   - Constants: `nanosecond`, `microsecond`, `millisecond`, `second`, `minute`, `hour`
   - `parse_duration(d)` - Parse duration from string
   - Arithmetic: Time ± Duration, Duration ± Duration, Duration × number

5. **Timezone Support**:
   - `is_valid_timezone(loc)` - Check if timezone name is valid
   - Support for named timezones (e.g., "America/New_York")

### starlark-pyo3 Constraints

The starlark-pyo3 library has significant limitations compared to starlark-go:

1. **JSON-only types**: Can only pass JSON-serializable values between Python and Starlark
2. **No custom objects**: Cannot create custom Starlark types with methods
3. **Function-based API**: All functionality must be exposed as functions, not methods
4. **No operator overloading**: Cannot implement custom arithmetic operations

## Proposed API Design

Given these constraints, we propose a function-based API that mimics starlark-go's patterns as closely as possible.

### Time Representation

Since we cannot create custom Time objects, we'll represent times as dictionaries:

```python
{
    "year": 2024,
    "month": 1,
    "day": 15,
    "hour": 14,
    "minute": 30,
    "second": 45,
    "nanosecond": 0,
    "unix": 1705329045,
    "unix_nano": 1705329045000000000,
    "timezone": "America/New_York"
}

```

### Core Functions

#### Time Creation

```python

# Create a time object
t = time_create(year=2024, month=1, day=15, hour=14, minute=30, timezone="America/New_York")

# Get current time
now = time_now()  # Returns time dict in local timezone
now_utc = time_now_utc()  # Returns time dict in UTC

# From Unix timestamp
t = time_from_timestamp(1705329045)
t_with_nanos = time_from_timestamp(1705329045, 500000000)

# Parse from string
t = time_parse("2024-01-15 14:30:45")
t = time_parse("Jan 15, 2024 2:30 PM", format="%b %d, %Y %I:%M %p")
t = time_parse("2024-01-15T14:30:45-05:00", timezone="America/New_York")

```

#### Time Manipulation

```python

# Convert timezone
t_utc = time_in_location(t, "UTC")
t_tokyo = time_in_location(t, "Asia/Tokyo")

# Format time
formatted = time_format(t, "%Y-%m-%d %H:%M:%S")
iso = time_format(t, "RFC3339")  # Special format constants

# Add/subtract duration (in seconds)
tomorrow = time_add(t, 86400)  # Add 1 day
yesterday = time_add(t, -86400)  # Subtract 1 day

# Add/subtract with duration units
next_week = time_add_duration(t, 7, "days")
earlier = time_add_duration(t, -30, "minutes")

# Get components
year = time_get_year(t)
month = time_get_month(t)
day = time_get_day(t)
hour = time_get_hour(t)
minute = time_get_minute(t)
second = time_get_second(t)
weekday = time_get_weekday(t)  # 0=Sunday, 6=Saturday

```

#### Duration Functions

```python

# Parse duration strings
d = duration_parse("1h30m")  # Returns seconds: 5400
d = duration_parse("2d12h")  # Returns seconds: 216000

# Duration arithmetic (all return seconds)
total = duration_add(3600, 1800)  # 1 hour + 30 minutes
diff = duration_subtract(7200, 3600)  # 2 hours - 1 hour
double = duration_multiply(3600, 2)  # 1 hour × 2
half = duration_divide(3600, 2)  # 1 hour ÷ 2

# Human-readable duration
readable = duration_human(5400)  # Returns "1h30m"
readable = duration_human(90)  # Returns "1m30s"

```

#### Comparison Functions

```python

# Compare times
is_before = time_before(t1, t2)
is_after = time_after(t1, t2)
is_equal = time_equal(t1, t2)

# Time difference
diff_seconds = time_diff(t2, t1)  # Returns seconds between times

```

#### Timezone Functions

```python

# Check timezone validity
valid = timezone_is_valid("America/New_York")  # Returns True
valid = timezone_is_valid("Invalid/Zone")  # Returns False

# Get timezone offset
offset = timezone_offset("America/New_York", t)  # Returns offset in seconds

```

### Duration Constants

We'll provide duration constants as a convenience:

```python

# Duration constants (in seconds)
NANOSECOND = 0.000000001
MICROSECOND = 0.000001
MILLISECOND = 0.001
SECOND = 1
MINUTE = 60
HOUR = 3600
DAY = 86400
WEEK = 604800

```

### Example Usage

Here's how common time operations would look in practice:

```python

# Get current time and format it
now = time_now()
formatted = time_format(now, "%Y-%m-%d %H:%M:%S")
print("Current time:", formatted)

# Schedule something for tomorrow at 9 AM
tomorrow_9am = time_create(
    year=time_get_year(now),
    month=time_get_month(now),
    day=time_get_day(now) + 1,
    hour=9,
    minute=0,
    timezone=now["timezone"]
)

# Calculate time until an event
event_time = time_parse("2024-12-25 00:00:00", timezone="UTC")
seconds_until = time_diff(event_time, time_now_utc())
days_until = duration_divide(seconds_until, DAY)
print("Days until Christmas:", int(days_until))

# Work with different timezones
meeting_ny = time_create(year=2024, month=1, day=15, hour=14, timezone="America/New_York")
meeting_tokyo = time_in_location(meeting_ny, "Asia/Tokyo")
print("Meeting time in Tokyo:", time_format(meeting_tokyo, "%Y-%m-%d %H:%M"))

# Parse and manipulate durations
work_duration = duration_parse("8h30m")
break_duration = duration_parse("1h")
actual_work = duration_subtract(work_duration, break_duration)
print("Actual work time:", duration_human(actual_work))

```

## Differences from starlark-go

### Major Differences

1. **No Time Type**: Times are dictionaries instead of custom objects
2. **No Method Calls**: All operations are functions, not methods
3. **No Operator Overloading**: Must use explicit functions for arithmetic
4. **Simplified Duration**: Durations are just numbers (seconds) rather than a special type

### API Mapping

| starlark-go | Our Implementation | Notes |
|-------------|-------------------|-------|
| `time.now()` | `time_now()` | Returns dict instead of Time object |
| `t.format(fmt)` | `time_format(t, fmt)` | Function instead of method |
| `t.in_location(loc)` | `time_in_location(t, loc)` | Function instead of method |
| `t + duration` | `time_add(t, seconds)` | Explicit function call |
| `t1 - t2` | `time_diff(t1, t2)` | Returns seconds |
| `time.parse_duration(s)` | `duration_parse(s)` | Returns seconds |

### Design Rationale

These differences exist because:

1. **Type System Limitations**: starlark-pyo3 cannot create custom Starlark types with methods
2. **JSON Serialization**: All values must be JSON-serializable for Python-Starlark bridge
3. **Operator Constraints**: Cannot implement custom operators in starlark-pyo3
4. **Simplicity**: Function-based API is more explicit and easier to document

## Implementation Plan

### Phase 1: Core Time Functions

- Implement basic time creation and manipulation functions
- Add timezone support using Python's `zoneinfo` module
- Create comprehensive test suite

### Phase 2: Duration Support

- Implement duration parsing and arithmetic
- Add human-readable duration formatting
- Support common duration calculations

### Phase 3: Integration

- Add time module to Starlark engine
- Create example scripts demonstrating usage
- Document in user guide

### Phase 4: Extended Features (Future)

- Add calendar-aware operations (next Monday, last day of month)
- Support for recurring time patterns
- Integration with calendar tools

## Security Considerations

1. **Timezone Validation**: Always validate timezone names to prevent errors
2. **Resource Limits**: Parsing complex time formats should have timeouts
3. **Time Bounds**: Consider limiting time ranges to prevent overflow issues

## Testing Strategy

1. **Unit Tests**: Test each function independently
2. **Integration Tests**: Test time operations in Starlark scripts
3. **Timezone Tests**: Ensure correct behavior across timezones
4. **Edge Cases**: Test boundary conditions (leap years, DST transitions)

## Documentation Requirements

1. **API Reference**: Document every function with examples
2. **Migration Guide**: Help users familiar with starlark-go adapt
3. **Common Patterns**: Cookbook of common time operations
4. **Timezone Guide**: Best practices for timezone handling

## Conclusion

This design provides a comprehensive time API that balances familiarity for Starlark users with the technical constraints of our implementation. While we cannot perfectly replicate starlark-go's object-oriented API, our function-based approach provides equivalent functionality with clear, explicit operations.

The API is designed to be:

- **Intuitive**: Functions names clearly indicate their purpose
- **Complete**: Covers all common time manipulation needs
- **Efficient**: Minimal overhead in the Python-Starlark bridge
- **Extensible**: Easy to add new functions as needs arise
