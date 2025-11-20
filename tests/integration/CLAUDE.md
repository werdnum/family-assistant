# Integration Testing Guide

This file provides guidance for working with integration tests in this project.

## Overview

Integration tests verify that multiple components work together correctly. This project uses two
main approaches:

1. **VCR.py for HTTP Interactions** - Records and replays HTTP requests/responses for fast,
   reproducible tests
2. **Real Services** - Spins up actual services (Home Assistant, Radicale CalDAV) in record mode for
   thorough testing

## VCR.py: Record/Replay of HTTP Interactions

### What is VCR.py?

VCR.py is a library that records HTTP interactions made by your code and replays them in future test
runs. When you make an HTTP request, VCR:

1. **Record Mode**: Intercepts the request, sends it to the real API, records the response in a
   cassette file (YAML)
2. **Replay Mode**: Intercepts the request, matches it against previously recorded requests, and
   returns the recorded response

### Why Use VCR.py?

- **Speed**: No external API calls needed - tests run in seconds instead of seconds-to-minutes
- **Reliability**: Tests don't depend on external service availability or network conditions
- **Cost**: No API rate limiting or usage costs during testing
- **Reproducibility**: Same inputs always produce same outputs - tests are deterministic
- **CI-Friendly**: Tests can run offline or in CI without API keys

### Record Modes

For LLM integration tests, use the `LLM_RECORD_MODE` environment variable to control recording and
replay behavior. This provides a unified interface across all LLM providers (OpenAI, Gemini, etc.).

For other HTTP-based integration tests (e.g., Home Assistant), you can still use the
`VCR_RECORD_MODE` environment variable or the `--vcr-record` command-line flag for direct VCR.py
control.

#### LLM Integration Test Modes

**`replay` (Default)**

```bash
LLM_RECORD_MODE=replay pytest tests/integration/llm/
```

- **Behavior**: Only use existing recordings - does NOT make API calls
- **What happens if recording is missing**: Test fails with recording not found error
- **When to use**: Normal development and CI - use pre-recorded interactions
- **Best for**: Fast, reliable test runs that don't require API keys
- **Equivalent to**: VCR's `none` mode

**`auto`**

```bash
LLM_RECORD_MODE=auto pytest tests/integration/llm/
```

- **Behavior**: Record if missing, else replay
- **What happens**: Creates new recordings for any interactions not yet captured, replays all others
- **When to use**: Development workflow - automatically records new tests while replaying existing
  ones
- **Best for**: Incremental test development without manually switching modes
- **Equivalent to**: VCR's `once` mode

**`record`**

```bash
LLM_RECORD_MODE=record pytest tests/integration/llm/
```

- **Behavior**: Force re-record everything, overwriting existing recordings
- **What happens**: Makes real API calls for every request, even if recordings exist
- **When to use**: When APIs have changed, responses need updating, or verifying current behavior
- **Best for**: Refreshing all recordings after provider changes
- **Warning**: Requires valid API keys in environment variables
- **Equivalent to**: VCR's `all` mode

#### VCR Record Modes (Non-LLM Tests)

For other integration tests using VCR.py directly:

**`none` (Default)**

```bash
VCR_RECORD_MODE=none pytest tests/integration/home_assistant/
```

- **Behavior**: Replay only - does NOT record new interactions
- **What happens if cassette is missing**: Test fails with cassette not found error
- **When to use**: Normal development and CI - use pre-recorded cassettes
- **Best for**: Fast test runs once cassettes are recorded

**`once`**

```bash
VCR_RECORD_MODE=once pytest tests/integration/home_assistant/
```

- **Behavior**: Record missing interactions, replay existing ones
- **What happens**: Creates new cassettes for any requests not yet recorded, replays all others
- **When to use**: First time recording cassettes, or when adding new test cases
- **Best for**: Initial cassette creation with real service access

**`all`**

```bash
VCR_RECORD_MODE=all pytest tests/integration/home_assistant/
```

- **Behavior**: Re-record all interactions, overwriting existing cassettes
- **What happens**: Makes real HTTP calls for every request, even if cassettes exist
- **When to use**: When APIs have changed or responses need updating
- **Best for**: Updating cassettes after API changes
- **Warning**: Requires valid API keys/service access

**`new_episodes`**

```bash
VCR_RECORD_MODE=new_episodes pytest tests/integration/home_assistant/
```

- **Behavior**: Record new interactions, keep existing ones
- **What happens**: Replays existing cassettes, records any new requests not yet seen
- **When to use**: Extending tests with additional API calls
- **Best for**: Adding new functionality while preserving existing cassettes

### Recording Cassettes

#### For LLM Integration Tests

LLM tests use a **unified record/replay system** that works across all providers (OpenAI, Google
Gemini, etc.) via the `LLM_RECORD_MODE` environment variable:

```bash
# Record missing LLM interactions (auto mode)
export OPENAI_API_KEY="your-openai-key"
export GEMINI_API_KEY="your-gemini-key"
LLM_RECORD_MODE=auto pytest tests/integration/llm/ -xvs

# Re-record all LLM interactions (when APIs change)
LLM_RECORD_MODE=record pytest tests/integration/llm/ -xvs

# Run with existing recordings only (default - safe for CI)
LLM_RECORD_MODE=replay pytest tests/integration/llm/ -xvs

# Verify recordings are created
ls tests/cassettes/llm/          # OpenAI (VCR.py cassettes)
ls tests/cassettes/gemini/       # Gemini (SDK replay files)
```

**LLM_RECORD_MODE values:**

- `replay` (default): Only use existing recordings - no API calls
- `auto`: Record if missing, else replay - convenient for development
- `record`: Force re-record everything - requires API keys

**Note**: Different providers use different mechanisms under the hood:

- OpenAI uses VCR.py (HTTP-level YAML cassettes)
- Google Gemini uses SDK's DebugConfig (JSON replay files with native streaming support)

#### For Home Assistant Integration Tests

Home Assistant tests run a real HA instance in record mode to capture authentic interactions:

```bash
# Start recording HA cassettes (requires Home Assistant CLI)
VCR_RECORD_MODE=once pytest tests/integration/home_assistant/ -xvs

# This automatically:
# 1. Starts a real Home Assistant instance
# 2. Completes onboarding and generates access token
# 3. Records all API interactions to cassette files
# 4. Cleans up the HA instance after tests

# Verify cassettes are recorded
ls tests/cassettes/home_assistant/test_*[postgres,sqlite].yaml
```

### Cassette Files

Cassettes are stored as YAML files that record HTTP interactions:

```yaml
interactions:
- request:
    body: '{"model": "gpt-4", "messages": [...]}'
    headers:
      Accept: ["*/*"]
      Authorization: ["Bearer REDACTED"]
    method: POST
    uri: https://api.openai.com/v1/chat/completions
  response:
    body:
      string: '{"id": "chatcmpl-...", "choices": [...]}'
    headers:
      content-type: ["application/json"]
    status:
      code: 200
      message: OK
- request:
    ...
```

**Key Points**:

- **Location**: `tests/cassettes/llm/` for LLM tests, auto-organized by test names
- **Sensitive Data**: API keys are automatically redacted before recording
- **Timestamps**: Normalized to placeholders to avoid cassette mismatches
- **Reproducibility**: Re-running tests with same cassettes produces identical results

### Customizing VCR Matching

VCR matches incoming requests to recorded ones. The matching logic is configured in
`tests/conftest.py`:

```python
"match_on": ["method", "scheme", "host", "path", "query"]
```

This matches requests based on:

- `method`: HTTP method (GET, POST, etc.)
- `scheme`: Protocol (http, https)
- `host`: Domain name
- `path`: URL path
- `query`: Query string parameters

**Note**: Body and port are excluded because:

- **Body**: Dynamic values in request bodies can change between runs
- **Port**: Home Assistant tests use random ports for test isolation

### Request Normalization

To handle dynamic values that change between test runs, VCR normalization filters are applied:

**Timestamps** (in `tests/conftest.py`):

```python
# Before recording, timestamps in Home Assistant history API paths are normalized:
/api/history/period/2025-10-30T01:55:32+00:00  →  /api/history/period/{START_TIME}
```

This ensures cassettes match regardless of what time the test runs.

**Sensitive Data** (automatically filtered):

- Authorization headers
- API keys in headers and query parameters
- These are replaced with `REDACTED` in cassettes

## VCR Compatibility Issues

### Issue #927: MockClientResponse.content Property

VCR.py version 5.x defines `content` as a read-only property without a setter. However, aiohttp
3.12+ and some libraries (like `homeassistant_api`) attempt to assign to `response.content` during
request processing.

**Error**: `AttributeError: "property 'content' of 'MockClientResponse' object has no setter"`

**Our Solution**: VCR compatibility patch in `tests/integration/home_assistant/vcr_patches.py`

```python
# Monkey-patch VCR's MockClientResponse to add a content setter
@pytest.fixture(scope="session", autouse=True)
def patch_vcr_mock_client_response():
    """Patch MockClientResponse.content to add setter for aiohttp 3.12+ compatibility."""
    original_property = aiohttp_stubs.MockClientResponse.content

    def content_getter(self):
        """Delegate to original getter."""
        return original_property.fget(self)

    def content_setter(self, value):
        """Allow setting for compatibility, but ignore the value.

        VCR manages content internally via _body and reconstructs it on access.
        We don't need to actually store the assigned value.
        """
        pass

    # Replace property with one that has both getter and setter
    aiohttp_stubs.MockClientResponse.content = property(
        fget=content_getter,
        fset=content_setter,
    )

    yield

    # Restore original property after tests
    aiohttp_stubs.MockClientResponse.content = original_property
```

**How It Works**:

1. The fixture runs once per test session (autouse=True)
2. Stores the original property from VCR's MockClientResponse
3. Creates a new property with both getter (delegates to original) and setter (silently accepts)
4. Replaces the class property with the new one
5. After tests, restores the original property

**Impact**: Home Assistant tests now work with aiohttp 3.12+ and modern VCR.py

### Similar Pattern in LLM Streaming Tests

See `tests/integration/llm/streaming_mocks.py` for a similar workaround. When VCR.py's MockStream
doesn't support required methods, we provide custom mock implementations:

```python
class MockStreamReader:
    """Mock aiohttp StreamReader with proper streaming support."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.index = 0

    async def readany(self) -> bytes:
        """Read any available data - required by aiohttp 3.12+"""
        if self.index >= len(self.chunks):
            return b""
        chunk = self.chunks[self.index]
        self.index += 1
        await asyncio.sleep(0.001)  # Simulate streaming delay
        return chunk
```

## Home Assistant Integration Tests

### How the Fixture Works

The `home_assistant_service` fixture in `tests/integration/home_assistant/conftest.py` intelligently
manages Home Assistant based on VCR record mode:

**In Replay Mode** (`VCR_RECORD_MODE=none`):

```python
if record_mode == "none":
    # Skip starting real HA instance
    yield ("http://localhost", None)
    return
```

- Doesn't start Home Assistant subprocess
- Returns placeholder URL (VCR matches URI, not host/port)
- Cassettes are replayed - no real API calls needed
- Tests run in seconds

**In Record Mode** (any other mode):

```python
# Start real Home Assistant subprocess
process = subprocess.Popen(
    ["hass", "-c", str(config_dir), "--log-file", str(log_file_path)],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
```

1. Creates temporary config directory with Home Assistant configuration
2. Validates configuration with `hass --script check_config`
3. Starts Home Assistant subprocess
4. Polls `/api/` endpoint until ready (60s timeout)
5. Completes onboarding by creating test user
6. Generates long-lived access token
7. Waits for integrations to load
8. Yields base URL and access token to tests
9. Cleans up: terminates process and removes temp directory

### Test Cassette Organization

Cassettes are organized by database backend:

```
tests/cassettes/llm/
├── test_history_tool_with_entities[postgres].yaml      # PostgreSQL version
├── test_history_tool_with_entities[sqlite].yaml        # SQLite version
├── test_camera_snapshot_tool_get_snapshot[postgres].yaml
├── test_camera_snapshot_tool_get_snapshot[sqlite].yaml
├── test_render_template_tool[postgres].yaml
└── test_render_template_tool[sqlite].yaml
```

Each test records cassettes for both databases because:

- SQLite and PostgreSQL may behave differently (though usually identical)
- Cassettes capture both the state data and API responses
- Having separate cassettes ensures accurate replay for each backend

### Running Home Assistant Tests

```bash
# Replay HA tests with recorded cassettes (default)
pytest tests/integration/home_assistant/ -xvs

# Record new cassettes (requires Home Assistant CLI installed)
VCR_RECORD_MODE=once pytest tests/integration/home_assistant/ -xvs

# Re-record all cassettes (when testing new HA features)
VCR_RECORD_MODE=all pytest tests/integration/home_assistant/ -xvs
```

### Best Practices for HA Tests

1. **Use Fixture Setup Properly**: Home Assistant fixture is session-scoped when recording,
   test-scoped when replaying
2. **Normalize Dynamic Data**: Timestamps in API paths are automatically normalized
3. **Keep Tests Isolated**: Each test should be independent and work with cassettes
4. **Check Cassettes Into Git**: Cassettes are version controlled for reproducible CI
5. **Update Cassettes When Needed**: When Home Assistant or your integration changes, re-record with
   `VCR_RECORD_MODE=all`

## Troubleshooting

### "Cassette Not Found" Error

**Error**: `AssertionError: cassette not found at tests/cassettes/llm/...`

**Cause**: Test made HTTP requests but no cassette exists to replay

**Solution**:

```bash
# Option 1: Record missing cassettes
VCR_RECORD_MODE=once pytest tests/integration/path/to/test.py -xvs

# Option 2: Skip the test if cassette doesn't exist
@pytest.mark.skipif(not os.path.exists("tests/cassettes/llm/my_cassette.yaml"),
                     reason="Cassette not recorded")
async def test_something():
    pass
```

### "Request Not Matched" Error

**Error**: `VCR.py couldn't match request to recorded request`

**Cause**: Request being made doesn't match any recorded request (different query params, body,
etc.)

**Debug Steps**:

1. **Check what request was made**:

   ```bash
   # Run test with VCR logging enabled
   pytest tests/integration/path/to/test.py -xvs --log-cli-level=DEBUG
   ```

2. **Compare to cassette**:

   ```bash
   # View cassette file
   cat tests/cassettes/llm/your_cassette.yaml

   # Look for request matching your parameters
   ```

3. **Update cassette if request should change**:

   ```bash
   VCR_RECORD_MODE=all pytest tests/integration/path/to/test.py::test_name -xvs
   ```

### VCR Matching Issues

If tests fail with "request not matched" even though cassettes exist:

1. **Check request normalization** - Timestamps may not be normalized correctly
2. **Verify query parameters** - Order and encoding matter for matching
3. **Check headers** - Some headers might be blocking matches
4. **Use custom matchers** - For complex scenarios like LLM tests, custom matchers handle
   normalization

Example from LLM tests (`tests/integration/llm/vcr_helpers.py`):

```python
def llm_request_matcher(r1, r2) -> bool:
    """Custom matcher for LLM API requests."""
    # Compare normalized bodies instead of exact bodies
    # Handles key ordering, whitespace, dynamic values
    norm1 = normalize_llm_request_body(json.loads(r1.body))
    norm2 = normalize_llm_request_body(json.loads(r2.body))
    return norm1 == norm2
```

### Home Assistant Connection Errors

**Error**: `Connection refused` or `Timeout waiting for entity`

**Cause**: Home Assistant instance didn't start or was too slow to initialize

**Debug Steps**:

1. **Check Home Assistant logs**:

   ```bash
   # Fixture dumps last 50 lines of logs on timeout
   # Look for error messages about startup
   tail -50 /tmp/ha_test_*/config/home-assistant.log
   ```

2. **Increase timeout** (if system is slow):

   ```python
   # In conftest.py, increase deadline
   deadline = time.time() + 120  # 120 seconds instead of 60
   ```

3. **Check Home Assistant CLI is installed**:

   ```bash
   which hass
   hass --version
   ```

4. **Verify fixture config is valid**:

   ```bash
   # Check fixture config file exists
   cat tests/fixtures/home_assistant/configuration.yaml
   ```

### API Key Issues

**Error**: `Unauthorized` (401) when recording cassettes

**Cause**: API key is invalid or expired

**Solutions**:

1. **Verify API key is set**:

   ```bash
   echo $OPENAI_API_KEY
   echo $GEMINI_API_KEY
   ```

2. **Use valid test/sandbox keys**:

   ```bash
   # Use test API keys for recording if available
   LLM_RECORD_MODE=auto OPENAI_API_KEY="sk-test-..." pytest tests/integration/llm/
   ```

3. **Limit what you record** - Only record specific tests:

   ```bash
   LLM_RECORD_MODE=auto pytest tests/integration/llm/test_providers.py::test_basic_completion -xvs
   ```

## Advanced Usage

### Custom Request Matchers

For complex HTTP interactions, use custom matchers to handle non-trivial variations:

```python
@pytest.mark.vcr(match_on=['method', 'scheme', 'host', 'path'])
async def test_with_custom_matching():
    """Custom matcher ignores query parameter order and body variations."""
    pass
```

### Cassette Introspection

Inspect cassettes to understand what was recorded:

```python
import vcr

# Load cassette directly
with vcr.VCR().use_cassette('tests/cassettes/llm/test_something.yaml') as cassette:
    print(f"Number of interactions: {len(cassette)}")
    for i, interaction in enumerate(cassette):
        print(f"Interaction {i}:")
        print(f"  Request: {interaction.request.method} {interaction.request.uri}")
        print(f"  Response: {interaction.response['status']['code']}")
```

### Debugging HTTP Interactions

Enable detailed logging to see what VCR is doing:

```bash
pytest tests/integration/ -xvs \
  --log-cli-level=DEBUG \
  --capture=no 2>&1 | grep -A 5 "VCR\|cassette\|request"
```

## Integration Test File Organization

```
tests/integration/
├── conftest.py                           # VCR configuration, shared fixtures
├── CLAUDE.md                             # This file
├── fixtures/
│   └── home_assistant/
│       └── configuration.yaml            # HA fixture configuration
├── llm/
│   ├── conftest.py                       # LLM-specific fixtures
│   ├── vcr_helpers.py                    # Custom request matching, sanitization
│   ├── streaming_mocks.py                # Custom streaming mocks
│   ├── test_providers.py                 # Basic LLM provider tests
│   ├── test_streaming.py                 # Streaming response tests
│   ├── test_tool_calling.py              # LLM tool call tests
│   └── test_thought_signatures.py        # Extended thinking tests
├── home_assistant/
│   ├── conftest.py                       # HA fixture and polling logic
│   ├── vcr_patches.py                    # VCR compatibility fixes
│   ├── test_tools.py                     # HA tool tests
│   └── test_wrapper.py                   # HA API wrapper tests
└── cassettes/
    └── llm/
        ├── test_basic_completion[postgres].yaml
        ├── test_basic_completion[sqlite].yaml
        └── ... more cassettes
```

## See Also

- **[tests/CLAUDE.md](../CLAUDE.md)** - General testing patterns and fixtures
- **[tests/functional/web/CLAUDE.md](../functional/web/CLAUDE.md)** - Playwright web UI testing
- **[tests/functional/telegram/CLAUDE.md](../functional/telegram/CLAUDE.md)** - Telegram bot testing
- **[src/family_assistant/tools/CLAUDE.md](../../src/family_assistant/tools/CLAUDE.md)** - Tool
  development and testing
