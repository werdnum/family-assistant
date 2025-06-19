# Lua Scripting Migration Analysis

## Executive Summary

This document analyzes the feasibility and implications of migrating Family Assistant's scripting system from Starlark to Lua. While technically feasible with an estimated 2-3 week implementation effort, the migration presents mixed benefits. The current Starlark implementation provides strong security guarantees and adequate functionality, while Lua would offer more power and flexibility at the cost of increased complexity and security considerations.

## Current System Overview

### Starlark Implementation

The current scripting system uses Starlark, a Python-like configuration language designed by Google for use in Bazel. Key characteristics:

- **Language**: Starlark via `starlark-pyo3`
- **Security**: Sandboxed by design with no file/network access
- **Integration**: Thread-based async-to-sync bridge for tool execution
- **Features**: JSON support, tool access, timeout protection
- **Use Case**: Safe user automation scripts within Family Assistant

### Architecture Components

1. **StarlarkEngine**: Core execution engine with configuration management
2. **ToolsAPI**: Bridge between async Python tools and sync Starlark execution
3. **Security Layer**: Built-in language restrictions plus execution timeouts
4. **Tool Integration**: Multiple access methods (direct, prefixed, API calls)

## Proposed Lua Architecture

### Technology Stack

**Recommended**: `lupa` (LuaJIT integration)

- PyPI package embedding LuaJIT
- Bi-directional Python ↔ Lua object conversion
- Coroutine support for async patterns
- Mature and well-maintained

**Alternatives Considered**:

- `lua-python3`: Pure Python implementation (slower, limited features)
- CFFI/ctypes: Direct Lua binding (complex, platform-specific)

### Component Design

```text
src/family_assistant/scripting/
├── lua_engine.py       # Main Lua execution engine
├── lua_sandbox.py      # Security and sandboxing implementation
├── lua_tools_bridge.py # Bridge for tool access from Lua
├── lua_stdlib.py       # Standard library additions and overrides
└── lua_utils.py        # Type conversion and utility functions

```

### Implementation Architecture

```python
class LuaEngine:
    """Main Lua scripting engine with sandboxing and tool integration."""

    def __init__(self, config: LuaConfig):
        self.lua = lupa.LuaRuntime(
            unpack_returned_tuples=True,
            register_eval=False,  # Security: disable eval
            attribute_filter=self._attribute_filter
        )
        self._setup_sandbox()
        self._setup_tools_bridge()
        self._setup_stdlib()

    def _setup_sandbox(self):
        """Remove dangerous Lua functions and modules."""
        sandbox_script = """
        -- Remove file system access
        os = nil
        io = nil

        -- Remove module loading
        require = nil
        loadfile = nil
        dofile = nil
        package = nil

        -- Remove debugging capabilities
        debug = nil

        -- Limit string metatable access
        getmetatable('').__index = nil
        """
        self.lua.execute(sandbox_script)

    def _setup_tools_bridge(self):
        """Set up Family Assistant tools access from Lua."""
        # Similar to current ToolsAPI implementation
        pass

```

## Migration Requirements

### 1. Core Engine Development

**Tasks**:

- Implement `LuaEngine` with lupa integration
- Set up Lua runtime configuration
- Implement execution timeout mechanism using debug hooks
- Create error handling and exception translation layer

**Effort**: 2-3 days

### 2. Security Sandbox

**Tasks**:

- Remove dangerous Lua built-ins (os, io, require, etc.)
- Implement attribute access filtering
- Set up execution limits (memory, instructions)
- Create security test suite

**Key Challenges**:

- Lua provides more access by default than Starlark
- Must carefully audit all standard library functions
- Need to prevent metatable manipulation attacks

**Effort**: 2-3 days

### 3. Tools Bridge Implementation

**Async-to-Sync Bridge Design**:

```python
class LuaToolsBridge:
    def __init__(self, tools_provider, context):
        self.tools_provider = tools_provider
        self.context = context
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop)
        self._thread.start()

    def execute_tool(self, tool_name, **kwargs):
        """Execute async tool from sync Lua context."""
        future = asyncio.run_coroutine_threadsafe(
            self._execute_async(tool_name, kwargs),
            self._loop
        )
        return future.result(timeout=30)

```

**Effort**: 3-4 days

### 4. Type System Mapping

**Conversion Requirements**:

| Python Type | Lua Type | Notes |
|------------|----------|-------|
| dict | table | Key differences in iteration |
| list | table (array) | 1-based indexing in Lua |
| str | string | UTF-8 handling needed |
| int/float | number | Single numeric type in Lua |
| bool | boolean | Direct mapping |
| None | nil | Direct mapping |
| async function | coroutine | Wrapper needed |

**Special Handling**:

- JSON serialization/deserialization
- Date/time objects
- Complex tool return values

**Effort**: 2 days

### 5. Feature Parity Implementation

**Required Lua Standard Library Additions**:

```text
-- JSON support
json = {
    encode = function(value) ... end,
    decode = function(str) ... end
}

-- Enhanced string functions
string.split = function(str, sep) ... end
string.trim = function(str) ... end

-- Table utilities
table.keys = function(t) ... end
table.values = function(t) ... end
table.merge = function(t1, t2) ... end

```

**Effort**: 2 days

### 6. Testing & Edge Cases

**Test Categories**:

- Security sandbox verification
- Tool execution correctness
- Type conversion edge cases
- Performance benchmarks
- Error handling scenarios
- Timeout enforcement

**Effort**: 3-4 days

### 7. Migration Utilities

**Requirements**:

- Starlark to Lua syntax converter (basic)
- Script validation tool
- Side-by-side execution comparison
- Documentation generator

**Effort**: 1-2 days

## Comparative Analysis

### Security Comparison

| Aspect | Starlark | Lua |
|--------|----------|-----|
| File System Access | ❌ Impossible | ⚠️ Must remove |
| Network Access | ❌ Impossible | ⚠️ Must remove |
| Module Loading | ❌ No imports | ⚠️ Must disable |
| Memory Safety | ✅ Built-in | ✅ GC + limits |
| Execution Limits | ✅ Bounded | ⚠️ Debug hooks |
| Type Safety | ✅ Strong | ⚠️ Dynamic |

### Feature Comparison

| Feature | Starlark | Lua |
|---------|----------|-----|
| Performance | ⭐⭐⭐ Good | ⭐⭐⭐⭐⭐ Excellent (LuaJIT) |
| Language Features | ⭐⭐⭐ Limited | ⭐⭐⭐⭐⭐ Full language |
| Debugging | ⭐⭐⭐ Basic | ⭐⭐⭐⭐ Native debug hooks |
| Ecosystem | ⭐⭐ Minimal | ⭐⭐⭐⭐ Rich |
| Learning Curve | ⭐⭐⭐⭐ Python-like | ⭐⭐⭐ Different syntax |
| Documentation | ⭐⭐⭐ Adequate | ⭐⭐⭐⭐⭐ Extensive |

### Implementation Complexity

| Component | Starlark (Current) | Lua (Proposed) |
|-----------|-------------------|----------------|
| Core Engine | Simple | Moderate |
| Security | Built-in | Manual implementation |
| Tool Bridge | Thread-based | Similar approach |
| Type Conversion | Automatic | Semi-automatic |
| Error Handling | Straightforward | More complex |

## Risk Assessment

### High Risks

1. **Security Vulnerabilities**
   - **Risk**: Incomplete sandboxing could expose system
   - **Mitigation**: Comprehensive security audit and testing
   - **Impact**: Critical

2. **Migration Complexity**
   - **Risk**: Existing scripts need rewriting
   - **Mitigation**: Automated conversion tools
   - **Impact**: High

### Medium Risks

1. **Performance Regression**
   - **Risk**: Despite LuaJIT, bridge overhead could impact performance
   - **Mitigation**: Benchmark and optimize critical paths
   - **Impact**: Medium

2. **Type Conversion Issues**
   - **Risk**: Subtle bugs in Python ↔ Lua conversions
   - **Mitigation**: Extensive test coverage
   - **Impact**: Medium

### Low Risks

1. **User Adoption**
   - **Risk**: Users unfamiliar with Lua syntax
   - **Mitigation**: Good documentation and examples
   - **Impact**: Low

2. **Maintenance Burden**
   - **Risk**: More complex codebase to maintain
   - **Mitigation**: Clear architecture and documentation
   - **Impact**: Low

## Cost-Benefit Analysis

### Benefits of Migration

✅ **Performance**: LuaJIT offers exceptional performance for compute-intensive scripts

✅ **Language Features**: Full programming language with coroutines, metatables, etc.

✅ **Ecosystem**: Access to Lua libraries and patterns

✅ **Industry Standard**: Lua widely used in automation and embedded scripting

✅ **Debugging**: Better debugging capabilities with native hooks

### Costs of Migration

❌ **Development Effort**: 2-3 weeks of focused development

❌ **Security Complexity**: Manual sandboxing requires careful implementation

❌ **Migration Pain**: All existing scripts need conversion

❌ **Testing Overhead**: Comprehensive security and compatibility testing needed

❌ **Ongoing Maintenance**: More complex system to maintain

## Recommendations

### Proceed with Migration If

1. **Performance Critical**: Current scripts hit performance limitations
2. **Feature Requirements**: Need advanced language features (coroutines, metatables)
3. **User Base**: Users already familiar with Lua or requesting it
4. **Long-term Vision**: Planning to build extensive scripting ecosystem

### Stay with Starlark If

1. **Current System Adequate**: No pressing limitations in current implementation
2. **Security Paramount**: Prefer built-in security over manual implementation
3. **Resource Constraints**: Limited development resources for migration
4. **Risk Averse**: Cannot afford potential security or stability issues

## Implementation Timeline

If proceeding with migration:

| Week | Phase | Deliverables |
|------|-------|--------------|
| 1 | Core Development | LuaEngine, basic sandbox, type conversions |
| 2 | Integration | Tools bridge, stdlib additions, error handling |
| 3 | Testing & Polish | Security audit, performance testing, migration tools |

## Conclusion

While migrating to Lua is technically feasible and would provide certain advantages (performance, flexibility, ecosystem), the current Starlark implementation already provides a secure and functional scripting environment. The migration would require significant effort and introduce security complexity without addressing any critical limitations in the current system.

**Recommendation**: Unless there are specific compelling reasons (performance requirements, user demand, feature limitations), maintaining the current Starlark implementation is the pragmatic choice. The security-by-design nature of Starlark and the elegant existing implementation outweigh the potential benefits of Lua for Family Assistant's use case.

If migration becomes necessary in the future, this analysis provides a comprehensive roadmap for implementation.
