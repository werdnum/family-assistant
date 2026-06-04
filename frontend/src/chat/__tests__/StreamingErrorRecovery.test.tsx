import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, it, beforeEach, expect, vi } from 'vitest';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

/**
 * Helper to create an SSE stream from a sequence of payloads.
 * Each payload is JSON-serialized and sent as a `data:` line.
 */
function createSSEStream(payloads: Record<string, unknown>[]) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const payload of payloads) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
      }
      controller.close();
    },
  });

  return new HttpResponse(stream, {
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  private listeners = new Map<string, Array<(event: { data: string }) => void>>();

  onerror: ((event: unknown) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: { data: string }) => void) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close() {
    // No-op for tests.
  }

  emit(type: string, payload: Record<string, unknown>) {
    const listeners = this.listeners.get(type) ?? [];
    for (const listener of listeners) {
      listener({ data: JSON.stringify(payload) });
    }
  }
}

// Run sequentially to avoid MSW handler conflicts with parallel tests
describe.sequential('Streaming Error Recovery', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/chat');
  });

  it(
    'continues displaying content after a mid-stream error',
    async () => {
      // Simulate: text content → error → more text content → done
      server.use(
        http.post('/api/v1/chat/send_message_stream', () => {
          return createSSEStream([
            { content: 'Starting response... ' },
            { error: 'Tool execution failed: timeout' },
            { content: 'But I can continue answering.' },
            { done: true },
          ]);
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Test mid-stream error');
      await user.keyboard('{Enter}');

      // The final text should include content from BOTH before and after the error
      await waitFor(
        () => {
          expect(
            screen.getByText('Starting response... But I can continue answering.')
          ).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      // The error message should NOT replace the content
      expect(screen.queryByText(/encountered an error/)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'shows error message when stream ends with only an error and no content',
    async () => {
      // Simulate: error only → done (no text content, no tool calls)
      server.use(
        http.post('/api/v1/chat/send_message_stream', () => {
          return createSSEStream([{ error: 'Failed to process request' }, { done: true }]);
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'This will fail completely');
      await user.keyboard('{Enter}');

      // When the stream ends with ONLY an error, the diagnostics message should appear
      await waitFor(
        () => {
          expect(screen.getByText(/encountered an error/)).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    },
    { timeout: 30000 }
  );

  it(
    'preserves tool calls when an error occurs mid-stream',
    async () => {
      // Simulate: tool call → error → done (with text content to finalize)
      server.use(
        http.post('/api/v1/chat/send_message_stream', () => {
          return createSSEStream([
            {
              content: 'Let me look that up.',
              tool_calls: [
                {
                  id: 'call-recovery-test',
                  type: 'function',
                  function: {
                    name: 'search_notes',
                    arguments: JSON.stringify({ query: 'test' }),
                  },
                },
              ],
            },
            { error: 'Minor hiccup during processing' },
            {
              tool_call_id: 'call-recovery-test',
              result: JSON.stringify({ notes: ['Test note'] }),
            },
            { content: ' Found some results.' },
            { done: true },
          ]);
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Search for test notes');
      await user.keyboard('{Enter}');

      // Text content should be preserved (accumulated from both chunks)
      await waitFor(
        () => {
          expect(screen.getByText('Let me look that up. Found some results.')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      // Error should NOT be shown since content was delivered successfully
      expect(screen.queryByText(/encountered an error/)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'clears error state between conversations',
    async () => {
      let callCount = 0;

      server.use(
        http.post('/api/v1/chat/send_message_stream', () => {
          callCount++;

          if (callCount === 1) {
            // First message: error only
            return createSSEStream([{ error: 'Something went wrong' }, { done: true }]);
          }

          // Second message: successful response
          return createSSEStream([{ content: 'This works fine!' }, { done: true }]);
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');

      // First message triggers error
      await user.type(messageInput, 'First message fails');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText(/encountered an error/)).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      // Second message should work fine without leftover error state
      await user.type(messageInput, 'Second message works');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText('This works fine!')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    },
    { timeout: 30000 }
  );

  it(
    'does not let live history reload replace an active activate_tools stream',
    async () => {
      const originalEventSource = globalThis.EventSource;
      globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
      MockEventSource.instances = [];

      mockLocalStorage.getItem.mockImplementation((key: string) =>
        key === 'lastConversationId' ? 'web_conv_activate_tools_race' : null
      );

      let shouldReturnTruncatedHistory = false;

      server.use(
        http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
          expect(params.conversationId).toBe('web_conv_activate_tools_race');

          if (!shouldReturnTruncatedHistory) {
            return HttpResponse.json({ messages: [] });
          }

          return HttpResponse.json({
            messages: [
              {
                internal_id: '300',
                role: 'user',
                content:
                  'New inkplate image: a humorous animal themed scene of a king having a whimsical birthday party.',
                timestamp: '2026-06-04T12:29:00Z',
              },
              {
                internal_id: '301',
                role: 'assistant',
                content: null,
                timestamp: '2026-06-04T12:29:01Z',
                tool_calls: [
                  {
                    id: 'call_activate_tools',
                    type: 'function',
                    function: {
                      name: 'activate_tools',
                      arguments: JSON.stringify({
                        tool_names: ['execute_script', 'generate_image'],
                      }),
                    },
                  },
                ],
              },
              {
                internal_id: '302',
                role: 'tool',
                content: 'Activated tools: execute_script, generate_image. You can now use them.',
                tool_call_id: 'call_activate_tools',
                timestamp: '2026-06-04T12:29:02Z',
              },
            ],
          });
        }),
        http.post('/api/v1/chat/send_message_stream', async ({ request }) => {
          const body = (await request.json()) as { conversation_id: string };
          expect(body.conversation_id).toBe('web_conv_activate_tools_race');

          const encoder = new TextEncoder();
          const stream = new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  `data: ${JSON.stringify({
                    tool_call: {
                      id: 'call_activate_tools',
                      type: 'function',
                      function: {
                        name: 'activate_tools',
                        arguments: JSON.stringify({
                          tool_names: ['execute_script', 'generate_image'],
                        }),
                      },
                    },
                  })}\n\n`
                )
              );
              controller.enqueue(
                encoder.encode(
                  `data: ${JSON.stringify({
                    tool_call_id: 'call_activate_tools',
                    result:
                      'Activated tools: execute_script, generate_image. You can now use them.',
                  })}\n\n`
                )
              );

              shouldReturnTruncatedHistory = true;
              MockEventSource.instances[MockEventSource.instances.length - 1]?.emit('message', {
                internal_id: '302',
                timestamp: '2026-06-04T12:29:02Z',
                new_messages: true,
                role: 'tool',
                content: 'Activated tools: execute_script, generate_image. You can now use them.',
                conversation_id: 'web_conv_activate_tools_race',
              });

              setTimeout(() => {
                controller.enqueue(
                  encoder.encode(
                    `data: ${JSON.stringify({
                      content: 'I can generate and deploy that Inkplate image now.',
                    })}\n\n`
                  )
                );
                controller.enqueue(encoder.encode('data: {"done": true}\n\n'));
                controller.close();
              }, 25);
            },
          });

          return new HttpResponse(stream, {
            headers: { 'Content-Type': 'text/event-stream' },
          });
        })
      );

      try {
        const user = userEvent.setup();
        await renderChatApp({ waitForReady: true });

        await waitFor(
          () => {
            expect(MockEventSource.instances.length).toBeGreaterThan(0);
          },
          { timeout: 3000 }
        );

        const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
        await user.type(messageInput, 'New inkplate image');
        await user.keyboard('{Enter}');

        await waitFor(
          () => {
            expect(
              screen.getByText('I can generate and deploy that Inkplate image now.')
            ).toBeInTheDocument();
          },
          { timeout: 5000 }
        );
      } finally {
        globalThis.EventSource = originalEventSource;
      }
    },
    { timeout: 30000 }
  );

  it(
    'loads historical tool calls without results as terminal instead of pending',
    async () => {
      mockLocalStorage.getItem.mockImplementation((key: string) =>
        key === 'lastConversationId' ? 'web_conv_stuck_tool' : null
      );

      server.use(
        http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
          expect(params.conversationId).toBe('web_conv_stuck_tool');
          return HttpResponse.json({
            messages: [
              {
                internal_id: '100',
                role: 'user',
                content: 'Please check this',
                timestamp: '2026-01-01T10:00:00Z',
              },
              {
                internal_id: '101',
                role: 'assistant',
                content: null,
                timestamp: '2026-01-01T10:00:01Z',
                tool_calls: [
                  {
                    id: 'call_missing_result',
                    type: 'function',
                    function: {
                      name: 'search_notes',
                      arguments: JSON.stringify({ query: 'missing result' }),
                    },
                  },
                ],
              },
            ],
          });
        })
      );

      await renderChatApp({ waitForReady: true });

      await waitFor(
        () => {
          expect(screen.getByTestId('send-button')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      expect(screen.getByPlaceholderText('Message Family Assistant...')).toBeEnabled();
      expect(screen.queryByText('Stop generating')).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'loads persisted error rows as assistant messages',
    async () => {
      mockLocalStorage.getItem.mockImplementation((key: string) =>
        key === 'lastConversationId' ? 'web_conv_error_row' : null
      );

      server.use(
        http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
          expect(params.conversationId).toBe('web_conv_error_row');
          return HttpResponse.json({
            messages: [
              {
                internal_id: '200',
                role: 'user',
                content: 'Please run the tool',
                timestamp: '2026-01-01T10:00:00Z',
              },
              {
                internal_id: '201',
                role: 'assistant',
                content: null,
                timestamp: '2026-01-01T10:00:01Z',
                tool_calls: [
                  {
                    id: 'call_before_error',
                    type: 'function',
                    function: {
                      name: 'attach_to_response',
                      arguments: JSON.stringify({ attachment_ids: ['missing'] }),
                    },
                  },
                ],
              },
              {
                internal_id: '202',
                role: 'error',
                content: 'An error occurred during a tool call.',
                timestamp: '2026-01-01T10:00:02Z',
                error_traceback: 'ValueError: missing attachment',
              },
            ],
          });
        })
      );

      await renderChatApp({ waitForReady: true });

      await waitFor(
        () => {
          expect(screen.getByText('An error occurred during a tool call.')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      expect(screen.getByPlaceholderText('Message Family Assistant...')).toBeEnabled();
    },
    { timeout: 30000 }
  );
});
