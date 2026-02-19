# LLM Integration Testing

## Overview

We use a **unified record/replay system** for testing LLM provider integrations:

- **OpenAI**: Uses [VCR.py](https://vcrpy.readthedocs.io/) for HTTP-level recording
- **Google Gemini**: Uses SDK's built-in DebugConfig for native streaming support

Both mechanisms are controlled via the `LLM_RECORD_MODE` environment variable, providing:

- Testing against real LLM APIs without incurring costs on every test run
- Deterministic and reproducible tests
- CI/CD execution without requiring API keys
- Verification of provider-specific behavior
- Full streaming support for all providers

## Running Tests

### Replay Mode (Default)

By default, tests run in replay mode using previously recorded cassettes:

```bash
# Run all LLM integration tests
pytest tests/integration/llm -m llm_integration

# Run tests for a specific provider
pytest tests/integration/llm -k "openai" -m llm_integration
pytest tests/integration/llm -k "google" -m llm_integration

# Run with verbose output
pytest tests/integration/llm -m llm_integration -v
```

### Recording New Interactions

To record new interactions, you need:

1. Valid API keys for the providers you want to test
2. The `LLM_RECORD_MODE` environment variable

```bash
# Set API keys
export OPENAI_API_KEY=your-openai-key
export GEMINI_API_KEY=your-gemini-key

# Record new interactions (force re-record everything)
LLM_RECORD_MODE=record pytest tests/integration/llm -m llm_integration

# Auto-record missing interactions (recommended for development)
LLM_RECORD_MODE=auto pytest tests/integration/llm -m llm_integration

# Replay only (default - safe for CI)
LLM_RECORD_MODE=replay pytest tests/integration/llm -m llm_integration
```

### LLM_RECORD_MODE Values

- `replay` (default): Only use existing recordings - no API calls, safe for CI
- `auto`: Record if missing, else replay - convenient for development
- `record`: Force re-record everything - requires API keys, overwrites existing

**Implementation Details:**

- OpenAI: Recordings stored as YAML in `tests/cassettes/llm/`
- Gemini: Recordings stored as JSON in `tests/cassettes/gemini/`

## Test Structure

### Basic Provider Tests

Located in `tests/integration/llm/test_providers.py`:

- Basic text completion
- System message handling
- Multi-turn conversations
- Model parameter handling
- Provider-specific features
- Edge cases (empty messages, etc.)

### Tool Calling Tests

Located in `tests/integration/llm/test_tool_calling.py`:

- Single tool calls
- Multiple tool selection
- Parallel tool calls (where supported)
- Tool response handling
- Conversation with tool history

## Adding New Tests

### 1. Create a Test Function

Mark your test with the appropriate decorators:

```python
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize("provider,model", [
    ("openai", "gpt-3.5-turbo"),
    ("google", "gemini-2.0-flash-latest"),
])
async def test_new_feature(provider: str, model: str, llm_client_factory):
    """Test description."""
    # Skip in CI without API keys
    if os.getenv("CI") and not os.getenv(f"{provider.upper()}_API_KEY"):
        pytest.skip(f"Skipping {provider} test in CI without API key")
    
    client = await llm_client_factory(provider, model)
    
    # Test implementation
    response = await client.generate_response(messages)
    assert response.content is not None
```

### 2. Use Meaningful Test Names

Test names determine cassette filenames. Use descriptive names that indicate what's being tested.

### 3. Handle Provider Differences

Some providers may have different behaviors or capabilities:

```python
if provider == "openai":
    # OpenAI-specific test logic
    pass
elif provider == "google":
    # Google-specific test logic
    pass
```

## Cassette Management

### Cassette Location

Cassettes are stored in `tests/cassettes/llm/` with the following structure:

```
tests/cassettes/llm/
├── test_providers/
│   ├── test_basic_completion[openai-gpt-3.5-turbo].yaml
│   ├── test_basic_completion[google-gemini-2.0-flash-latest].yaml
│   └── ...
└── test_tool_calling/
    ├── test_single_tool_call[openai-gpt-3.5-turbo].yaml
    └── ...
```

### Sanitizing Sensitive Data

The `sanitize_response` function in `vcr_helpers.py` automatically removes:

- API keys from headers
- Authentication tokens
- Other sensitive headers

Always review cassettes before committing to ensure no sensitive data is included.

### Validating Cassettes

To validate all cassettes for corruption or sensitive data:

```bash
python scripts/validate_cassettes.py
```

## Cost Considerations

### Recommended Models for Testing

Use the most affordable models for testing:

- **OpenAI**: `gpt-4.1-nano` (cheapest option)
- **Google**: `gemini-3-flash-preview-preview-06-17` (most cost-effective)
- **Anthropic**: `claude-3-haiku` (when implemented)

### Minimizing Costs

1. Keep test prompts short and focused
2. Avoid unnecessary model calls in tests
3. Use parameterization efficiently
4. Record cassettes locally, not in CI
5. Regularly review and clean up unused cassettes

## CI/CD Integration

### GitHub Actions Configuration

The CI pipeline runs tests in replay-only mode:

```yaml
- name: Run LLM Integration Tests
  env:
    LLM_RECORD_MODE: replay
  run: |
    pytest tests/integration/llm -m llm_integration
```

This ensures:

- No API calls are made in CI
- Tests fail if recordings are missing
- No accidental costs from CI runs

### Refreshing Recordings

Recordings should be refreshed periodically (e.g., monthly) to ensure they reflect current API
behavior:

1. Set up API keys locally
2. Run tests with `LLM_RECORD_MODE=record`
3. Review changes in recording files
4. Commit updated recordings

## Troubleshooting

### Missing Recordings

If you see errors about missing recordings:

1. Check if you're running a new test that hasn't been recorded
2. Record the interaction locally with proper API keys using `LLM_RECORD_MODE=auto`
3. Commit the new recording file

### API Changes

If providers change their API format:

1. Re-record affected interactions with `LLM_RECORD_MODE=record`
2. Update tests if necessary to handle new formats
3. Document any breaking changes

### Authentication Errors

During recording, ensure:

1. API keys are properly set in environment (e.g., `OPENAI_API_KEY`, `GEMINI_API_KEY`)
2. Keys have necessary permissions
3. Keys haven't expired or been revoked

### Recording Playback Errors

If recordings fail to replay:

1. Check `LLM_RECORD_MODE` isn't set to `record` (which forces re-recording)
2. Verify recording file isn't corrupted
3. Ensure request matching is working correctly
4. For OpenAI tests: Check VCR cassette files in `tests/cassettes/llm/`
5. For Gemini tests: Check replay files in `tests/cassettes/gemini/`

## Best Practices

1. **Keep Tests Focused**: Each test should verify one specific behavior
2. **Use Deterministic Prompts**: Avoid prompts that might generate variable responses
3. **Handle Provider Variations**: Account for different response formats between providers
4. **Document Provider-Specific Behavior**: Note any differences in comments
5. **Review Cassettes**: Always review cassette files before committing
6. **Test Error Cases**: Include tests for error conditions and edge cases
7. **Minimize Token Usage**: Keep prompts concise to reduce costs during recording

## Future Improvements

1. **Anthropic Support**: Add tests when Anthropic provider is implemented
2. **Streaming Tests**: Add tests for streaming responses when implemented
3. **Advanced Features**: Test provider-specific features (e.g., function calling variations)
4. **Performance Benchmarks**: Track response times and token usage
5. **Automated Cassette Refresh**: Set up scheduled cassette updates
