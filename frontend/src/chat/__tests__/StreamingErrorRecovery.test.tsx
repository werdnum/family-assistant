import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, it, beforeEach, expect, vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
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

// Run sequentially to avoid MSW handler conflicts with parallel tests
describe.sequential('Streaming Error Recovery', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
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

      const messageInput = screen.getByPlaceholderText('Write a message...');
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

      const messageInput = screen.getByPlaceholderText('Write a message...');
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

      const messageInput = screen.getByPlaceholderText('Write a message...');
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

      const messageInput = screen.getByPlaceholderText('Write a message...');

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
});
