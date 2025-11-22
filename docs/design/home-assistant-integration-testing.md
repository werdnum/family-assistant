# Home Assistant Integration Testing Design

## Motivation

The Home Assistant integration is a critical component of Family Assistant. Previously, we only had
unit tests that mocked the Home Assistant API responses, which led to:

1. **Missed Integration Issues**: The refactor from custom dataclasses to library models introduced
   an N+1 query problem that unit tests didn't catch
2. **API Contract Drift**: Changes in the Home Assistant API or the `homeassistant_api` library
   could silently break our integration
3. **False Confidence**: Mocked tests passed even when real API interactions would fail

Integration tests against a real Home Assistant instance provide:

- **Early Detection**: Catch performance issues like N+1 queries
- **API Contract Validation**: Ensure compatibility with actual HA responses
- **Regression Prevention**: Detect breaking changes in dependencies

## Architecture Overview

### Test Modes

The integration testing system supports three modes:

1. **Record Mode** (`--record-mode=once` or `--record-mode=all`):

   - Starts a real Home Assistant subprocess
   - Executes tests against the live instance
   - Records all HTTP interactions as VCR cassettes The integration testing system supports three
     modes:

2. **Record Mode** (`--vcr-record=once` or `--vcr-record=all`):

   - Starts a real Home Assistant subprocess
   - Executes tests against the live instance
   - Records all HTTP interactions as VCR cassettes
   - **When to use**: Creating new tests or updating existing ones

3. **Replay Mode** (`--vcr-record=none`):

   - Skips starting Home Assistant entirely
   - Replays recorded HTTP interactions from cassettes
   - Fast, deterministic, no external dependencies
   - **When to use**: CI/CD pipelines, normal development

4. **Rewrite Mode** (`--vcr-record=all`):

   - Like record mode but overwrites existing cassettes
   - **When to use**: Updating all tests after API changes │ ├── conftest.py # HA subprocess fixture
     │ ├── home_assistant/ │ │ └── test_history.py # HA history integration tests │ └── fixtures/ │
     └── home_assistant/ │ └── configuration.yaml # Minimal HA config └── cassettes/ └──
     home_assistant/ └── test_history\_\*.yaml # Recorded HTTP interactions

````

## Implementation Details

### 1. Home Assistant Subprocess Fixture

Location: `tests/integration/conftest.py`

The `home_assistant_service` fixture:

1. **Checks VCR Record Mode**:

   ```python
   record_mode = request.config.getoption("--record-mode")
   if record_mode == "none":
       # Replay mode - skip startup
       yield "http://localhost:8123"
       return
````

2. **Starts HA Subprocess** (if needed):

   ```python
   # Create temp directory for HA data
   config_dir = temp_dir / "config"
   # Copy minimal configuration.yaml
   # Start: hass -c {config_dir}
   # Poll /api/ until ready (timeout 60s)
   ```

3. **Yields Base URL**: `http://localhost:8123`

4. **Teardown**: Terminates subprocess, cleans up temp files

### 2. Minimal Home Assistant Configuration

Location: `tests/integration/fixtures/home_assistant/configuration.yaml`

```yaml
homeassistant:
  # Disable auth for tests (ONLY in isolated test environment)
  auth_providers:
    - type: trusted_networks
      trusted_networks:
        - 127.0.0.1
        - ::1
      allow_bypass_login: true

# Enable required APIs
api:
websocket_api:

# Recorder for history functionality
recorder:
  db_url: sqlite:///:memory:  # In-memory for speed

# Test entities
input_boolean:
  test_switch:
    name: Test Switch
    initial: off

input_text:
  test_sensor:
    name: Test Sensor
    initial: "test_value"
```

**Key Configuration Choices**:

- **No Authentication**: `trusted_networks` with `allow_bypass_login` eliminates auth complexity
- **In-Memory Database**: Fastest startup, no cleanup needed
- **Minimal Components**: Only `api`, `websocket_api`, and `recorder` for history
- **Predictable Entities**: `input_boolean` and `input_text` have deterministic behavior

### 3. Integration Tests

Location: `tests/integration/home_assistant/test_history.py`

Tests cover:

1. **Basic Connectivity**: Verify HA starts and API is accessible
2. **Entity States**: Test querying entity states
3. **History with Entity IDs**: **Critical test** - catches N+1 queries
4. **History without Filtering**: Test bulk history download
5. **Error Handling**: Test behavior with invalid entities

All tests use:

```python
@pytest.mark.integration
@pytest.mark.vcr
async def test_name(home_assistant_service):
    # Test implementation
```

### 4. VCR Cassette Management

**Cassette Storage**: `tests/cassettes/home_assistant/`

**Naming Convention**: `test_name[provider-model].yaml` (follows existing pattern)

**Sanitization**: May need to sanitize timestamps or UUIDs for deterministic replay

## Workflow

### Recording New Tests

**Prerequisites**: Home Assistant is installed as a dev dependency. Ensure dev dependencies are
installed:

```bash
# Install all dependencies including HA
uv sync --extra dev

# Verify installation
which hass
hass --version  # Should show 2025.10.4 or later
```

**Recording**:

```bash
# Record cassettes
pytest tests/integration/home_assistant/ --record-mode=once

# Commit cassettes
git add tests/cassettes/home_assistant/
git commit -m "Add HA integration test cassettes"
```

**Note**: Recording cassettes is only needed when creating new tests or updating after API changes.
Normal development and CI use replay mode with pre-recorded cassettes.

### Running in CI

```yaml
# .github/workflows/test.yml
- name: Run HA Integration Tests
  run: pytest tests/integration/home_assistant/ --record-mode=none
```

**No Home Assistant installation needed** - tests replay from cassettes.

### Updating After API Changes

```bash
# Re-record all cassettes
pytest tests/integration/home_assistant/ --record-mode=all

# Review changes
git diff tests/cassettes/home_assistant/

# Commit if expected
git add tests/cassettes/home_assistant/
git commit -m "Update HA integration cassettes for API v2024.11"
```

## Benefits

### Development Benefits

1. **Fast Feedback**: Unit tests + replayed integration tests run in \<1 minute
2. **Confidence**: Know the integration actually works with real HA
3. **Documentation**: Cassettes serve as API usage examples

### CI/CD Benefits

1. **No External Dependencies**: CI doesn't need Docker or HA installation
2. **Deterministic**: Cassettes ensure identical behavior across runs
3. **Fast**: Replay is ~100x faster than real HTTP calls

### Maintenance Benefits

1. **API Version Tracking**: Cassettes record which HA version was tested
2. **Change Detection**: Diff cassettes to see API changes
3. **Regression Prevention**: Tests catch breaking changes immediately

## Trade-offs

### Cassette Maintenance

**Cost**: Need to re-record when:

- Home Assistant API changes
- `homeassistant_api` library updates significantly
- Test assertions change

**Mitigation**: Mark tests with expected HA version, automate re-recording

### Subprocess Complexity

**Cost**: Managing subprocess lifecycle is more complex than Docker

**Mitigation**: Well-tested fixture handles all edge cases (timeouts, cleanup, ports)

### Test Flakiness Risk

**Cost**: Real HA subprocess could be slower on different systems

**Mitigation**:

- Use in-memory database for speed
- Generous timeouts (60s for startup)
- Retry logic for polling

## Future Enhancements

1. **Parallel Test Execution**: Use different ports for parallel HA instances
2. **Docker Option**: Add `--use-docker` flag for systems with Docker access
3. **HA Version Matrix**: Test against multiple HA versions
4. **WebSocket Testing**: Extend to test real-time event streams

## References

- [pytest-recording Documentation](https://github.com/kiwicom/pytest-recording)
- [VCR.py Documentation](https://vcrpy.readthedocs.io/)
- [Home Assistant Developer Docs](https://developers.home-assistant.io/)
- [Similar Pattern in Our LLM Tests](../development/llm-integration-testing.md)
