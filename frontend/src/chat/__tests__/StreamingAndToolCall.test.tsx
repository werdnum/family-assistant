import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

describe('Streaming with Tool Calls', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
  });

  it(
    'correctly renders a message with both text and a tool call',
    async () => {
      // This is the crucial part: we mock the SSE stream to send a single
      // event that contains both content and a tool call.
      server.use(
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
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
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
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

      // 2. The tool call is wrapped in a ToolGroup that starts collapsed once complete
      // Verify the ToolGroup trigger is present (now shows category-based summary)
      await waitFor(
        () => {
          // The text will be something like "1 attachments" based on the tool category
          expect(screen.getByTestId('tool-group-trigger')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );

      const toolGroupContent = screen.getByTestId('tool-group-content');
      expect(toolGroupContent).toHaveAttribute('data-state', 'closed');
      expect(screen.queryByText('📎 Attachments')).not.toBeInTheDocument();

      await user.click(screen.getByTestId('tool-group-trigger'));

      // Check for the attachment UI element after expanding the collapsed tool group
      await waitFor(
        () => {
          expect(screen.getByText('📎 Attachments')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
    },
    { timeout: 30000 }
  );

  it(
    'ignores events tagged with a different turn_id (concurrent foreign turn)',
    async () => {
      // Capture the turn_id the client generates so the mock stream can emit
      // both a matching event and a foreign-turn event interleaved.
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
            conversation_id: body.conversation_id || 'web_conv_foreign',
            first_seq: 0,
          });
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          const encoder = new TextEncoder();
          const stream = new ReadableStream({
            start(controller) {
              // A turn started concurrently from another tab/device publishes
              // into the same conversation stream — must be ignored here.
              controller.enqueue(
                encoder.encode(
                  `event: text\ndata: ${JSON.stringify({
                    turn_id: 'some-other-turn',
                    content: 'FOREIGN-REPLY',
                  })}\n\n`
                )
              );
              // Our turn's reply.
              controller.enqueue(
                encoder.encode(
                  `event: text\ndata: ${JSON.stringify({
                    turn_id: ourTurnId,
                    content: 'My own reply',
                  })}\n\n`
                )
              );
              controller.enqueue(
                encoder.encode(
                  `event: turn_ended\ndata: ${JSON.stringify({
                    turn_id: ourTurnId,
                    status: 'complete',
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

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Hello there');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText('My own reply')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      // The concurrent foreign turn's text must never have been rendered into
      // this send's assistant message.
      expect(screen.queryByText(/FOREIGN-REPLY/)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );
});
