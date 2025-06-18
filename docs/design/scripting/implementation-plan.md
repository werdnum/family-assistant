# Starlark Scripting Engine Implementation Plan

## Overview

This document outlines the implementation plan for the core Starlark scripting engine in Family Assistant. The implementation follows a phased approach with incremental functionality and comprehensive testing at each stage.

## Current Status (Updated: December 2024)

### Summary
The Starlark scripting engine has been successfully implemented with Phases 1, 3, and most of Phase 4 completed. The engine is production-ready with full tool integration, security controls, and comprehensive testing. Only Phase 2 (TimeAPI and StateAPI) remains unimplemented.

### Completed Features

1. **Core Starlark Engine** ✅
   - StarlarkEngine class implemented in `src/family_assistant/scripting/engine.py`
   - Basic expression evaluation working
   - 10-minute execution timeout (to allow for external API calls)
   - Sandboxed environment (no file system or network access)
   - JSON encode/decode functions built-in

2. **Tool Integration** ✅
   - ToolsAPI fully implemented in `src/family_assistant/scripting/apis/tools.py`
   - Scripts can discover and execute all available tools
   - Two interfaces: functional (`tools_execute()`) and direct callable
   - Security controls via `allowed_tools` and `deny_all_tools` configuration
   - Comprehensive error handling and result serialization

3. **Execute Script Tool** ✅
   - `execute_script` tool added to the tool registry
   - Accessible via LLM and web API
   - Supports passing global variables to scripts
   - Integration with root tools provider for web API calls

4. **Production Features** ✅
   - Execution timeout (10 minutes for scripts, configurable)
   - Comprehensive error handling with line numbers
   - Sandboxing via Starlark's built-in restrictions
   - Full test coverage for implemented features
   - Custom exception types (ScriptSyntaxError, ScriptExecutionError, ScriptTimeoutError)
   - StarlarkConfig class for configuration management

### Not Yet Implemented

1. **TimeAPI** ❌
   - Current time access
   - Hour/day helpers
   - Time comparison utilities

2. **StateAPI** ❌
   - Persistent key-value storage
   - Per-script/user isolation
   - Database integration

## Current Capabilities Examples

With the current implementation, users can write scripts like:

```starlark
# List all available tools
tools = tools_list()
for tool in tools:
    print(tool["name"] + ": " + tool["description"])

# Add a note using functional interface
result = tools_execute("add_or_update_note", 
    title="Shopping List", 
    content="Milk, Eggs, Bread")

# Or using direct callable interface
add_or_update_note(
    title="Meeting Notes",
    content="Discussed project timeline"
)

# Search for documents
docs = search_documents(query="project timeline", limit=5)
for doc in docs:
    print(doc["title"])

# Complex automation with multiple tools
calendar_events = get_calendar_events(days_ahead=7)
for event in calendar_events:
    if "birthday" in event["summary"].lower():
        # Create a reminder note
        add_or_update_note(
            title="Birthday Reminder: " + event["summary"],
            content="Don't forget! Event on " + event["start"]
        )

# Working with JSON data
data = {"tasks": ["review PR", "update docs"]}
json_str = json_encode(data)
parsed = json_decode(json_str)
```

## Proposed Next Steps

### Immediate Priorities

1. **Documentation and Examples**
   - Create user documentation for writing Starlark scripts
   - Add example scripts demonstrating common automation patterns
   - Document all available functions and tool access patterns
   - Update USER_GUIDE.md with scripting instructions

2. **Implement TimeAPI** (Phase 2 completion)
   - Basic time access (`now()`, `today()`, `hour()`, `day_of_week()`)
   - Time comparison helpers (`is_between()`, `is_weekend()`)
   - Timezone support
   - Tests for time-based automation scenarios

3. **Implement StateAPI** (Phase 2 completion)
   - Design database schema for script state storage
   - Implement get/set/delete operations
   - Add per-script and per-user isolation
   - Add TTL/expiration support for temporary state
   - Tests for state persistence across executions

### Medium-term Goals

4. **Integration with Event System**
   - Allow scripts to be triggered by events
   - Add event context to script execution
   - Create event listener that executes scripts
   - Example: Run script when email received, calendar event approaching

5. **Enhanced Security and Limits**
   - Per-user script execution quotas
   - Memory usage tracking (if supported by starlark-pyo3)
   - Script size limits
   - Rate limiting for tool executions
   - Audit logging for all script executions

6. **Script Management**
   - Script storage and versioning
   - Script library/templates
   - Web UI for script editing and testing
   - Script scheduling (cron-like functionality)

### Long-term Vision

7. **Advanced Features**
   - Script debugging tools
   - Performance profiling
   - Multi-step script workflows
   - Script sharing and permissions
   - Integration with external script repositories

## Implementation Phases

### Phase 1: Foundation ✅ COMPLETED
**Goal**: Establish basic Starlark integration and simple expression evaluation

#### Tasks:
1. **Set up starlark-pyo3 dependency** ✅
   - Add to pyproject.toml
   - Verify installation and import

2. **Create basic StarlarkEngine class** ✅
   - Location: `src/family_assistant/scripting/engine.py`
   - Basic structure with initialization
   - Simple evaluate method for expressions

3. **Add test for basic expression evaluation** ✅
   - Location: `tests/functional/scripting/test_engine.py`
   - Test arithmetic expressions
   - Test boolean logic
   - Test string operations

#### Deliverables:
- Working StarlarkEngine that can evaluate simple expressions ✅
- Tests demonstrating basic functionality ✅
- All code passing lint checks ✅

### Phase 2: Context APIs
**Goal**: Enable scripts to access contextual information (time, state)

#### Tasks:
1. **Implement TimeAPI**
   - Current time access
   - Hour/day helpers
   - Time comparison utilities
   - `is_between()` helper for time ranges

2. **Implement StateAPI**
   - Key-value storage per script/user
   - get/set/delete operations
   - Type-safe value handling

3. **Create module builder**
   - Method to inject APIs into Starlark module
   - Proper scoping and isolation
   - Context parameter passing

4. **Add tests for context APIs**
   - Test time-based conditions
   - Test state persistence
   - Test API isolation between executions

#### Deliverables:
- Scripts can access time and maintain state
- Comprehensive tests for all context APIs
- Documentation of available APIs

### Phase 3: Tool Integration ✅ COMPLETED
**Goal**: Enable scripts to execute tools via ToolsProvider

#### Tasks:
1. **Create ToolsAPI wrapper** ✅
   - Bridge between Starlark and ToolsProvider
   - Handle tool discovery (`tools_list`, `tools_get`)
   - Execute tools with parameters (`tools_execute`, `tools_execute_json`)
   - Return handling and error propagation
   - Direct callable interface (tools as functions)

2. **Implement tool execution** ✅
   - Parameter validation
   - Async tool execution from sync Starlark
   - Result serialization back to Starlark

3. **Add security controls** ✅
   - Tool allowlist configuration (`allowed_tools`)
   - Execution permission checks (`deny_all_tools`)
   - Security event logging

4. **Add tests for tool execution** ✅
   - Mock ToolsProvider integration
   - Test successful tool calls
   - Test error handling
   - Test permission controls
   - Test direct callable interface

#### Deliverables:
- Scripts can discover and execute allowed tools ✅
- Security controls for tool access ✅
- Tests covering success and failure scenarios ✅

### Phase 4: Production Readiness ✅ MOSTLY COMPLETED
**Goal**: Add robustness features for production use

#### Tasks:
1. **Add execution limits** ✅ PARTIAL
   - CPU timeout (configurable, default 10 minutes for scripts) ✅
   - Memory limits (not supported by starlark-pyo3) ❌
   - Script size limits ❌
   - Recursion depth limits (built into Starlark) ✅

2. **Implement error handling** ✅
   - Graceful handling of syntax errors (ScriptSyntaxError) ✅
   - Runtime error capture and reporting (ScriptExecutionError) ✅
   - Useful error messages for debugging ✅
   - Error context (line numbers, etc.) ✅

3. **Add audit logging** ✅ PARTIAL
   - Script execution logging ✅
   - Performance metrics ❌
   - Error tracking ✅
   - Tool call auditing (via security events) ✅

4. **Create integration tests** ✅
   - Real-world automation scenarios ✅
   - Complex multi-step scripts ✅
   - Error recovery scenarios ✅
   - Performance benchmarks ❌

#### Deliverables:
- Production-ready engine with safety limits ✅
- Comprehensive error handling ✅
- Full test coverage of edge cases ✅
- Performance baselines established ❌

## Technical Design

### API Interface Design Note

**Important**: The exact interface exposed to scripts (TimeAPI, StateAPI, ToolsAPI) is subject to refinement based on anticipated use cases. The specific methods, parameters, and return types may evolve as we better understand how users will write automation scripts. However, this does not block the infrastructure work - we can proceed with the core engine implementation using placeholder APIs that can be adjusted later. The key is to establish the pattern for how APIs are exposed to scripts, not the exact API surface.

### Directory Structure
```
src/family_assistant/scripting/
├── __init__.py
├── engine.py          # Main StarlarkEngine class ✅
├── apis/              # Context APIs
│   ├── __init__.py    ✅
│   ├── time.py       # TimeAPI implementation (NOT YET)
│   ├── state.py      # StateAPI implementation (NOT YET)
│   └── tools.py      # ToolsAPI implementation ✅
└── errors.py         # Custom exceptions ✅

tests/functional/scripting/
├── __init__.py                    ✅
├── test_engine.py                 # Basic engine tests ✅
├── test_tools_api.py              # Tools API tests ✅
├── test_direct_tool_callables.py  # Direct callable tests ✅
├── test_tools_security.py         # Security tests ✅
├── test_json_functions.py         # JSON function tests ✅
└── test_execute_script.py         # Integration tests ✅
```

### Key Classes

```python
class StarlarkEngine:
    """Main engine for executing Starlark scripts"""
    
    def __init__(self, 
                 tools_provider: Optional[ToolsProvider] = None,
                 config: Optional[ScriptingConfig] = None):
        pass
    
    async def evaluate(self, 
                      code: str, 
                      context: Dict[str, Any]) -> Any:
        """Execute Starlark code with given context"""
        pass

class TimeAPI:
    """Time utilities for scripts"""
    
class StateAPI:
    """Persistent state for scripts"""
    
class ToolsAPI:
    """Bridge to ToolsProvider for scripts"""
```

### Testing Strategy

1. **Unit Tests**: Each API tested in isolation
2. **Integration Tests**: APIs working together
3. **Functional Tests**: Real-world scenarios
4. **Performance Tests**: Execution time benchmarks
5. **Security Tests**: Sandbox escape attempts

### Success Criteria

- [x] All tests passing for implemented features
- [x] Lint checks passing (ruff, basedpyright, pylint)
- [x] Sub-100ms execution for simple scripts (typical execution < 10ms)
- [x] Sandboxed execution environment preventing security vulnerabilities
- [x] Clear error messages with line numbers and context
- [ ] Comprehensive user documentation (in progress)
- [ ] TimeAPI implementation with tests
- [ ] StateAPI implementation with tests
- [ ] Integration with event system

## Implementation Notes

### Subagent Usage

Each self-contained task will use a subagent:
- "Investigate starlark-pyo3 API details"
- "Implement TimeAPI with tests"
- "Add error handling to engine"
- "Fix specific test failures"

### Best Practices

1. Follow existing codebase patterns
2. Use async/await consistently
3. Add type hints for all public APIs
4. Write tests before implementation
5. Keep commits small and focused
6. Run lint checks before committing

### Dependencies

- `starlark-pyo3`: Core Starlark implementation
- Existing Family Assistant components:
  - `ToolsProvider` for tool integration
  - `DatabaseContext` for state storage
  - Configuration system
  - Logging infrastructure

## Timeline

- **Week 1**: Phase 1 + Phase 2 (Foundation and Context APIs)
- **Week 2**: Phase 3 (Tool Integration)
- **Week 3**: Phase 4 (Production Readiness)
- **Week 4**: Integration with event system (future work)

## Future Work

After core engine implementation:
- Integration with event listener system
- Web UI for script editing
- Script library/templates
- Advanced debugging tools
- Performance optimizations