# HAR Recording and Replay for Playwright Tests

## Problem Statement

Currently, every Playwright test requires:

- Full backend startup (FastAPI server, database, mock LLM)
- Full frontend assets (React app)
- Browser automation

This is slow and makes it hard to isolate changes:

- Frontend changes require full backend even if API didn't change
- Backend changes require browser even if we just need to validate API responses

We already use VCR.py successfully for integration tests against external APIs. We want similar
functionality for Playwright tests.

## Solution Overview

Implement a VCR.py-style HAR recording/replay system with two capabilities:

1. **Frontend-only testing**: Run UI tests without backend by replaying API responses from HAR files
2. **Backend-only testing**: Validate backend API changes by replaying frontend HTTP requests
   without a browser

**Philosophy**: Start simple, add complexity only when necessary based on actual failures.

## Implementation Strategy

### Phase 1: Basic HAR Recording (Start Here)

**Goal**: Get HAR recording working for one simple test

**Steps**:

1. Create `tests/functional/web/cassettes/` directory
2. Add simple fixture that records HAR using Playwright's built-in `page.context.tracing.start()` or
   manual recording
3. Pick simplest test (e.g., `test_notes_list_display` - read-only, no auth, simple data)
4. Record HAR for that test
5. Inspect HAR file to understand structure

**Implementation**:

```python
# tests/functional/web/conftest.py

@pytest.fixture
def record_mode(request) -> str:
    """Get record mode from CLI or default to 'none'."""
    return request.config.getoption("--record-mode", default="none")

@pytest_asyncio.fixture
async def web_test_fixture_with_har(
    page: Page,
    web_only_assistant: Assistant,
    api_socket_and_port: tuple[int, socket.socket],
    build_frontend_assets: None,
    request: pytest.FixtureRequest,
    record_mode: str,
) -> AsyncGenerator[WebTestFixture]:
    """WebTestFixture with HAR recording support."""

    api_port, _ = api_socket_and_port
    base_url = f"http://localhost:{api_port}"

    # Get cassette path
    test_name = request.node.name
    test_file = Path(request.node.fspath).relative_to(Path.cwd())
    cassette_dir = Path("tests/functional/web/cassettes") / test_file.parent.name
    cassette_dir.mkdir(parents=True, exist_ok=True)
    cassette_path = cassette_dir / f"{test_name}.har"

    # Start recording if needed
    if record_mode in ("rewrite", "once"):
        if record_mode == "rewrite" or not cassette_path.exists():
            # Playwright's built-in HAR recording
            await page.context.route_from_har(
                str(cassette_path),
                update=True  # Record mode
            )

    # Navigate and wait for app
    await page.goto(base_url)
    await page.wait_for_selector('[data-app-ready="true"]', timeout=15000)

    yield WebTestFixture(assistant=web_only_assistant, page=page, base_url=base_url)

    # Stop recording happens automatically when context closes
    await page.close()
```

**Add CLI option**:

```python
# tests/functional/web/conftest.py
def pytest_addoption(parser):
    parser.addoption(
        "--record-mode",
        action="store",
        default="none",
        choices=["none", "once", "rewrite"],
        help="HAR recording mode: none (replay only), once (record if missing), rewrite (always record)"
    )
```

**Try it**:

```bash
# Record one test
pytest tests/functional/web/ui/test_notes_ui.py::test_notes_list_display --record-mode=rewrite

# Check the cassette
ls tests/functional/web/cassettes/ui/
cat tests/functional/web/cassettes/ui/test_notes_list_display.har | jq '.log.entries | length'
```

**Success criteria**: HAR file created with recorded HTTP traffic

______________________________________________________________________

### Phase 2: Basic HAR Replay (The Critical Test)

**Goal**: Run test using HAR file WITHOUT starting backend

**Steps**:

1. Modify fixture to detect replay mode
2. Skip backend startup when replaying
3. Configure `page.context.route_from_har()` with `update=False`
4. Run test and see what breaks

**Implementation**:

```python
@pytest_asyncio.fixture
async def web_test_fixture_with_har(
    page: Page,
    # Make web_only_assistant optional
    request: pytest.FixtureRequest,
    record_mode: str,
    build_frontend_assets: None,
) -> AsyncGenerator[WebTestFixture]:
    """WebTestFixture with HAR recording/replay support."""

    # Get cassette path
    test_name = request.node.name
    test_file = Path(request.node.fspath).relative_to(Path.cwd())
    cassette_dir = Path("tests/functional/web/cassettes") / test_file.parent.name
    cassette_path = cassette_dir / f"{test_name}.har"

    # Determine mode
    use_har_replay = os.getenv("USE_HAR_REPLAY") == "1" or record_mode == "none"
    should_record = record_mode in ("rewrite", "once") and (record_mode == "rewrite" or not cassette_path.exists())

    if use_har_replay:
        # REPLAY MODE - No backend needed!
        if not cassette_path.exists():
            raise FileNotFoundError(
                f"HAR cassette not found: {cassette_path}\n"
                f"Run with --record-mode=rewrite to create it."
            )

        # Configure HAR replay
        await page.context.route_from_har(str(cassette_path), update=False)

        # Serve frontend from static location (no backend server)
        # This is the tricky part - we need a simple HTTP server for static files
        # For now, assume backend is running but we're just not hitting it
        base_url = "http://localhost:8000"  # We'll need to solve this

        yield WebTestFixture(assistant=None, page=page, base_url=base_url)

    else:
        # RECORD MODE or NORMAL MODE - Start backend
        # Get actual assistant fixture here
        api_port = ...  # Need to handle this
        await page.context.route_from_har(str(cassette_path), update=should_record)
        # ... rest of normal fixture logic
```

**Run it**:

```bash
USE_HAR_REPLAY=1 pytest tests/functional/web/ui/test_notes_ui.py::test_notes_list_display -v
```

**Expected issues to discover**:

- Dynamic data (IDs, timestamps) might not match
- Authentication tokens might be stale
- Frontend might need backend for static files (need to solve this)

**Success criteria**: Test runs and we see specific failure reasons

______________________________________________________________________

### Phase 3: Iterate on What Breaks

**This is where we learn what sanitization is needed**

After Phase 2, we'll see failures like:

- "Request not found in HAR: /api/v1/notes?timestamp=1234567890"
- "Authentication failed: token expired"
- "ID mismatch: expected abc-123, got def-456"

**For each failure type**, add minimal fix:

**Example: Timestamp mismatches**

```python
# Only add this if we see timestamp issues
def sanitize_har_entry(entry):
    """Remove or normalize dynamic data from HAR entries."""
    # Remove query params we know are dynamic
    if 'timestamp' in entry['request']['url']:
        entry['request']['url'] = re.sub(r'[?&]timestamp=\d+', '', entry['request']['url'])
    return entry
```

**Example: ID mismatches in response**

```python
# Only add if we see ID mismatches causing issues
def normalize_response_ids(response_text):
    """Replace UUIDs with predictable values for matching."""
    # Only normalize if we're having matching issues
    return re.sub(
        r'"id":\s*"[a-f0-9-]+"',
        '"id": "normalized-id"',
        response_text
    )
```

**Strategy**: Fix only what breaks, keep it simple

______________________________________________________________________

### Phase 4: Opt-out Marker Support

**Goal**: Allow tests to opt out of HAR mode

**Implementation**:

```python
@pytest_asyncio.fixture
async def web_test_fixture_with_har(...):
    # Check for marker
    if request.node.get_closest_marker("no_har"):
        # Use original fixture logic, no HAR
        ...

# Usage in tests:
@pytest.mark.no_har
async def test_streaming_sse(web_test_fixture_with_har):
    # This test doesn't use HAR
    pass
```

**Apply marker to**:

- SSE streaming tests (if HAR doesn't handle them well)
- File upload tests (if binary data is problematic)
- Any test that fundamentally can't work with HAR

______________________________________________________________________

### Phase 5: Expand Coverage

**Goal**: Apply to more tests, discover more edge cases

**Strategy**:

1. Start with read-only tests (notes list, events list)
2. Move to tests with mutations (create note, delete note)
3. Try chat tests (most complex)
4. Add `@pytest.mark.no_har` to tests that don't work

**Track**:

- Which tests work with HAR?
- Which need `@pytest.mark.no_har`?
- What patterns cause issues?

**Success criteria**: 60-80% of tests work with HAR replay

______________________________________________________________________

### Phase 6: Backend Replay (Future)

**Goal**: Test backend changes without browser

**Only start this after Phase 5 is working**

**Simple approach**:

1. Parse HAR file to extract requests
2. Replay requests against live backend
3. Compare responses (ignoring dynamic fields we discovered in Phase 3)

**Implementation**:

```python
# tests/functional/web/test_backend_contracts.py

async def test_backend_matches_har_contract(cassette_path: str):
    """Validate backend responses match recorded HAR."""
    har_data = json.loads(Path(cassette_path).read_text())

    async with AsyncClient() as client:
        for entry in har_data['log']['entries']:
            request = entry['request']
            expected_response = entry['response']

            # Skip non-API requests
            if not request['url'].startswith('/api/'):
                continue

            # Replay request
            actual = await client.request(
                method=request['method'],
                url=request['url'],
                headers=dict(request['headers']),
                json=json.loads(request['postData']['text']) if 'postData' in request else None
            )

            # Compare (ignoring dynamic fields we discovered)
            assert actual.status_code == expected_response['status']
            # More comparison logic based on what we learned in Phase 3
```

______________________________________________________________________

## Migration Plan

### Step 1: Start Small

- Pick 3-5 simple read-only tests
- Record HAR cassettes
- Get replay working
- Document issues found

### Step 2: Learn & Iterate

- Based on issues, add minimal sanitization
- Try 10 more tests
- Refine approach

### Step 3: Expand

- Apply to all suitable tests
- Mark incompatible tests with `@pytest.mark.no_har`
- Document patterns

### Step 4: CI Integration

```yaml
# .github/workflows/test.yml

# Fast tests with HAR replay (on every PR)
- name: Frontend tests (HAR replay)
  run: USE_HAR_REPLAY=1 pytest tests/functional/web/ui/ -n 4

# Full tests (weekly or on-demand)
- name: Full integration tests
  run: pytest tests/functional/web/ --record-mode=rewrite
  if: github.event_name == 'schedule' || contains(github.event.head_commit.message, '[full-test]')
```

______________________________________________________________________

## Key Decisions

1. **Recording mechanism**: Use Playwright's built-in `page.context.route_from_har()` (no custom
   code needed)
2. **Storage**: `tests/functional/web/cassettes/{test_module}/{test_name}.har`
3. **Opt-out**: `@pytest.mark.no_har` for incompatible tests
4. **Record modes**: Same as VCR.py (`none`, `once`, `rewrite`)
5. **Sanitization**: Add only when we see failures, keep it minimal

## Success Metrics

1. **Speed improvement**: HAR replay tests 5-10x faster than full tests
2. **Coverage**: 60-80% of tests work with HAR replay
3. **Reliability**: HAR tests pass consistently
4. **CI time**: PR checks complete in \<5 minutes instead of 15-20 minutes

## Non-Goals (For Initial Implementation)

- ❌ Perfect sanitization of all dynamic data (do it incrementally)
- ❌ Complex request matching algorithms (use Playwright's built-in)
- ❌ HAR file compression/optimization (solve later if needed)
- ❌ Automatic staleness detection (rely on manual re-recording)

## Open Questions (To Discover During Implementation)

1. How to serve frontend static files without backend?

   - Option A: Keep minimal backend for static files only
   - Option B: Use separate static file server
   - Option C: Serve from built assets directly with simple HTTP server

2. How much sanitization is actually needed?

   - Will discover in Phase 3

3. Which tests fundamentally can't use HAR?

   - Will discover in Phase 5

4. Is backend replay worth the effort?

   - Evaluate after Phase 5 success

______________________________________________________________________

## Next Steps

1. ✅ Write this design doc
2. Create `tests/functional/web/cassettes/` directory
3. Add `--record-mode` CLI option to pytest
4. Implement basic HAR recording fixture (Phase 1)
5. Record one simple test and inspect HAR
6. Try replay and see what breaks (Phase 2)
7. Fix issues as we find them (Phase 3)
