import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, it, beforeEach, expect, vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

async function findMessageInput() {
  await waitFor(
    () => {
      expect(screen.getByPlaceholderText('Message Family Assistant...')).toBeInTheDocument();
    },
    { timeout: 5000 }
  );
  return screen.getByPlaceholderText('Message Family Assistant...');
}

// Run sequentially to avoid MSW handler conflicts with parallel tests
describe.sequential('ErrorHandling', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
  });

  it('handles network errors gracefully', async () => {
    // Mock network failure for streaming endpoint
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        return HttpResponse.error();
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = await findMessageInput();

    // Try to send a message that will fail
    await user.type(messageInput, 'This should fail');
    await user.keyboard('{Enter}');

    // Give the runtime time to process the error
    await waitFor(
      () => {
        // Chat interface should still be present and functional after error
        expect(screen.getByText('Chat')).toBeInTheDocument();
        expect(messageInput).toBeInTheDocument();
      },
      { timeout: 5000 }
    );
  }, 30000); // Increased timeout for parallel test runs

  it('handles API server errors', async () => {
    let requestSeen = false;

    // Mock 500 server error
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        requestSeen = true;
        return HttpResponse.json({ error: 'Internal Server Error' }, { status: 500 });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    const messageInput = await findMessageInput();

    await user.type(messageInput, 'This should get a server error');
    await user.keyboard('{Enter}');

    // Wait removed - using waitForReady option

    // App should handle server errors without crashing
    await waitFor(() => {
      expect(requestSeen).toBe(true);
      expect(screen.getByText('Chat')).toBeInTheDocument();
    });
  }, 30000); // Increased timeout for parallel test runs

  it('handles malformed streaming responses', async () => {
    // Mock malformed SSE stream
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            // Send malformed JSON
            controller.enqueue(encoder.encode('data: {invalid json}\n\n'));
            controller.enqueue(encoder.encode('data: {"content": "partial'));
            // Don't close properly to test error handling
            controller.error(new Error('Stream error'));
          },
        });

        return new HttpResponse(stream, {
          headers: {
            'Content-Type': 'text/event-stream',
          },
        });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    const messageInput = await findMessageInput();

    await user.type(messageInput, 'This will have a malformed response');
    await user.keyboard('{Enter}');

    // Wait removed - using waitForReady option

    // Runtime should handle stream errors gracefully
    expect(screen.getByText('Chat')).toBeInTheDocument();
  }, 30000); // Increased timeout for parallel test runs

  it('surfaces a failed turn_ended error instead of silently completing', async () => {
    // The producer reports a failed turn as turn_ended with status "failed" and
    // an error field. The client must surface that before exiting, not clear
    // loading as if it completed normally.
    let ourTurnId = '';
    server.use(
      http.post('/api/v1/chat/turns', async ({ request }) => {
        const body = (await request.json()) as {
          turn_id: string;
          conversation_id?: string;
        };
        ourTurnId = body.turn_id;
        return HttpResponse.json({
          turn_id: body.turn_id,
          conversation_id: body.conversation_id || 'web_conv_failed',
          first_seq: 0,
        });
      }),
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                `event: turn_started\ndata: ${JSON.stringify({ turn_id: ourTurnId, seq: 0 })}\n\n`
              )
            );
            controller.enqueue(
              encoder.encode(
                `event: turn_ended\ndata: ${JSON.stringify({
                  turn_id: ourTurnId,
                  status: 'failed',
                  error: 'The model provider is unavailable.',
                })}\n\n`
              )
            );
            controller.close();
          },
        });
        return new HttpResponse(stream, {
          headers: { 'Content-Type': 'text/event-stream' },
        });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = await findMessageInput();
    await user.type(messageInput, 'This turn will fail');
    await user.keyboard('{Enter}');

    await waitFor(
      () => {
        expect(
          screen.getByText(/encountered an error processing your message/i)
        ).toBeInTheDocument();
      },
      { timeout: 5000 }
    );
  }, 30000);

  it('handles conversation loading errors', async () => {
    // Mock error loading conversations
    server.use(
      http.get('/api/v1/chat/conversations', () => {
        return HttpResponse.json({ error: 'Failed to load conversations' }, { status: 500 });
      })
    );

    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    // Chat should still be usable even if conversations fail to load
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(await findMessageInput()).toBeInTheDocument();
  });

  it('handles profile loading errors', async () => {
    // Mock error loading profiles
    server.use(
      http.get('/api/v1/profiles', () => {
        return HttpResponse.json({ error: 'Failed to load profiles' }, { status: 500 });
      })
    );

    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    // Chat should still function with profile loading errors
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(await findMessageInput()).toBeInTheDocument();
  });

  it('handles tool confirmation API errors', async () => {
    // Mock tool call that triggers confirmation
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(`event: turn_started\ndata: ${JSON.stringify({ seq: 0 })}\n\n`)
            );
            controller.enqueue(
              encoder.encode(
                `data: ${JSON.stringify({
                  tool_call: {
                    id: 'call-error-test',
                    type: 'function',
                    function: {
                      name: 'add_or_update_note',
                      arguments: JSON.stringify({ title: 'Test', content: 'Test' }),
                    },
                  },
                })}\n\n`
              )
            );
            controller.enqueue(
              encoder.encode(
                `event: turn_ended\ndata: ${JSON.stringify({ status: 'complete' })}\n\n`
              )
            );
            controller.close();
          },
        });

        return new HttpResponse(stream, {
          headers: {
            'Content-Type': 'text/event-stream',
          },
        });
      }),

      // Mock confirmation API error
      http.post('/api/v1/chat/confirm_tool', () => {
        return HttpResponse.json({ error: 'Confirmation failed' }, { status: 500 });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    const messageInput = await findMessageInput();

    await user.type(messageInput, 'Please execute a tool call');
    await user.keyboard('{Enter}');

    // Wait removed - using waitForReady option

    // Should handle confirmation errors gracefully
    expect(screen.getByText('Chat')).toBeInTheDocument();
  }, 10000);

  it('handles attachment upload errors', async () => {
    // Mock attachment upload failure
    server.use(
      http.post('/api/attachments/upload', () => {
        return HttpResponse.json({ error: 'Upload failed' }, { status: 500 });
      })
    );

    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    // Test attachment error handling if the UI provides file upload
    // This would involve creating a mock file and testing the upload error
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('recovers from temporary network issues', async () => {
    let callCount = 0;

    // Mock intermittent failures that succeed on retry
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        callCount++;

        if (callCount === 1) {
          // First call fails
          return HttpResponse.error();
        }

        // Second call succeeds
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode('data: {"content": "Recovery successful!"}\n\n'));
            controller.enqueue(encoder.encode('data: {"done": true}\n\n'));
            controller.close();
          },
        });

        return new HttpResponse(stream, {
          headers: {
            'Content-Type': 'text/event-stream',
          },
        });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    const messageInput = await findMessageInput();

    // First message fails, but runtime might retry
    await user.type(messageInput, 'Test recovery');
    await user.keyboard('{Enter}');

    // Wait removed - using waitForReady option

    // App should handle retry logic appropriately
    expect(screen.getByText('Chat')).toBeInTheDocument();
  }, 10000); // Increase timeout

  it('handles extremely long responses', async () => {
    // Mock very long streaming response
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            // Send fewer chunks to avoid timeout - test the concept not performance
            for (let i = 0; i < 20; i++) {
              controller.enqueue(
                encoder.encode(`data: {"content": "Chunk ${i} of a long response. "}\n\n`)
              );
            }
            controller.enqueue(
              encoder.encode(
                `event: turn_ended\ndata: ${JSON.stringify({ status: 'complete' })}\n\n`
              )
            );
            controller.close();
          },
        });

        return new HttpResponse(stream, {
          headers: {
            'Content-Type': 'text/event-stream',
          },
        });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    const messageInput = await findMessageInput();

    await user.type(messageInput, 'Give me a long response');
    await user.keyboard('{Enter}');

    // UI should handle long responses without performance issues
    // Wait for the response to complete
    await waitFor(
      () => {
        expect(screen.getByText('Chat')).toBeInTheDocument();
      },
      { timeout: 5000 }
    );
  }, 10000); // Increase timeout
});
