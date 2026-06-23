import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import { reconcilePollTuning } from '../ChatApp';
import { streamResumeTuning } from '../useStreamingResponse';

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

  const createDeferred = (): { promise: Promise<void>; resolve: () => void } => {
    let resolve!: () => void;
    const promise = new Promise<void>((promiseResolve) => {
      resolve = promiseResolve;
    });
    return { promise, resolve };
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
      // First subscribe from the turn's first_seq (0); resume from just past the
      // last applied seq (5 + 1 = 6) since the hub replays seq >= from_seq.
      expect(subscribedSeqs[0]).toBe('0');
      expect(subscribedSeqs[1]).toBe('6');
    },
    { timeout: 30000 }
  );

  it(
    'resumes when the stream errors mid-read (not a clean EOF) after a tool call',
    async () => {
      // A proxy reset / network drop rejects reader.read() instead of yielding a
      // clean EOF or stream_dropped frame. That thrown error must be treated as
      // a resumable interruption, not surfaced as the spurious post-tool error.
      let ourTurnId = '';
      let streamCalls = 0;
      const encoder = new TextEncoder();
      const toolCall = {
        id: 'call_e',
        type: 'function' as const,
        function: { name: 'search_notes', arguments: '{}' },
      };
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          streamCalls += 1;
          if (streamCalls === 1) {
            // Tool call (seq 1), then ERROR the stream — read() rejects.
            return new HttpResponse(
              new ReadableStream({
                start(controller) {
                  controller.enqueue(
                    encoder.encode(
                      `event: tool_call\ndata: ${JSON.stringify({ turn_id: ourTurnId, tool_call: toolCall, seq: 1 })}\n\n`
                    )
                  );
                  controller.error(new Error('network reset'));
                },
              }),
              { headers: { 'Content-Type': 'text/event-stream' } }
            );
          }
          // Resume delivers the final text + turn_ended.
          return sse([
            `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'All done.', seq: 2 })}\n\n`,
            `event: turn_ended\ndata: ${JSON.stringify({ turn_id: ourTurnId, status: 'complete', seq: 3 })}\n\n`,
          ]);
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });
      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Search please');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText('All done.')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      expect(streamCalls).toBe(2);
      expect(
        screen.queryByText(/encountered an error after running tools/)
      ).not.toBeInTheDocument();
      expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'resume cursor advances past other turns; ack stays our turn',
    async () => {
      // The replay cursor (from_seq) must advance past a concurrent turn's seqs
      // so a resume stays ahead of the bounded conversation-wide buffer (else a
      // 410 eviction could wrongly complete this turn). The ack cursor (ack_seq)
      // must stay OUR turn's, so we never ack another turn's turn_ended.
      let ourTurnId = '';
      let streamCalls = 0;
      const fromSeqs: Array<string | null> = [];
      const ackSeqs: Array<string | null> = [];
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', ({ request }) => {
          const url = new URL(request.url);
          fromSeqs.push(url.searchParams.get('from_seq'));
          ackSeqs.push(url.searchParams.get('ack_seq'));
          streamCalls += 1;
          if (streamCalls === 1) {
            return sse([
              `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'Mine ', seq: 1 })}\n\n`,
              // Another turn's terminal frame with a higher seq — ignored for
              // rendering and ack, but it DOES advance the replay cursor.
              `event: turn_ended\ndata: ${JSON.stringify({ turn_id: 'other-turn', status: 'complete', seq: 5 })}\n\n`,
            ]);
          }
          // Our turn's continuation comes after the other turn's seq 5.
          return sse([
            `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'done.', seq: 6 })}\n\n`,
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
          expect(screen.getByText('Mine done.')).toBeInTheDocument();
        },
        { timeout: 5000 }
      );
      // Replay cursor advanced past the other turn's seq 5 → resume from 6.
      expect(fromSeqs[1]).toBe('6');
      expect(ackSeqs[1]).toBe('1');
    },
    { timeout: 30000 }
  );

  it(
    'silently reconciles from history when a turn is gone (no error)',
    async () => {
      let messagesFetches = 0;
      let ourTurnId = '';
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_interrupted'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () => {
          messagesFetches += 1;
          return HttpResponse.json({
            messages: [
              {
                internal_id: 'persisted-1',
                role: 'assistant',
                // Same turn as the dropped stream → counts as this turn's reply.
                turn_id: ourTurnId,
                content: 'Durably persisted reply',
                timestamp: '2026-06-09T10:00:00Z',
              },
            ],
          });
        }),
        // Every subscribe instantly drops with no progress → the turn is gone
        // from the hub, so resume gives up and reconciles from history.
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([`event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Will be interrupted');
      await user.keyboard('{Enter}');

      // The reload pulls the durably-persisted reply into the thread — silently,
      // with no error shown for a turn that succeeded server-side.
      await waitFor(
        () => {
          expect(screen.getByText('Durably persisted reply')).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      expect(messagesFetches).toBeGreaterThan(0);
      expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/connection was interrupted/i)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'a 410 surfaces the durably-persisted reply (no error)',
    async () => {
      // The common 410: the turn finished and its events rotated out of the hub
      // buffer. Reconciling history must surface the persisted reply with no
      // "couldn't confirm" marker.
      let ourTurnId = '';
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_410_done'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          HttpResponse.json({
            messages: [
              {
                internal_id: 'a1',
                role: 'assistant',
                turn_id: ourTurnId,
                content: 'Reply after buffer eviction',
                timestamp: '2026-06-09T10:00:00Z',
              },
            ],
          })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          HttpResponse.json({ detail: 'gone' }, { status: 410 })
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });
      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Will 410');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText('Reply after buffer eviction')).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'a 410 while the turn is still running reconciles its partial without error',
    async () => {
      // A 410 can also fire while the turn is STILL running (early events
      // evicted). Reconciling must NOT flag a failure for a turn the server still
      // reports as running — its reply is still coming.
      let ourTurnId = '';
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_410_running'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          HttpResponse.json({
            active_turns: [
              { turn_id: ourTurnId, started_at: '2026-06-09T10:00:00Z', status: 'running' },
            ],
            messages: [
              {
                internal_id: 'u1',
                role: 'user',
                turn_id: ourTurnId,
                content: 'Run the long tool',
                timestamp: '2026-06-09T10:00:00Z',
              },
              {
                internal_id: 'a1',
                role: 'assistant',
                turn_id: ourTurnId,
                content: null,
                tool_calls: [
                  {
                    id: 'call_q',
                    type: 'function',
                    function: { name: 'search_notes', arguments: '{}' },
                  },
                ],
                timestamp: '2026-06-09T10:00:01Z',
              },
              {
                internal_id: 't1',
                role: 'tool',
                turn_id: ourTurnId,
                content: 'Found 1 note.',
                tool_call_id: 'call_q',
                timestamp: '2026-06-09T10:00:02Z',
              },
            ],
          })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          HttpResponse.json({ detail: 'gone' }, { status: 410 })
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });
      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Run the long tool');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByTestId('tool-group')).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'a 410 with no persisted reply does NOT surface a marker (assumes success)',
    async () => {
      // Unlike a give-up (resume loop exhausted), a 410 means the reply was
      // durably persisted before eviction — it may just be lagging. A 410 whose
      // reconcile shows no reply must NOT mark "couldn't confirm"; it assumes
      // success (like already_complete) and the reply self-heals on a later load.
      let ourTurnId = '';
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_410_noreply'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          HttpResponse.json({
            messages: [
              {
                internal_id: 'u1',
                role: 'user',
                turn_id: ourTurnId,
                content: 'Will 410 with no reply',
                timestamp: '2026-06-09T10:00:00Z',
              },
            ],
          })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          HttpResponse.json({ detail: 'gone' }, { status: 410 })
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });
      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Will 410 with no reply');
      await user.keyboard('{Enter}');

      // The reconcile replaces the optimistic spinner with the server history
      // (just the user message) — no assistant bubble, and crucially no marker.
      await waitFor(
        () => {
          expect(screen.queryAllByTestId('assistant-message-content')).toHaveLength(0);
        },
        { timeout: 10000 }
      );
      expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'does not treat a tool-only persisted row as terminal on give-up',
    async () => {
      // Tool-call assistant rows can be persisted before the final reply/error
      // commits. After a give-up reconcile, that row is not proof that the turn
      // reached terminal completion — keep the turn unconfirmed instead of
      // silently accepting a tool-only bubble as the final reply.
      let ourTurnId = '';
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_toolonly'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          HttpResponse.json({
            messages: [
              {
                internal_id: 'u1',
                role: 'user',
                turn_id: ourTurnId,
                content: 'Run the tool',
                timestamp: '2026-06-09T10:00:00Z',
              },
              {
                internal_id: 'a1',
                role: 'assistant',
                turn_id: ourTurnId,
                content: null,
                tool_calls: [
                  {
                    id: 'call_y',
                    type: 'function',
                    function: { name: 'search_notes', arguments: '{}' },
                  },
                ],
                timestamp: '2026-06-09T10:00:01Z',
              },
              {
                internal_id: 't1',
                role: 'tool',
                turn_id: ourTurnId,
                content: 'Found 1 note.',
                tool_call_id: 'call_y',
                timestamp: '2026-06-09T10:00:02Z',
              },
            ],
          })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([`event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });
      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Run the tool');
      await user.keyboard('{Enter}');

      // Wait for the reconciled tool-call group to render — it only appears
      // after the give-up reload runs, and the error-append decision happens in
      // the same callback, so once it is visible the decision is final.
      await waitFor(
        () => {
          expect(screen.getByTestId('tool-group')).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'does not flag a still-running turn whose reply is still coming',
    async () => {
      // The give-up means we could not hold the stream open, NOT that the turn
      // failed. When /messages still reports the turn as running (active_turns),
      // its persisted rows so far are partial — a tool call with no final text —
      // and must NOT be treated as a terminal reply. No error; the answer is
      // still coming and the always-on follow stream will reconcile it.
      let ourTurnId = '';
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_still_running'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          HttpResponse.json({
            active_turns: [
              { turn_id: ourTurnId, started_at: '2026-06-09T10:00:00Z', status: 'running' },
            ],
            messages: [
              {
                internal_id: 'u1',
                role: 'user',
                turn_id: ourTurnId,
                content: 'Run the long tool',
                timestamp: '2026-06-09T10:00:00Z',
              },
              {
                internal_id: 'a1',
                role: 'assistant',
                turn_id: ourTurnId,
                content: null,
                tool_calls: [
                  {
                    id: 'call_z',
                    type: 'function',
                    function: { name: 'search_notes', arguments: '{}' },
                  },
                ],
                timestamp: '2026-06-09T10:00:01Z',
              },
              {
                internal_id: 't1',
                role: 'tool',
                turn_id: ourTurnId,
                content: 'Found 1 note.',
                tool_call_id: 'call_z',
                timestamp: '2026-06-09T10:00:02Z',
              },
            ],
          })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([`event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });
      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Run the long tool');
      await user.keyboard('{Enter}');

      // The reconciled partial tool-call renders (the reload ran); the error
      // decision happens in the same callback, so once it's visible it's final.
      await waitFor(
        () => {
          expect(screen.getByTestId('tool-group')).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'reconciles a still-running give-up turn via the fallback poll',
    async () => {
      // A give-up while the turn is still running relies on a later reload to
      // surface the reply. The always-on follow stream can miss the turn_ended
      // (it tails future-only / may be disconnected), so a bounded fallback poll
      // must reconcile the completion on its own. First reconcile sees the turn
      // running; a later poll sees the persisted reply.
      const originalTuning = { ...reconcilePollTuning };
      reconcilePollTuning.intervalMs = 50;
      let ourTurnId = '';
      let messagesCalls = 0;
      try {
        server.use(
          captureTurnId((id) => {
            ourTurnId = id;
          }, 'web_conv_poll_heal'),
          http.get('/api/v1/chat/conversations/:conversationId/messages', () => {
            messagesCalls += 1;
            const userRow = {
              internal_id: 'u1',
              role: 'user',
              turn_id: ourTurnId,
              content: 'Run the long tool',
              timestamp: '2026-06-09T10:00:00Z',
            };
            if (messagesCalls >= 2) {
              // The turn has since finished and persisted its reply.
              return HttpResponse.json({
                messages: [
                  userRow,
                  {
                    internal_id: 'a1',
                    role: 'assistant',
                    turn_id: ourTurnId,
                    content: 'Final reply after the poll',
                    timestamp: '2026-06-09T10:00:05Z',
                  },
                ],
              });
            }
            // First reconcile: still running, with only a PARTIAL tool-call row
            // persisted so far. The still-running check must win over the
            // terminal-reply check (a tool call is not the final reply while the
            // turn runs), or the turn would be cleared and never polled.
            return HttpResponse.json({
              active_turns: [
                { turn_id: ourTurnId, started_at: '2026-06-09T10:00:00Z', status: 'running' },
              ],
              messages: [
                userRow,
                {
                  internal_id: 'a0',
                  role: 'assistant',
                  turn_id: ourTurnId,
                  content: null,
                  tool_calls: [
                    {
                      id: 'call_p',
                      type: 'function',
                      function: { name: 'search_notes', arguments: '{}' },
                    },
                  ],
                  timestamp: '2026-06-09T10:00:01Z',
                },
                {
                  internal_id: 't0',
                  role: 'tool',
                  turn_id: ourTurnId,
                  content: 'Found 1 note.',
                  tool_call_id: 'call_p',
                  timestamp: '2026-06-09T10:00:02Z',
                },
              ],
            });
          }),
          http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
            sse([
              `event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`,
            ])
          )
        );

        const user = userEvent.setup();
        await renderChatApp({ waitForReady: true });
        const input = screen.getByPlaceholderText('Message Family Assistant...');
        await user.type(input, 'Run the long tool');
        await user.keyboard('{Enter}');

        // The fallback poll picks up the persisted reply with no error.
        await waitFor(
          () => {
            expect(screen.getByText('Final reply after the poll')).toBeInTheDocument();
          },
          { timeout: 10000 }
        );
        expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
      } finally {
        Object.assign(reconcilePollTuning, originalTuning);
      }
    },
    { timeout: 30000 }
  );

  it(
    'keeps a bailed 410 reconcile silent at the poll cap unless the turn was observed running',
    async () => {
      // A plain 410 assumes the reply is durably persisted and may just be lagging.
      // If its first reconcile is bailed by a newer active stream, the fallback
      // poll must not turn that healthy-410 assumption into a user-visible marker
      // unless a successful reconcile first observed this turn still running.
      const originalTuning = { ...reconcilePollTuning };
      reconcilePollTuning.intervalMs = 20;
      reconcilePollTuning.maxPolls = 0;

      const firstReloadStarted = createDeferred();
      const releaseFirstReload = createDeferred();
      const secondStreamStarted = createDeferred();
      let firstTurnId = '';
      let conversationId = '';
      let streamCalls = 0;
      let messagesCalls = 0;

      try {
        server.use(
          http.post('/api/v1/chat/turns', async ({ request }) => {
            const body = (await request.json()) as {
              turn_id: string;
              prompt: string;
              conversation_id?: string;
            };
            conversationId = body.conversation_id || conversationId || 'web_conv_410_bailed';
            if (body.prompt === 'First 410') {
              firstTurnId = body.turn_id;
            }
            return HttpResponse.json({
              turn_id: body.turn_id,
              conversation_id: conversationId,
              first_seq: 0,
            });
          }),
          http.get('/api/v1/chat/conversations/:conversationId/messages', async () => {
            messagesCalls += 1;
            if (messagesCalls === 1) {
              firstReloadStarted.resolve();
              await releaseFirstReload.promise;
              return HttpResponse.json({
                messages: [
                  {
                    internal_id: 'u1',
                    role: 'user',
                    turn_id: firstTurnId,
                    content: 'First 410',
                    timestamp: '2026-06-09T10:00:00Z',
                  },
                ],
              });
            }
            return HttpResponse.json({ messages: [] });
          }),
          http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
            streamCalls += 1;
            if (streamCalls === 1) {
              return HttpResponse.json({ detail: 'gone' }, { status: 410 });
            }
            secondStreamStarted.resolve();
            return new HttpResponse(new ReadableStream({ start() {} }), {
              headers: { 'Content-Type': 'text/event-stream' },
            });
          })
        );

        const user = userEvent.setup();
        await renderChatApp({ waitForReady: true });
        const input = screen.getByPlaceholderText('Message Family Assistant...');
        await user.type(input, 'First 410');
        await user.keyboard('{Enter}');

        await firstReloadStarted.promise;
        await user.type(input, 'Second hangs');
        await user.keyboard('{Enter}');
        await secondStreamStarted.promise;
        releaseFirstReload.resolve();

        await new Promise((resolve) => setTimeout(resolve, 100));
        expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
      } finally {
        releaseFirstReload.resolve();
        Object.assign(reconcilePollTuning, originalTuning);
      }
    },
    { timeout: 30000 }
  );

  it(
    'marks a 410 turn that was seen running but then finished with no reply',
    async () => {
      // A 410 normally assumes success — but once we OBSERVE the turn running, we
      // own its completion. If it then finishes with no persisted reply (producer
      // failed before committing a row), it must surface the marker, not silently
      // clear like a healthy 410.
      const originalTuning = { ...reconcilePollTuning };
      reconcilePollTuning.intervalMs = 50;
      let ourTurnId = '';
      let messagesCalls = 0;
      try {
        server.use(
          captureTurnId((id) => {
            ourTurnId = id;
          }, 'web_conv_410_ran_then_failed'),
          http.get('/api/v1/chat/conversations/:conversationId/messages', () => {
            messagesCalls += 1;
            const userRow = {
              internal_id: 'u1',
              role: 'user',
              turn_id: ourTurnId,
              content: 'Will fail',
              timestamp: '2026-06-09T10:00:00Z',
            };
            if (messagesCalls >= 2) {
              // Producer failed: turn no longer running, NO assistant/error row.
              return HttpResponse.json({ messages: [userRow] });
            }
            // First reconcile (from the 410): still running.
            return HttpResponse.json({
              active_turns: [
                { turn_id: ourTurnId, started_at: '2026-06-09T10:00:00Z', status: 'running' },
              ],
              messages: [userRow],
            });
          }),
          http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
            HttpResponse.json({ detail: 'gone' }, { status: 410 })
          )
        );

        const user = userEvent.setup();
        await renderChatApp({ waitForReady: true });
        const input = screen.getByPlaceholderText('Message Family Assistant...');
        await user.type(input, 'Will fail');
        await user.keyboard('{Enter}');

        await waitFor(
          () => {
            expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
          },
          { timeout: 10000 }
        );
      } finally {
        Object.assign(reconcilePollTuning, originalTuning);
      }
    },
    { timeout: 30000 }
  );

  it(
    'surfaces an error when a gone turn left no persisted reply',
    async () => {
      // A stream_dropped before turn_ended (e.g. server shutdown) is NOT proof
      // the reply was committed. If the reconciled history has no terminal reply
      // for the turn, surface an error rather than silently leaving the user
      // with just their message and no response.
      server.use(
        captureTurnId(() => {}, 'web_conv_gone_noreply'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          // Only the user message persisted — the producer never committed a reply.
          HttpResponse.json({
            messages: [
              {
                internal_id: 'persisted-user',
                role: 'user',
                content: 'Do the thing',
                timestamp: '2026-06-09T10:00:00Z',
              },
            ],
          })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([`event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Do the thing');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
    },
    { timeout: 30000 }
  );

  it(
    'surfaces an error when the give-up history reload itself fails',
    async () => {
      // A failed /messages reconcile (non-OK) after a give-up gives us no server
      // truth, so we must NOT go silent — surface the unconfirmed-reply error
      // rather than leaving the user with only their message.
      server.use(
        captureTurnId(() => {}, 'web_conv_reload_fail'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          HttpResponse.json({ detail: 'history unavailable' }, { status: 500 })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([`event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Drops then reload fails');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      // The error REPLACES the stranded optimistic spinner bubble — the user
      // must not see a perpetual loading spinner alongside the error. This
      // no-content give-up would otherwise leave a LOADING_MARKER bubble.
      expect(screen.getAllByTestId('assistant-message-content')).toHaveLength(1);
    },
    { timeout: 30000 }
  );

  it(
    'still flags an unconfirmed reply when partial output streamed but the reload fails',
    async () => {
      // The stream rendered PARTIAL text before dropping; the resume loop then
      // gives up and the /messages reconcile fails. The optimistic partial is NOT
      // proof the reply finished, so the marker must still surface alongside it —
      // never present a truncated answer as if it were complete.
      let ourTurnId = '';
      let streamLeg = 0;
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_partial_then_fail'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          HttpResponse.json({ detail: 'history unavailable' }, { status: 500 })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          streamLeg += 1;
          if (streamLeg === 1) {
            // First leg streams partial text, then ends with no turn_ended.
            return sse([
              `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'Partial answer so far', seq: 1 })}\n\n`,
            ]);
          }
          // Every resume fails → give up.
          return HttpResponse.json({ detail: 'gone' }, { status: 503 });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Stream then drop');
      await user.keyboard('{Enter}');

      // Both the partial text AND the unconfirmed marker are shown — the partial
      // is not mistaken for a finished reply.
      await waitFor(
        () => {
          expect(screen.getByText(/Partial answer so far/)).toBeInTheDocument();
          expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
    },
    { timeout: 30000 }
  );

  it(
    'surfaces an error even if a concurrent turn left a reply in history',
    async () => {
      // A concurrent turn (another tab/device) may leave its assistant reply in
      // history while THIS turn produced none. Matching the reply by turn_id
      // ensures the other turn's reply does not mask this turn's failure.
      let ourTurnId = '';
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_concurrent_other'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
          HttpResponse.json({
            messages: [
              {
                internal_id: 'other-user',
                role: 'user',
                turn_id: 'other-turn',
                content: 'A different question',
                timestamp: '2026-06-09T10:00:00Z',
              },
              {
                internal_id: 'other-reply',
                role: 'assistant',
                // Belongs to a DIFFERENT turn, not ours.
                turn_id: 'other-turn',
                content: 'Answer to the other tab',
                timestamp: '2026-06-09T10:00:01Z',
              },
            ],
          })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          sse([`event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`])
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'My question');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      // Sanity: ours is a fresh client-generated id, not the other turn's.
      expect(ourTurnId).not.toBe('other-turn');
    },
    { timeout: 30000 }
  );

  it(
    'surfaces an error when every subscribe attempt fails (never reaches the hub)',
    async () => {
      // Unlike a drop after connecting, a persistent 5xx subscribe means we
      // never observed any server truth about the turn, so we must NOT silently
      // reconcile (that could drop a still-running turn) — surface an error.
      let messagesFetches = 0;
      server.use(
        captureTurnId(() => {}, 'web_conv_subfail'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () => {
          messagesFetches += 1;
          return HttpResponse.json({ messages: [] });
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
          HttpResponse.json({ detail: 'upstream unavailable' }, { status: 503 })
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Server is down');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      // We reconciled from history (which had no reply) and surfaced an error
      // rather than silently swallowing the turn.
      expect(messagesFetches).toBeGreaterThan(0);
    },
    { timeout: 30000 }
  );

  it(
    'never treats a subscribe failure as liveness (gives up, no spin)',
    async () => {
      // A subscribe_failed must NEVER count as a held-open live turn, regardless
      // of how long it took — otherwise the resume loop resets its give-up
      // streak forever and the bubble spins. Shrinking livenessMs to 0 makes
      // every (instant) 503 leg trivially exceed the liveness window, so this
      // asserts the subscribe_failed exclusion itself, not a sub-threshold
      // duration — and needs no real wall-clock sleep to do it.
      // Snapshot before the try (a plain spread can't throw); everything that
      // mutates the shared tuning lives inside the try so the finally always
      // restores it, even if setup throws.
      const originalTuning = { ...streamResumeTuning };
      let streamCalls = 0;
      try {
        streamResumeTuning.livenessMs = 0;
        streamResumeTuning.initialDelayMs = 1;
        streamResumeTuning.maxDelayMs = 2;
        server.use(
          captureTurnId(() => {}, 'web_conv_slow_subfail'),
          http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
            HttpResponse.json({ messages: [] })
          ),
          http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
            streamCalls += 1;
            return HttpResponse.json({ detail: 'upstream error' }, { status: 503 });
          })
        );

        const user = userEvent.setup();
        await renderChatApp({ waitForReady: true });

        const input = screen.getByPlaceholderText('Message Family Assistant...');
        await user.type(input, 'Failing server');
        await user.keyboard('{Enter}');

        await waitFor(
          () => {
            expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
          },
          { timeout: 10000 }
        );
        // Bounded number of attempts (initial + maxNoProgressResumes), not a spin.
        expect(streamCalls).toBeLessThanOrEqual(6);
      } finally {
        Object.assign(streamResumeTuning, originalTuning);
      }
    },
    { timeout: 30000 }
  );

  it(
    'gives up a held-open leg when only a CONCURRENT turn is running, not ours',
    async () => {
      // The backend holds the conversation stream open while ANY of the user's
      // turns runs. A leg held open by a concurrent turn (another tab) must NOT
      // count as liveness for OUR gone turn — otherwise our turn spins until the
      // unrelated turn finishes. The active-turn check distinguishes them: with
      // livenessMs=0 every (held-open) stream_dropped leg would reset the streak,
      // but the check reports OUR turn is not running, so the loop still gives up.
      const originalTuning = { ...streamResumeTuning };
      let streamCalls = 0;
      try {
        streamResumeTuning.livenessMs = 0;
        streamResumeTuning.initialDelayMs = 1;
        streamResumeTuning.maxDelayMs = 2;
        let ourTurnId = '';
        server.use(
          captureTurnId((id) => {
            ourTurnId = id;
          }, 'web_conv_concurrent'),
          http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
            // A DIFFERENT turn is running; ours is not, and left no reply.
            HttpResponse.json({
              active_turns: [
                {
                  turn_id: 'someone-elses-turn',
                  started_at: '2026-06-09T10:00:00Z',
                  status: 'running',
                },
              ],
              messages: [
                {
                  internal_id: 'u1',
                  role: 'user',
                  turn_id: ourTurnId,
                  content: 'Hi',
                  timestamp: '2026-06-09T10:00:00Z',
                },
              ],
            })
          ),
          http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
            streamCalls += 1;
            // A held-open leg that drops (NOT a subscribe failure).
            return sse([
              `event: stream_dropped\ndata: ${JSON.stringify({ reason: 'idle_cut' })}\n\n`,
            ]);
          })
        );

        const user = userEvent.setup();
        await renderChatApp({ waitForReady: true });
        const input = screen.getByPlaceholderText('Message Family Assistant...');
        await user.type(input, 'Mine is gone');
        await user.keyboard('{Enter}');

        // Our turn gives up and surfaces the marker rather than spinning forever
        // behind the concurrent turn's held-open stream.
        await waitFor(
          () => {
            expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
          },
          { timeout: 10000 }
        );
        expect(streamCalls).toBeLessThanOrEqual(6);
      } finally {
        Object.assign(streamResumeTuning, originalTuning);
      }
    },
    { timeout: 30000 }
  );

  it(
    'gives up when the active-turn check itself keeps failing (no spin)',
    async () => {
      // The held-open active-turn check must NOT count an unverifiable leg as
      // liveness: if /messages keeps failing while a concurrent turn holds the
      // stream open, returning "still live" would spin forever. A failed check
      // returns false, so the loop reaches give-up; the give-up reconcile (which
      // also fails here) then surfaces the marker.
      const originalTuning = { ...streamResumeTuning };
      let streamCalls = 0;
      try {
        streamResumeTuning.livenessMs = 0;
        streamResumeTuning.initialDelayMs = 1;
        streamResumeTuning.maxDelayMs = 2;
        server.use(
          captureTurnId(() => {}, 'web_conv_check_fails'),
          // Both the active-turn check and the give-up reconcile hit /messages;
          // it is down throughout.
          http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
            HttpResponse.json({ detail: 'history unavailable' }, { status: 500 })
          ),
          http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
            streamCalls += 1;
            // A held-open leg that drops (NOT a subscribe failure).
            return sse([
              `event: stream_dropped\ndata: ${JSON.stringify({ reason: 'idle_cut' })}\n\n`,
            ]);
          })
        );

        const user = userEvent.setup();
        await renderChatApp({ waitForReady: true });
        const input = screen.getByPlaceholderText('Message Family Assistant...');
        await user.type(input, 'Check keeps failing');
        await user.keyboard('{Enter}');

        await waitFor(
          () => {
            expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
          },
          { timeout: 10000 }
        );
        expect(streamCalls).toBeLessThanOrEqual(6);
      } finally {
        Object.assign(streamResumeTuning, originalTuning);
      }
    },
    { timeout: 30000 }
  );

  it(
    'surfaces an error when the first leg connects but every resume fails (5xx)',
    async () => {
      // Connecting once does not prove the turn finished: if the connection
      // drops after a tool call and every resume returns 5xx, we cannot confirm
      // the reply was persisted, so the final subscribe_failed must surface an
      // error rather than silently reloading a possibly-still-running turn.
      let ourTurnId = '';
      let streamCalls = 0;
      let messagesFetches = 0;
      server.use(
        captureTurnId((id) => {
          ourTurnId = id;
        }, 'web_conv_resume_5xx'),
        http.get('/api/v1/chat/conversations/:conversationId/messages', () => {
          messagesFetches += 1;
          return HttpResponse.json({ messages: [] });
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          streamCalls += 1;
          if (streamCalls === 1) {
            // Connect: deliver a tool call (seq 1), then drop without turn_ended.
            return sse([
              `event: tool_call\ndata: ${JSON.stringify({
                turn_id: ourTurnId,
                tool_call: {
                  id: 'call_x',
                  type: 'function',
                  function: { name: 'search_notes', arguments: '{}' },
                },
                seq: 1,
              })}\n\n`,
            ]);
          }
          // Every resume fails.
          return HttpResponse.json({ detail: 'upstream unavailable' }, { status: 503 });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(input, 'Connect then resume-fail');
      await user.keyboard('{Enter}');

      await waitFor(
        () => {
          expect(screen.getByText(/couldn't confirm the reply/i)).toBeInTheDocument();
        },
        { timeout: 10000 }
      );
      // We reconciled from history; it had no terminal reply for the turn, so an
      // error was surfaced rather than silently swallowing it.
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
