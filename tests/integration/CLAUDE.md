# Integration Testing Guide

This file provides guidance for working with integration tests in this project.

Integration tests use two approaches: VCR.py (and, for Gemini, the SDK's own replay mechanism) to
record and replay HTTP interactions, and real services (Home Assistant, Radicale CalDAV) spun up in
record mode to capture those interactions in the first place.

## Record Modes

LLM integration tests use the `LLM_RECORD_MODE` environment variable, a unified interface across all
providers. Other HTTP integration tests (e.g. Home Assistant) use VCR.py's own modes via
`VCR_RECORD_MODE` or the `--vcr-record` flag.

- `replay` (default, VCR `none`): replay only — a missing recording fails the test. Safe for CI.
- `auto` (VCR `once`): record if missing, else replay. The usual development mode.
- `record` (VCR `all`): force re-record everything; requires valid API keys.

`VCR_RECORD_MODE` additionally accepts VCR's `new_episodes` (replay existing, record anything new).
`LLM_RECORD_MODE` is validated in `tests/conftest.py` and rejects any other value.

## Recording Cassettes

### LLM Integration Tests

LLM integration tests are excluded from normal runs — `pytest.ini` sets
`addopts = -n 4 -m "not gemini_live and not llm_integration"`, so you must select them explicitly
with `-m llm_integration`.

```bash
export OPENAI_API_KEY="..." GEMINI_API_KEY="..."

# Record anything missing, replay the rest
LLM_RECORD_MODE=auto pytest tests/integration/llm/ -xq -m llm_integration

# Force re-record (e.g. after a provider API change)
LLM_RECORD_MODE=record pytest tests/integration/llm/test_tool_calling.py::test_parallel_tool_calls \
  -xq -m llm_integration
```

Providers use different mechanisms under the hood, so recordings land in different places:

- **OpenAI/Anthropic and other HTTP providers**: VCR.py YAML cassettes in `tests/cassettes/llm/`.
- **Google Gemini**: the SDK's `DebugConfig` replay (JSON, with native streaming support) in
  `tests/cassettes/gemini/`, keyed by `<module>/<test name>/mldev` — see the `llm_replay_config`
  fixture in `tests/conftest.py`, which selects the mechanism from the test's `provider` parameter.
- **Google's Interactions API** (Deep Research and Antigravity agents): VCR, not the `DebugConfig`
  replay. These endpoints are served by a separate `_gaos` client inside the SDK, which the replay
  layer wrapping `models.generate_content` does not intercept; VCR sits at the HTTP transport and
  catches both. Such a test takes `@pytest.mark.vcr` and no `provider` parameter, so
  `llm_replay_config` leaves it to VCR — see
  `tests/integration/llm/test_google_antigravity_integration.py`.

### Home Assistant Integration Tests

HA tests record against a real Home Assistant instance, which the fixture starts for you:

```bash
VCR_RECORD_MODE=once pytest tests/integration/home_assistant/ -xvs
```

HA cassettes are written to `tests/cassettes/llm/` too (the HA `vcr_config` shares that
`cassette_library_dir`), one per database backend — e.g.
`test_history_tool_with_entities[postgres].yaml` and `...[sqlite].yaml`. Cassettes capture both the
state data and the API responses, so each backend needs its own. They are checked into git so CI can
replay them.

## VCR Matching

The LLM `vcr_config` fixture in `tests/conftest.py` matches on:

```python
"match_on": ["method", "scheme", "host", "port", "path", "query", "llm_body"]
```

`llm_body` is a custom matcher (`tests/integration/llm/vcr_helpers.py`, registered via
`pytest_recording_configure`) that compares *normalized* request bodies, so key ordering,
whitespace, and dynamic values don't break matching. Playback repeats are disabled to keep
multi-turn interactions aligned.

The Home Assistant `vcr_config` in `tests/integration/home_assistant/conftest.py` differs: it
matches on `["method", "scheme", "host", "path", "query"]`. Port is excluded because HA tests bind
random ports for parallel isolation, and body is excluded because URI normalization changes body
serialization. It also allows playback repeats.

**Normalization and filtering.** HA history-API timestamps are rewritten before recording
(`/api/history/period/2025-10-30T01:55:32+00:00` → `/api/history/period/{START_TIME}`, and the
`start_time` query parameter likewise) so cassettes match regardless of when the test runs; the same
hook drops fixture-setup requests and transient 404s. Both configs filter the `authorization`,
`x-api-key`, `api-key`, `x-goog-api-key`, and `openai-api-key` headers plus the `api_key` and `key`
query parameters, and neither records on exception.

## VCR Compatibility Patches

VCR.py 5.x defines `MockClientResponse.content` as a read-only property, but aiohttp 3.12+ and
`homeassistant_api` assign to it, giving
`AttributeError: "property 'content' of 'MockClientResponse' object has no setter"`. A
session-scoped autouse fixture in `tests/integration/home_assistant/vcr_patches.py` swaps in a
property with a no-op setter (VCR reconstructs content from `_body` anyway) and restores the
original at teardown.

`tests/integration/llm/streaming_mocks.py` carries a related workaround: VCR's MockStream lacks
`readany()`, which aiohttp 3.12+ requires, so streaming tests use a custom stream reader. Tests that
can't be made to work under VCR at all can be marked `@pytest.mark.no_vcr`, which the
`vcr_bypass_for_streaming` autouse fixture honours.

## Home Assistant Fixture

`home_assistant_service` (session scope, `tests/integration/home_assistant/conftest.py`) branches on
the record mode. In replay mode (`none`) it skips startup entirely and yields
`("http://localhost", None)` — VCR matches without the port, and no token is needed. In any record
mode it creates a temp config directory from
`tests/integration/fixtures/home_assistant/configuration.yaml`, validates it with
`hass --script check_config`, starts the `hass` subprocess on a free port, polls `/api/` until ready
(60s timeout), completes onboarding, generates a long-lived access token, and yields
`(base_url, token)`; teardown terminates the process and removes the temp directory.

## Troubleshooting

**"Cassette not found"** — the test made a request with no recording to replay. Record it
(`VCR_RECORD_MODE=once` / `LLM_RECORD_MODE=auto`) or, if the recording genuinely can't be made,
`@pytest.mark.skipif` on the cassette path.

**"Request not matched"** — the outgoing request differs from everything recorded. Run with
`--log-cli-level=DEBUG` to see the request VCR built, compare against the cassette YAML, and
re-record with `record`/`all` if the request legitimately changed. Common causes: a timestamp that
isn't being normalized, query parameter order or encoding, and (for LLM tests) a body change the
`llm_body` matcher doesn't normalize away.

**HA connection refused / timeout waiting for entity** — HA didn't start or was too slow. The
fixture dumps the last 50 log lines on timeout; the full log is at
`/tmp/ha_test_*/config/home-assistant.log`. Check `hass --version` is on PATH, and raise the
deadline in `conftest.py` on a slow machine.

**401 when recording** — the API key is missing or expired. Confirm `OPENAI_API_KEY` /
`GEMINI_API_KEY` are exported, and narrow the recording to a single test rather than re-recording
the whole suite.

## File Organization

Cassettes live under `tests/cassettes/` (not inside `tests/integration/`): `llm/` for VCR YAML,
`gemini/` for SDK replays. The shared VCR fixtures (`llm_record_mode`, `vcr_config`,
`llm_replay_config`) are in the top-level `tests/conftest.py`.

## See Also

- **[tests/CLAUDE.md](../CLAUDE.md)** - General testing patterns and fixtures
- **[tests/functional/web/CLAUDE.md](../functional/web/CLAUDE.md)** - Playwright web UI testing
- **[tests/functional/telegram/CLAUDE.md](../functional/telegram/CLAUDE.md)** - Telegram bot testing
- **[src/family_assistant/tools/CLAUDE.md](../../src/family_assistant/tools/CLAUDE.md)** - Tool
  development and testing
