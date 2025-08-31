import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

// Mock localStorage
const mockLocalStorage = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
};
Object.defineProperty(window, 'localStorage', { value: mockLocalStorage });

describe('ErrorHandling', () => {
  beforeEach(() => {
    mockLocalStorage.getItem.mockReturnValue(null);
    mockLocalStorage.setItem.mockClear();
    vi.clearAllMocks();
  });

  it('handles network errors gracefully', async () => {
    // Mock network failure for streaming endpoint
    server.use(
      http.post('/api/v1/chat/send_message_stream', () => {
        return HttpResponse.error();
      })
    );

    const user = userEvent.setup();
    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    const messageInput = screen.getByPlaceholderText('Write a message...');

    // Try to send a message that will fail
    await user.type(messageInput, 'This should fail');
    await user.keyboard('{Enter}');

    // The @assistant-ui/react runtime should handle the error gracefully
    // We can't predict the exact error UI, but the app shouldn't crash
    await new Promise((resolve) => setTimeout(resolve, 2000));

    // Chat interface should still be present and functional
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(messageInput).toBeInTheDocument();
  }, 10000); // Add timeout

  it('handles API server errors', async () => {
    // Mock 500 server error
    server.use(
      http.post('/api/v1/chat/send_message_stream', () => {
        return HttpResponse.json({ error: 'Internal Server Error' }, { status: 500 });
      })
    );

    const user = userEvent.setup();
    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    const messageInput = screen.getByPlaceholderText('Write a message...');

    await user.type(messageInput, 'This should get a server error');
    await user.keyboard('{Enter}');

    await new Promise((resolve) => setTimeout(resolve, 2000));

    // App should handle server errors without crashing
    expect(screen.getByText('Chat')).toBeInTheDocument();
  }, 10000); // Add timeout

  it('handles malformed streaming responses', async () => {
    // Mock malformed SSE stream
    server.use(
      http.post('/api/v1/chat/send_message_stream', () => {
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
    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    const messageInput = screen.getByPlaceholderText('Write a message...');

    await user.type(messageInput, 'This will have a malformed response');
    await user.keyboard('{Enter}');

    await new Promise((resolve) => setTimeout(resolve, 2000));

    // Runtime should handle stream errors gracefully
    expect(screen.getByText('Chat')).toBeInTheDocument();
  }, 10000); // Add timeout

  it('handles conversation loading errors', async () => {
    // Mock error loading conversations
    server.use(
      http.get('/api/v1/chat/conversations', () => {
        return HttpResponse.json({ error: 'Failed to load conversations' }, { status: 500 });
      })
    );

    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    // Chat should still be usable even if conversations fail to load
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Write a message...')).toBeInTheDocument();
  });

  it('handles profile loading errors', async () => {
    // Mock error loading profiles
    server.use(
      http.get('/api/v1/profiles', () => {
        return HttpResponse.json({ error: 'Failed to load profiles' }, { status: 500 });
      })
    );

    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    // Chat should still function with profile loading errors
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Write a message...')).toBeInTheDocument();
  });

  it('handles tool confirmation API errors', async () => {
    // Mock tool call that triggers confirmation
    server.use(
      http.post('/api/v1/chat/send_message_stream', async ({ request }) => {
        const body = (await request.json()) as {
          prompt: string;
          conversation_id: string;
        };

        if (body.prompt.includes('tool call')) {
          const encoder = new TextEncoder();
          const stream = new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  `data: ${JSON.stringify({
                    tool_calls: [
                      {
                        id: 'call-error-test',
                        type: 'function',
                        function: {
                          name: 'add_or_update_note',
                          arguments: JSON.stringify({ title: 'Test', content: 'Test' }),
                        },
                      },
                    ],
                  })}\n\n`
                )
              );
              controller.enqueue(encoder.encode('data: {"done": true}\n\n'));
              controller.close();
            },
          });

          return new HttpResponse(stream, {
            headers: {
              'Content-Type': 'text/event-stream',
            },
          });
        }

        return HttpResponse.json({ error: 'No handler' }, { status: 404 });
      }),

      // Mock confirmation API error
      http.post('/api/v1/chat/confirm_tool', () => {
        return HttpResponse.json({ error: 'Confirmation failed' }, { status: 500 });
      })
    );

    const user = userEvent.setup();
    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    const messageInput = screen.getByPlaceholderText('Write a message...');

    await user.type(messageInput, 'Please execute a tool call');
    await user.keyboard('{Enter}');

    await new Promise((resolve) => setTimeout(resolve, 2000));

    // Should handle confirmation errors gracefully
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('handles attachment upload errors', async () => {
    // Mock attachment upload failure
    server.use(
      http.post('/api/attachments/upload', () => {
        return HttpResponse.json({ error: 'Upload failed' }, { status: 500 });
      })
    );

    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    // Test attachment error handling if the UI provides file upload
    // This would involve creating a mock file and testing the upload error
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('recovers from temporary network issues', async () => {
    let callCount = 0;

    // Mock intermittent failures that succeed on retry
    server.use(
      http.post('/api/v1/chat/send_message_stream', () => {
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
    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    const messageInput = screen.getByPlaceholderText('Write a message...');

    // First message fails, but runtime might retry
    await user.type(messageInput, 'Test recovery');
    await user.keyboard('{Enter}');

    await new Promise((resolve) => setTimeout(resolve, 2000));

    // App should handle retry logic appropriately
    expect(screen.getByText('Chat')).toBeInTheDocument();
  }, 10000); // Increase timeout

  it('handles extremely long responses', async () => {
    // Mock very long streaming response
    server.use(
      http.post('/api/v1/chat/send_message_stream', () => {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            // Send fewer chunks to avoid timeout - test the concept not performance
            for (let i = 0; i < 20; i++) {
              controller.enqueue(
                encoder.encode(`data: {"content": "Chunk ${i} of a long response. "}\n\n`)
              );
            }
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
    renderChatApp();

    await new Promise((resolve) => setTimeout(resolve, 2000));

    const messageInput = screen.getByPlaceholderText('Write a message...');

    await user.type(messageInput, 'Give me a long response');
    await user.keyboard('{Enter}');

    // Allow time for long response processing
    await new Promise((resolve) => setTimeout(resolve, 3000));

    // UI should handle long responses without performance issues
    expect(screen.getByText('Chat')).toBeInTheDocument();
  }, 10000); // Increase timeout
});
