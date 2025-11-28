"""
Voice mode integration tests.

These tests verify the full voice mode integration:
1. Voice page loads correctly
2. Tool execution API works correctly
3. Full flow: Gemini sends tool call -> frontend calls backend -> response sent back

The tests use a test seam (window.__TEST_GEMINI_SESSION_FACTORY__) to inject
a mock Gemini session, allowing us to test the complete integration without
requiring real Gemini API credentials.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from playwright.async_api import Page, Route

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_voice_page_loads_with_start_button(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that the voice page loads correctly with a visible Start button."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    await page.goto(f"{base_url}/voice")

    # Should have a visible Start button
    start_button = page.locator("button:has-text('Start')")
    await start_button.wait_for(timeout=10000)
    assert await start_button.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_execution_api_list_notes(web_test_fixture: WebTestFixture) -> None:
    """Test the tool execution API endpoint directly.

    This tests the backend's /api/tools/execute/{name} endpoint which is
    called by the voice mode frontend when Gemini requests tool execution.
    """
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Call the tool execution API directly via page.evaluate
    result = await page.evaluate(
        """async (baseUrl) => {
            const response = await fetch(`${baseUrl}/api/tools/execute/list_notes`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ arguments: {} })
            });
            return {
                status: response.status,
                data: await response.json()
            };
        }""",
        base_url,
    )

    assert result["status"] == 200, f"Expected 200 status, got {result['status']}"
    assert "success" in result["data"] or "result" in result["data"], (
        f"Expected success or result in response, got: {result['data']}"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_execution_api_nonexistent_tool(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that the tool execution API returns an error for unknown tools."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    result = await page.evaluate(
        """async (baseUrl) => {
            const response = await fetch(`${baseUrl}/api/tools/execute/nonexistent_tool_xyz`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ arguments: {} })
            });
            return {
                status: response.status,
                data: await response.json()
            };
        }""",
        base_url,
    )

    # API returns 500 for tool execution errors (including unknown tools)
    assert result["status"] in {404, 500}, (
        f"Expected 404 or 500 status for unknown tool, got {result['status']}"
    )


async def _setup_mock_token_endpoint(page: Page, base_url: str) -> None:
    """Set up mock response for the ephemeral token endpoint."""
    expires_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()

    async def mock_token_response(route: Route) -> None:
        """Return a mock ephemeral token response."""
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "token": "test-ephemeral-token-12345",
                "expires_at": expires_at,
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "list_notes",
                                "description": "List all notes from the system",
                                "parameters": {"type": "OBJECT", "properties": {}},
                            },
                        ]
                    }
                ],
                "system_instruction": "You are a helpful voice assistant.",
                "model": "gemini-2.5-flash-preview-native-audio-dialog",
            }),
        )

    await page.route(f"{base_url}/api/gemini/ephemeral-token", mock_token_response)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_voice_start_fetches_token(web_test_fixture: WebTestFixture) -> None:
    """Test that clicking Start fetches an ephemeral token.

    Note: This only tests up to the token fetch, not the full WebSocket flow.
    """
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Set up mock token endpoint
    await _setup_mock_token_endpoint(page, base_url)

    # Navigate to voice page
    await page.goto(f"{base_url}/voice")
    await page.wait_for_selector("button:has-text('Start')", timeout=10000)

    # Click Start Call and wait for token request using Playwright's expect_request
    async with page.expect_request(
        "**/api/gemini/ephemeral-token", timeout=10000
    ) as request_info:
        await page.click("button:has-text('Start')")

    request = await request_info.value
    assert "/api/gemini/ephemeral-token" in request.url, (
        "Token request should be made when starting call"
    )


# JavaScript code to inject a mock Gemini session factory
MOCK_SESSION_FACTORY_SCRIPT = """
window.__TEST_GEMINI_SESSION_FACTORY__ = async (tokenData) => {
    // Store for test verification
    window.__TEST_TOOL_RESPONSES__ = [];
    window.__TEST_REALTIME_INPUTS__ = [];
    window.__TEST_SESSION_CLOSED__ = false;

    // Create an async generator that yields messages
    const messageQueue = [];
    let resolveNext = null;

    // Function to push messages to the queue (called from test)
    window.__TEST_PUSH_MESSAGE__ = (message) => {
        messageQueue.push(message);
        if (resolveNext) {
            resolveNext();
            resolveNext = null;
        }
    };

    // Create the async iterator
    const asyncIterator = {
        [Symbol.asyncIterator]() {
            return {
                async next() {
                    // Wait for a message if queue is empty
                    while (messageQueue.length === 0 && !window.__TEST_SESSION_CLOSED__) {
                        await new Promise(resolve => {
                            resolveNext = resolve;
                            // Also resolve after timeout to allow checking closed state
                            setTimeout(resolve, 100);
                        });
                    }

                    if (messageQueue.length > 0) {
                        return { value: messageQueue.shift(), done: false };
                    }

                    return { done: true };
                }
            };
        }
    };

    // Create mock session object
    const mockSession = {
        ...asyncIterator,

        sendToolResponse(response) {
            console.log('[MockSession] sendToolResponse:', JSON.stringify(response));
            window.__TEST_TOOL_RESPONSES__.push(response);
            return Promise.resolve();
        },

        sendRealtimeInput(input) {
            console.log('[MockSession] sendRealtimeInput:', input);
            window.__TEST_REALTIME_INPUTS__.push(input);
            return Promise.resolve();
        },

        close() {
            console.log('[MockSession] close');
            window.__TEST_SESSION_CLOSED__ = true;
            if (resolveNext) {
                resolveNext();
            }
        }
    };

    // Schedule a tool call message after a short delay
    setTimeout(() => {
        window.__TEST_PUSH_MESSAGE__({
            toolCall: {
                functionCalls: [{
                    id: 'test-tool-call-123',
                    name: 'list_notes',
                    args: {}
                }]
            }
        });
    }, 500);

    return mockSession;
};
"""


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_voice_tool_call_integration(web_test_fixture: WebTestFixture) -> None:
    """Test the full tool call flow: Gemini -> Frontend -> Backend -> Frontend -> Gemini.

    This test verifies that:
    1. When Gemini sends a tool call, the frontend receives it
    2. The frontend calls the backend tool execution API
    3. The backend executes the tool and returns a result
    4. The frontend sends the tool response back to Gemini
    """
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Inject the mock session factory BEFORE navigating to the page
    await page.add_init_script(MOCK_SESSION_FACTORY_SCRIPT)

    # Also add a simple marker to verify init script runs
    await page.add_init_script(
        "window.__TEST_INIT_SCRIPT_RAN__ = true; console.log('[Test] Init script executed');"
    )

    # Mock the audio APIs to prevent microphone permission issues
    await page.add_init_script("""
        // Mock getUserMedia to return a fake audio stream
        const fakeStream = {
            getTracks: () => [{
                stop: () => console.log('[Test] Fake track stopped'),
                kind: 'audio'
            }],
            getAudioTracks: () => [{ stop: () => {} }]
        };

        navigator.mediaDevices.getUserMedia = async (constraints) => {
            console.log('[Test] Mock getUserMedia called');
            return fakeStream;
        };

        // Mock AudioContext and AudioWorklet
        const OriginalAudioContext = window.AudioContext;
        window.AudioContext = class MockAudioContext {
            constructor(options) {
                console.log('[Test] Mock AudioContext created');
                this.sampleRate = options?.sampleRate || 16000;
                this.state = 'running';
            }

            createMediaStreamSource(stream) {
                return {
                    connect: () => console.log('[Test] MediaStreamSource connected')
                };
            }

            get audioWorklet() {
                return {
                    addModule: async (url) => {
                        console.log('[Test] Mock audioWorklet.addModule called');
                        return Promise.resolve();
                    }
                };
            }

            close() {
                console.log('[Test] Mock AudioContext closed');
            }
        };

        // Mock AudioWorkletNode
        window.AudioWorkletNode = class MockAudioWorkletNode {
            constructor(context, name) {
                console.log('[Test] Mock AudioWorkletNode created:', name);
                this.port = {
                    onmessage: null,
                    postMessage: () => {}
                };
            }
            disconnect() {
                console.log('[Test] Mock AudioWorkletNode disconnected');
            }
        };

        console.log('[Test] Audio APIs mocked');
    """)

    # Set up mock token endpoint (uses the real backend for everything else)
    await _setup_mock_token_endpoint(page, base_url)

    # Navigate to voice page
    await page.goto(f"{base_url}/voice")
    await page.wait_for_selector("button:has-text('Start')", timeout=10000)

    # Verify init script ran
    init_ran = await page.evaluate("window.__TEST_INIT_SCRIPT_RAN__")
    assert init_ran is True, "Init script should have run"

    # Verify mock factory is defined
    factory_defined = await page.evaluate(
        "typeof window.__TEST_GEMINI_SESSION_FACTORY__"
    )
    assert factory_defined == "function", (
        f"Mock factory should be defined as function, got: {factory_defined}"
    )

    # Start listening for tool execution API calls
    tool_api_called = []

    async def capture_tool_call(route: Route) -> None:
        """Capture tool API calls but let them through to the real backend."""
        request = route.request
        tool_api_called.append({
            "url": request.url,
            "method": request.method,
            "post_data": request.post_data,
        })
        # Continue to real backend
        await route.continue_()

    await page.route("**/api/tools/execute/**", capture_tool_call)

    # Click Start to initiate the connection
    await page.click("button:has-text('Start')")

    # Wait for the mock session to send the tool call and frontend to process it
    # The mock sends a tool call after 500ms, then frontend calls backend
    # and sends the response back. We wait for the tool response to be recorded.
    await page.wait_for_function(
        "window.__TEST_TOOL_RESPONSES__ && window.__TEST_TOOL_RESPONSES__.length > 0",
        timeout=15000,
    )

    # Verify the tool API was called
    assert len(tool_api_called) > 0, "Tool execution API should have been called"
    assert "/api/tools/execute/list_notes" in tool_api_called[0]["url"], (
        f"Expected list_notes tool call, got: {tool_api_called[0]['url']}"
    )

    # Verify the tool response was sent back to the mock session
    tool_responses = await page.evaluate("window.__TEST_TOOL_RESPONSES__")
    assert len(tool_responses) > 0, "Tool response should have been sent to session"

    # Check the response structure
    response = tool_responses[0]
    assert "functionResponses" in response, (
        f"Response should have functionResponses, got: {response}"
    )
    assert len(response["functionResponses"]) > 0, (
        "Should have at least one function response"
    )

    func_response = response["functionResponses"][0]
    assert func_response["id"] == "test-tool-call-123", (
        f"Response should have correct tool call ID, got: {func_response['id']}"
    )
    assert func_response["name"] == "list_notes", (
        f"Response should have correct tool name, got: {func_response['name']}"
    )
    assert "response" in func_response, "Response should have response field"

    # The response should contain a result (from the real backend executing list_notes)
    assert (
        "result" in func_response["response"]
        or "error" not in func_response["response"]
    ), f"Tool should have executed successfully, got: {func_response['response']}"
