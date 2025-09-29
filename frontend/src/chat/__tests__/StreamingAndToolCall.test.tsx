import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

// Mock localStorage for conversation persistence
const mockLocalStorage = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
};
Object.defineProperty(window, 'localStorage', { value: mockLocalStorage });

describe('Streaming with Tool Calls', () => {
  beforeEach(() => {
    mockLocalStorage.getItem.mockReturnValue(null);
    mockLocalStorage.setItem.mockClear();
    vi.clearAllMocks();
  });

  it(
    'correctly renders a message with both text and a tool call',
    async () => {
      // This is the crucial part: we mock the SSE stream to send a single
      // event that contains both content and a tool call.
      server.use(
        http.post('/api/v1/chat/send_message_stream', () => {
          const encoder = new TextEncoder();
          const stream = new ReadableStream({
            start(controller) {
              const payload = {
                content: 'Of course, here is your photo',
                tool_calls: [
                  {
                    id: 'attach_tool_call',
                    type: 'function',
                    function: {
                      name: 'attach_to_response',
                      arguments: JSON.stringify({ attachment_ids: ['some-id'] }),
                    },
                  },
                ],
              };
              // Note: We send a single 'data' packet with both fields.
              // We also don't specify an 'event' type, so it defaults to 'message'.
              controller.enqueue(
                encoder.encode(`data: ${JSON.stringify(payload)}

`)
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
        })
      );

      const user = userEvent.setup();
      renderChatApp();

      // Wait for the app to be ready
      await screen.findByPlaceholderText('Write a message...');

      const messageInput = screen.getByPlaceholderText('Write a message...');
      await user.type(messageInput, 'Send the image');
      await user.keyboard('{Enter}');

      // Now, we assert that both the text and the tool call are visible.
      // This is what was failing in the Playwright test.

      // 1. Check for the assistant's text message
      await waitFor(
        () => {
          expect(screen.getByText('Of course, here is your photo')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      // 2. The tool call is wrapped in a ToolGroup that starts expanded
      // Verify the ToolGroup trigger is present
      await waitFor(
        () => {
          expect(screen.getByText('1 tool call')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      // Check for the attachment UI element (should be visible since ToolGroup starts expanded)
      await waitFor(
        () => {
          expect(screen.getByText('📎 Attachments')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    },
    { timeout: 10000 }
  );
});
