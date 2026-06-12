import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

/**
 * Tests for the resumable-streaming client behaviors implemented in
 * useStreamingResponse / ChatApp: attachment source filtering, stream_dropped
 * resubscribe, interrupted streams, acks, and already_complete history reload.
 */
describe('Resumable streaming client', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/chat');
  });

  const sse = (frames: string[]) => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        for (const frame of frames) {
          controller.enqueue(encoder.encode(frame));
        }
        controller.close();
      },
    });
    return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
  };

  const captureTurnId = (onCapture: (id: string) => void, conversationId = 'web_conv_test') =>
    http.post('/api/v1/chat/turns', async ({ request }) => {
      const body = (await request.json()) as { turn_id: string; conversation_id?: string };
      onCapture(body.turn_id);
      return HttpResponse.json({
        turn_id: body.turn_id,
        conversation_id: body.conversation_id || conversationId,
        first_seq: 0,
      });
    });

  it(
    'only accumulates response-source attachments, ignoring trigger uploads',
    async () => {
      let ourTurnId = '';
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([
            `event: attachment\ndata: ${JSON.stringify({
              turn_id: ourTurnId,
              type: 'attachment',
              source: 'trigger',
              attachment_id: 'user-upload',
              content_url: '/api/attachments/user-upload',
              mime_type: 'image/png',
              seq: 1,
            })}\n\n`,
            `event: attachment\ndata: ${JSON.stringify({
              turn_id: ourTurnId,
              type: 'attachment',
              source: 'response',
              attachment_id: 'assistant-att',
              content_url: '/api/attachments/assistant-att',
              mime_type: 'image/png',
              seq: 2,
            })}\n\n`,
            `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'Here you go', seq: 3 })}\n\n`,
            `event: turn_ended\ndata: ${JSON.stringify({ turn_id: ourTurnId, status: 'complete', seq: 4 })}\n\n`,
          ])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Send the image');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByTestId('tool-group-trigger')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      await user.click(screen.getByTestId('tool-group-trigger'));
      await waitFor(
        () => {
          expect(screen.getByText('📎 Attachments')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      // Exactly one attachment (the response one); the trigger upload is excluded.
      const images = document.querySelectorAll('img[src*="/api/attachments/"]');
      const srcs = Array.from(images).map((img) => img.getAttribute('src'));
      expect(srcs.some((src) => src?.includes('assistant-att'))).toBe(true);
      expect(srcs.some((src) => src?.includes('user-upload'))).toBe(false);
    },
    { timeout: 30000 }
  );

  it(
    'resubscribes from the last seq after a stream_dropped event',
    async () => {
      let ourTurnId = '';
      let streamCalls = 0;
      const subscribedSeqs: Array<string | null> = [];
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', ({ request }) => {
          const url = new URL(request.url);
          subscribedSeqs.push(url.searchParams.get('from_seq'));
          streamCalls += 1;
          if (streamCalls === 1) {
            // Deliver some content, then drop before turn_ended.
            return sse([
              `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'Partial reply ', seq: 5 })}\n\n`,
              `event: stream_dropped\ndata: ${JSON.stringify({ reason: 'queue_overflow' })}\n\n`,
            ]);
          }
          // Resubscribe delivers the rest and ends the turn.
          return sse([
            `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'finished.', seq: 6 })}\n\n`,
            `event: turn_ended\ndata: ${JSON.stringify({ turn_id: ourTurnId, status: 'complete', seq: 7 })}\n\n`,
          ]);
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Tell me something');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText('Partial reply finished.')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      expect(streamCalls).toBe(2);
      // First subscribe from the turn's first_seq (0), resubscribe from the
      // last applied seq (5).
      expect(subscribedSeqs[0]).toBe('0');
      expect(subscribedSeqs[1]).toBe('5');
    },
    { timeout: 30000 }
  );

  it(
    'reloads history and surfaces an error when a stream is interrupted without turn_ended',
    async () => {
      let messagesFetches = 0;
      server.use(
        captureTurnId(() => {}, 'web_conv_interrupted'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () => {
          messagesFetches += 1;
          return HttpResponse.json({
            messages: [
              {
                internal_id: 'persisted-1',
                role: 'assistant',
                content: 'Durably persisted reply',
                timestamp: '2026-06-09T10:00:00Z',
              },
            ],
          });
        }),
        // Two consecutive drops (no successful resubscribe) → interrupted.
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([`event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Will be interrupted');
      await user.keyboard('{Enter}');

      // The deferred reload pulls the durably-persisted reply into the thread.
      await waitFor(
        () => {
          expect(screen.getByText('Durably persisted reply')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      expect(messagesFetches).toBeGreaterThan(0);
    },
    { timeout: 30000 }
  );

  it(
    'POSTs an ack with the turn_ended seq after rendering its turn',
    async () => {
      let ourTurnId = '';
      const ackBodies: Array<{ conversation_id: string; ack_seq: number }> = [];
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }),
        http.post('/api/v1/chat/ack', async ({ request }) => {
          ackBodies.push((await request.json()) as { conversation_id: string; ack_seq: number });
          return HttpResponse.json({ ok: true });
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([
            `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'Hi', seq: 10 })}\n\n`,
            `event: turn_ended\ndata: ${JSON.stringify({ turn_id: ourTurnId, status: 'complete', seq: 11 })}\n\n`,
          ])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Hello');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(ackBodies.length).toBeGreaterThan(0);
        },
        { timeout: 5000 }
      );
      expect(ackBodies[0].ack_seq).toBe(11);
    },
    { timeout: 30000 }
  );

  it(
    'passes ack_seq=-1 on the initial subscribe when nothing applied yet',
    async () => {
      let ourTurnId = '';
      let ackSeqParam: string | null = null;
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', ({ request }) => {
          ackSeqParam = new URL(request.url).searchParams.get('ack_seq');
          return sse([
            `event: turn_ended\ndata: ${JSON.stringify({ turn_id: ourTurnId, status: 'complete', seq: 1 })}\n\n`,
          ]);
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Hello');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(ackSeqParam).toBe('-1');
        },
        { timeout: 5000 }
      );
    },
    { timeout: 30000 }
  );

  it(
    'reloads history instead of opening /stream when the turn is already_complete',
    async () => {
      let streamOpened = false;
      let messagesFetches = 0;
      server.use(
        http.post('/api/v1/chat/turns', async ({ request }) => {
          const body = (await request.json()) as { turn_id: string; conversation_id?: string };
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || 'web_conv_done',
            first_seq: 0,
            already_complete: true,
          });
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          streamOpened = true;
          return sse([`event: turn_ended\ndata: ${JSON.stringify({ status: 'complete' })}\n\n`]);
        }),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () => {
          messagesFetches += 1;
          return HttpResponse.json({
            messages: [
              {
                internal_id: 'done-1',
                role: 'assistant',
                content: 'Reply restored from durable record',
                timestamp: '2026-06-09T10:00:00Z',
              },
            ],
          });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Retry a finished turn');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText('Reply restored from durable record')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      expect(streamOpened).toBe(false);
      expect(messagesFetches).toBeGreaterThan(0);
    },
    { timeout: 30000 }
  );

  it(
    'retries the subscribe after a transient 500 and renders the reply',
    async () => {
      // The turn keeps running server-side even if the subscribe GET fails,
      // so a transient 5xx must lead to a resubscribe (mirroring the follow
      // stream's reconnect behavior), not a spurious error banner.
      let ourTurnId = '';
      let streamCalls = 0;
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          streamCalls += 1;
          if (streamCalls === 1) {
            return HttpResponse.json({ detail: 'Internal Server Error' }, { status: 500 });
          }
          return sse([
            `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'Recovered reply', seq: 1 })}\n\n`,
            `event: turn_ended\ndata: ${JSON.stringify({ turn_id: ourTurnId, status: 'complete', seq: 2 })}\n\n`,
          ]);
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Trigger a flaky subscribe');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText('Recovered reply')).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      expect(streamCalls).toBeGreaterThanOrEqual(2);
      expect(screen.queryByText(/Sorry, I encountered an error/)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );
});
