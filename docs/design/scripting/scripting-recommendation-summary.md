# Scripting Language Recommendation Summary

## Final Recommendation: Starlark-Only

After thorough analysis, we recommend implementing **Starlark as the single scripting language** for Family Assistant, using the `starlark-pyo3` library.

## Key Insights

1. **The LLM is the primary "developer"** - It doesn't benefit from "simpler" syntax or need different languages for different complexity levels

2. **Maintenance overhead of two languages isn't justified** - We'd need double the integration code, testing, documentation, and security auditing

3. **Starlark scales naturally** - From simple one-line expressions to complex multi-function scripts without language switching

4. **Performance difference is negligible** - Event automation doesn't need microsecond-level optimization; milliseconds are fine

5. **Industry precedent** - Successful automation systems (Bazel, Terraform, Ansible) use one language that scales, not two

## Implementation Plan

### Quick Start

```bash
pip install starlark-pyo3
```

### Simple Example

```python
import starlark as sl

# Create a simple event filter
code = "event.temperature > 30 and time.hour >= 6"

# Set up execution environment
globals = sl.Globals.standard()
module = sl.Module()
module["event"] = {"temperature": 35}
module["time"] = {"hour": 14}

# Execute
ast = sl.parse("filter", code)
result = sl.eval(module, ast, globals)
print(result)  # True
```

### Architecture

```
┌─────────────────┐
│ Event Listener  │
├─────────────────┤
│ Starlark Filter │ ←── "event.temp > 30 and time.hour >= 6"
├─────────────────┤
│ Starlark Action │ ←── Complex automation script
└─────────────────┘
         ↓
┌─────────────────┐
│ Starlark Engine │ ←── Single implementation
├─────────────────┤
│ • Parser        │
│ • Evaluator     │
│ • Context APIs  │
│ • Security      │
└─────────────────┘
```

## Benefits

1. **Simplicity**: One language, one integration, one mental model
2. **Flexibility**: Natural progression from simple to complex
3. **Maintainability**: Half the code compared to dual-language approach
4. **LLM-Optimized**: Consistent syntax, no language selection decision
5. **Secure**: Rust-based implementation with strong sandboxing
6. **Performant**: ~1ms for simple expressions, suitable for automation

## Next Steps

1. Implement Starlark engine with basic context APIs
2. Add to event listener system for filtering and actions
3. Create script storage and management
4. Build web UI for script editing and testing
5. Develop comprehensive script library/templates

## Resources

- [Starlark Language Specification](https://github.com/bazelbuild/starlark/blob/master/spec.md)
- [starlark-pyo3 Documentation](https://documen.tician.de/starlark-pyo3/)
- [Implementation Example](./starlark-integration-example.md)
- [Detailed Proposal](./scripting-language-proposal-v2.md)