# Starlark Scripting Engine Implementation Plan

## Overview

This document outlines the implementation plan for the core Starlark scripting engine in Family Assistant. The implementation follows a phased approach with incremental functionality and comprehensive testing at each stage.

## Implementation Phases

### Phase 1: Foundation
**Goal**: Establish basic Starlark integration and simple expression evaluation

#### Tasks:
1. **Set up starlark-pyo3 dependency**
   - Add to pyproject.toml
   - Verify installation and import

2. **Create basic StarlarkEngine class**
   - Location: `src/family_assistant/scripting/engine.py`
   - Basic structure with initialization
   - Simple evaluate method for expressions

3. **Add test for basic expression evaluation**
   - Location: `tests/functional/scripting/test_engine.py`
   - Test arithmetic expressions
   - Test boolean logic
   - Test string operations

#### Deliverables:
- Working StarlarkEngine that can evaluate simple expressions
- Tests demonstrating basic functionality
- All code passing lint checks

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

### Phase 3: Tool Integration
**Goal**: Enable scripts to execute tools via ToolsProvider

#### Tasks:
1. **Create ToolsAPI wrapper**
   - Bridge between Starlark and ToolsProvider
   - Handle tool discovery (`is_available`)
   - Execute tools with parameters
   - Return handling and error propagation

2. **Implement tool execution**
   - Parameter validation
   - Async tool execution from sync Starlark
   - Result serialization back to Starlark

3. **Add security controls**
   - Tool allowlist configuration
   - Execution permission checks
   - Rate limiting preparation

4. **Add tests for tool execution**
   - Mock ToolsProvider integration
   - Test successful tool calls
   - Test error handling
   - Test permission controls

#### Deliverables:
- Scripts can discover and execute allowed tools
- Security controls for tool access
- Tests covering success and failure scenarios

### Phase 4: Production Readiness
**Goal**: Add robustness features for production use

#### Tasks:
1. **Add execution limits**
   - CPU timeout (configurable, default 5s)
   - Memory limits (if supported by starlark-pyo3)
   - Script size limits
   - Recursion depth limits

2. **Implement error handling**
   - Graceful handling of syntax errors
   - Runtime error capture and reporting
   - Useful error messages for debugging
   - Error context (line numbers, etc.)

3. **Add audit logging**
   - Script execution logging
   - Performance metrics
   - Error tracking
   - Tool call auditing

4. **Create integration tests**
   - Real-world automation scenarios
   - Complex multi-step scripts
   - Error recovery scenarios
   - Performance benchmarks

#### Deliverables:
- Production-ready engine with safety limits
- Comprehensive error handling
- Full test coverage of edge cases
- Performance baselines established

## Technical Design

### API Interface Design Note

**Important**: The exact interface exposed to scripts (TimeAPI, StateAPI, ToolsAPI) is subject to refinement based on anticipated use cases. The specific methods, parameters, and return types may evolve as we better understand how users will write automation scripts. However, this does not block the infrastructure work - we can proceed with the core engine implementation using placeholder APIs that can be adjusted later. The key is to establish the pattern for how APIs are exposed to scripts, not the exact API surface.

### Directory Structure
```
src/family_assistant/scripting/
├── __init__.py
├── engine.py          # Main StarlarkEngine class
├── apis/              # Context APIs
│   ├── __init__.py
│   ├── time.py       # TimeAPI implementation
│   ├── state.py      # StateAPI implementation
│   └── tools.py      # ToolsAPI implementation
└── errors.py         # Custom exceptions

tests/functional/scripting/
├── __init__.py
├── test_engine.py    # Basic engine tests
├── test_apis.py      # Context API tests
├── test_tools.py     # Tool integration tests
└── test_integration.py # Full scenario tests
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

- [ ] All tests passing
- [ ] Lint checks passing (ruff, basedpyright, pylint)
- [ ] Sub-100ms execution for simple scripts
- [ ] No security vulnerabilities
- [ ] Clear error messages
- [ ] Comprehensive documentation

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