# Comparison of Starlark Python Bindings: starlark-pyo3 vs python-starlark-go

## Overview

Both libraries provide Python bindings for the Starlark configuration language, offering sandboxed execution of Python-like code. However, they differ significantly in their implementation approach, architecture, and characteristics.

## starlark-pyo3

### Architecture

- **Implementation**: Built on Facebook's starlark-rust implementation
- **Binding Technology**: Uses PyO3 (Rust-Python bindings)
- **Language Stack**: Python → Rust
- **Build System**: Uses maturin for building Python wheels

### Installation

```bash
pip install starlark-pyo3

```

- Binary wheels available for major platforms

- No additional runtime dependencies required
- Development requires nightly Rust and maturin

### API Design

```python
import starlark as sl

# Create globals and module
glb = sl.Globals.standard()
mod = sl.Module()
mod["a"] = 5

# Add Python functions
def g(x):
    print(f"g called with {x}")
    return 2 *x

mod.add_callable("g", g)

# Parse and evaluate
ast = sl.parse("script.star", source_code)
val = sl.eval(mod, ast, glb)

```

### Key Features

- Deterministic evaluation
- Hermetic execution (no filesystem, network, or clock access)
- Parallel module loading
- Type conversion via JSON intermediate format
- Comprehensive linting support via AST

### Performance

- Built on Rust for excellent performance
- Parallel evaluation without Python's GIL
- Optimized string operations and data structures
- Garbage collected values

### Security

- Sandboxed environment preventing system access
- Safe for executing untrusted code
- No known vulnerabilities (per Snyk analysis)
- Cannot access filesystem, network, or system clock

### Maintenance Status

- Active development (83 commits as of search)
- 22 stars on GitHub
- 35,305 weekly downloads on PyPI
- Status: "reasonably complete and usable"
- Documentation: https://documen.tician.de/starlark-pyo3/

### Limitations

- Value conversion currently uses JSON as intermediate format
- Limited to types that can be represented in JSON
- Requires Rust toolchain for development builds

## python-starlark-go

### Architecture

- **Implementation**: Built on Google's starlark-go implementation
- **Binding Technology**: Uses CGO (C bindings for Go)
- **Language Stack**: Python → C (via CGO) → Go
- **Build System**: Compiles Go code to shared library

### Installation

```bash
pip install starlark-go

```

- Requires CGO and C compiler at build time

- Pre-built wheels may be available
- Embeds starlark-go version v0.0.0-20230302034142-4b1e35fe2254

### API Design

```python
from starlark_go import Starlark

s = Starlark()

# Execute code
s.exec("""
def fibonacci(n=10):
    res = list(range(n))
    for i in res[2:]:
        res[i] = res[i-2] + res[i-1]
    return res
""")

# Evaluate expressions
result = s.eval("fibonacci(5)")  # [0, 1, 1, 2, 3]

# Set variables
s.set(x=5)
s.eval("x")  # 5

```

### Key Features

- Hermetic execution
- Parallel thread execution without GIL
- Configuration via global settings
- Based on mature Go implementation used in Bazel

### Performance

- Go implementation with parallel thread execution
- No Python GIL limitations
- Good performance for concurrent workloads
- CGO overhead for Python-Go communication

### Security

- Sandboxed execution environment
- Safe for untrusted code
- Inherits security properties from starlark-go

### Maintenance Status

- Based on actively maintained Google starlark-go
- Less active Python binding development
- Originally forked from pystarlark
- Documentation: https://python-starlark-go.readthedocs.io/

### Limitations

- CGO dependency adds complexity
- Requires C compiler for building
- Three-layer architecture (Python → C → Go) adds overhead
- Global configuration affects all interpreters

## Comparison Summary

| Feature | starlark-pyo3 | python-starlark-go |
|---------|---------------|-------------------|
| **Implementation Language**| Rust | Go |
| **Binding Technology**| PyO3 | CGO |
| **Installation Complexity**| Simple (binary wheels) | Moderate (CGO required) |
| **Architecture Layers**| 2 (Python → Rust) | 3 (Python → C → Go) |
| **Performance**| Excellent (Rust) | Good (Go + CGO overhead) |
| **Parallel Execution**| Yes (no GIL) | Yes (no GIL) |
| **API Style**| Module/AST-based | Interpreter object |
| **Value Conversion**| JSON intermediate | Direct via CGO |
| **Development Activity**| Active | Less active |
| **Weekly Downloads**| 35,305 | Not specified |
| **Documentation**| Comprehensive | Basic |
| **Maturity**| "Reasonably complete" | Based on mature Go impl |
| **Build Requirements**| Rust (dev only) | C compiler |
| **Error Handling**| Rust-style | Go-style via CGO |

## Recommendations

### Choose starlark-pyo3 if

- You want the simplest installation experience
- You prefer Rust's performance characteristics
- You need comprehensive documentation
- You want an actively maintained Python binding
- You're comfortable with JSON-based value conversion
- You want better error messages and debugging

### Choose python-starlark-go if

- You're already using Go tooling
- You need specific starlark-go features
- You want the implementation used by Bazel
- You're comfortable with CGO dependencies
- You need direct value passing without JSON conversion

## Integration Complexity

### starlark-pyo3

- **Low complexity**: Binary wheels make installation straightforward
- Clear API with good documentation
- Standard Python exception handling
- Easy to embed in existing Python applications

### python-starlark-go

- **Medium complexity**: CGO requirements add build complexity
- Less comprehensive documentation
- May require understanding of Go/CGO internals for debugging
- More layers of indirection in the architecture

## Conclusion

Both libraries provide secure, sandboxed execution of Starlark code with good performance. starlark-pyo3 offers a more polished Python experience with better documentation and simpler installation, while python-starlark-go provides access to Google's reference implementation with its specific features and behaviors. The choice depends on your specific requirements, existing toolchain, and preference for Rust vs Go ecosystems.
