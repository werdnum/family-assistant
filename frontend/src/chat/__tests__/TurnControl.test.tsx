import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

// A controllable SSE stream: the handler emits turn_started and then parks,
// handing its controller to the test so it can drive cancelled / user_input
// events on demand. This keeps the turn "running" (isStreaming true) so the
// Stop button and steer input are mounted.
function installOpenStream(): {
  ready: Promise<ReadableStreamDefaultController<Uint8Array>>;
  turnIdRef: { current: string };
} {
  const encoder = new TextEncoder();
  const turnIdRef = { current: 'mock-turn' };
  let resolveController: (c: ReadableStreamDefaultController<Uint8Array>) => void;
  const ready = new Promise<ReadableStreamDefaultController<Uint8Array>>((resolve) => {
    resolveController = resolve;
  });

  server.use(
    http.post('/api/v1/chat/turns', async ({ request }) => {
      const body = (await request.json()) as { turn_id: string; conversation_id?: string };
      turnIdRef.current = body.turn_id;
      return HttpResponse.json({
        turn_id: body.turn_id,
        conversation_id: body.conversation_id || `web_conv_${Date.now()}`,
        first_seq: 0,
      });
    }),
    http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(
            encoder.encode(
              `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnIdRef.current, seq: 0 })}\n\n`
            )
          );
          // Hand the controller to the test; it stays open until the test
          // enqueues a terminal event and closes it.
          resolveController(controller);
        },
      });
      return new HttpResponse(stream, {
        headers: { 'Content-Type': 'text/event-stream' },
      });
    })
  );

  return { ready, turnIdRef };
}

const enc = new TextEncoder();
const sse = (event: string, data: unknown) =>
  enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

// Generous waits: the full suite runs many test files in parallel, so the async
// chain (SSE event → state update → render) can take well over the 1s default.
const WAIT = { timeout: 15000 } as const;

describe('Web turn control (Stop / Steer)', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
  });

  it(
    'Stop button cancels the running turn server-side and renders a stopped marker',
    async () => {
      const { ready, turnIdRef } = installOpenStream();
      let cancelBody: { conversation_id: string } | null = null;
      let cancelledTurnId = '';
      server.use(
        http.post('/api/v1/chat/turns/:turnId/cancel', async ({ request, params }) => {
          cancelledTurnId = String(params.turnId);
          cancelBody = (await request.json()) as { conversation_id: string };
          return HttpResponse.json({
            turn_id: cancelledTurnId,
            conversation_id: cancelBody.conversation_id,
            status: 'cancelling',
          });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Do something slow');
      await user.keyboard('{Enter}');

      const controller = await ready;

      // Stop button is mounted only while running.
      const stopButton = await screen.findByTestId('stop-button', undefined, WAIT);
      await user.click(stopButton);

      await waitFor(() => {
        expect(cancelledTurnId).toBe(turnIdRef.current);
      }, WAIT);
      expect(cancelBody).not.toBeNull();
      expect(cancelBody!.conversation_id).toBeTruthy();

      // The server then ends the turn as cancelled over SSE; the bubble settles
      // with a stopped marker and no error.
      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'cancelled', seq: 1 })
      );
      controller.close();

      await waitFor(() => {
        expect(screen.getByText('Stopped.')).toBeInTheDocument();
      }, WAIT);
      expect(screen.queryByText(/encountered an error/i)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'Steer input injects a mid-turn message and renders the steering bubble',
    async () => {
      const { ready, turnIdRef } = installOpenStream();
      let steerBody: { conversation_id: string; prompt: string } | null = null;
      server.use(
        http.post('/api/v1/chat/turns/:turnId/steer', async ({ request }) => {
          steerBody = (await request.json()) as { conversation_id: string; prompt: string };
          return HttpResponse.json({
            turn_id: turnIdRef.current,
            conversation_id: steerBody.conversation_id,
            accepted: true,
          });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;

      // Steer input appears only while running.
      const steerInput = await screen.findByTestId('steer-input', undefined, WAIT);
      await user.type(steerInput, 'focus on tomorrow');
      await user.click(screen.getByTestId('steer-button'));

      await waitFor(() => {
        expect(steerBody).not.toBeNull();
      }, WAIT);
      expect(steerBody!.prompt).toBe('focus on tomorrow');
      expect(steerBody!.conversation_id).toBeTruthy();

      // The server echoes the injected message as a user_input event; it should
      // render as a user bubble mid-stream.
      controller.enqueue(
        sse('user_input', { turn_id: turnIdRef.current, content: 'focus on tomorrow', seq: 1 })
      );

      await waitFor(() => {
        expect(screen.getByText('focus on tomorrow')).toBeInTheDocument();
      }, WAIT);

      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 2 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );

  it(
    'steering a finished turn falls back to sending a normal new message',
    async () => {
      const { ready, turnIdRef } = installOpenStream();
      let turnsPosts = 0;
      server.use(
        // Count kickoff POSTs; the fallback sends the steer as a new turn.
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turnsPosts += 1;
          const body = (await request.json()) as { turn_id: string; conversation_id?: string };
          turnIdRef.current = body.turn_id;
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || `web_conv_${Date.now()}`,
            first_seq: 0,
          });
        }),
        http.post('/api/v1/chat/turns/:turnId/steer', () =>
          HttpResponse.json({ detail: 'Turn is not running' }, { status: 409 })
        )
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;
      await waitFor(() => {
        expect(turnsPosts).toBe(1);
      }, WAIT);

      const steerInput = await screen.findByTestId('steer-input', undefined, WAIT);
      await user.type(steerInput, 'do it differently');
      await user.click(screen.getByTestId('steer-button'));

      // The 409 fallback is serialized after the current stream settles, so let
      // the first turn finish; the queued follow-up then fires.
      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 1 })
      );
      controller.close();

      // 409 from steer → the text is sent as a normal follow-up (a 2nd kickoff),
      // not silently lost.
      await waitFor(() => {
        expect(turnsPosts).toBe(2);
      }, WAIT);
    },
    { timeout: 30000 }
  );

  it(
    'recovers an accepted steer that the turn never echoes',
    async () => {
      const { ready, turnIdRef } = installOpenStream();
      let turnsPosts = 0;
      server.use(
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turnsPosts += 1;
          const body = (await request.json()) as { turn_id: string; conversation_id?: string };
          turnIdRef.current = body.turn_id;
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || `web_conv_${Date.now()}`,
            first_seq: 0,
          });
        }),
        // Accept the steer, but the stream below never echoes a user_input for
        // it (simulating a final text-only iteration that never drains it).
        http.post('/api/v1/chat/turns/:turnId/steer', async ({ request }) => {
          const body = (await request.json()) as { conversation_id: string; prompt: string };
          return HttpResponse.json({
            turn_id: turnIdRef.current,
            conversation_id: body.conversation_id,
            accepted: true,
          });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;
      await waitFor(() => {
        expect(turnsPosts).toBe(1);
      }, WAIT);

      const steerInput = await screen.findByTestId('steer-input', undefined, WAIT);
      await user.type(steerInput, 'use the newer plan');
      await user.click(screen.getByTestId('steer-button'));

      // The turn completes WITHOUT echoing the accepted steer; on completion it
      // is recovered as a normal follow-up (a 2nd kickoff) rather than lost.
      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 1 })
      );
      controller.close();

      await waitFor(() => {
        expect(turnsPosts).toBe(2);
      }, WAIT);
    },
    { timeout: 30000 }
  );

  it(
    'retries Stop through the turn-registration race (404 then 200)',
    async () => {
      const { ready, turnIdRef } = installOpenStream();
      let cancelCalls = 0;
      server.use(
        http.post('/api/v1/chat/turns/:turnId/cancel', async ({ request }) => {
          cancelCalls += 1;
          const body = (await request.json()) as { conversation_id: string };
          // First click races the kickoff: the turn isn't registered yet (404).
          if (cancelCalls === 1) {
            return HttpResponse.json({ detail: 'not found' }, { status: 404 });
          }
          return HttpResponse.json({
            turn_id: turnIdRef.current,
            conversation_id: body.conversation_id,
            status: 'cancelling',
          });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Do something slow');
      await user.keyboard('{Enter}');

      const controller = await ready;
      const stopButton = await screen.findByTestId('stop-button', undefined, WAIT);
      await user.click(stopButton);

      // stopTurn retries the 404 and succeeds on the second attempt.
      await waitFor(() => {
        expect(cancelCalls).toBeGreaterThanOrEqual(2);
      }, WAIT);

      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'cancelled', seq: 1 })
      );
      controller.close();

      await waitFor(() => {
        expect(screen.getByText('Stopped.')).toBeInTheDocument();
      }, WAIT);
      // No stop-failure warning, since the retry secured the stop.
      expect(screen.queryByText(/could not confirm the turn/i)).not.toBeInTheDocument();
    },
    { timeout: 30000 }
  );

  it(
    'recovers every accepted steer when the turn never echoes',
    async () => {
      const enc2 = new TextEncoder();
      const turnIdRef = { current: 'mock-turn' };
      let turnsPosts = 0;
      let firstOpened = false;
      let resolveFirst: (c: ReadableStreamDefaultController<Uint8Array>) => void;
      const ready = new Promise<ReadableStreamDefaultController<Uint8Array>>((r) => {
        resolveFirst = r;
      });
      server.use(
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turnsPosts += 1;
          const body = (await request.json()) as { turn_id: string; conversation_id?: string };
          turnIdRef.current = body.turn_id;
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || `web_conv_${Date.now()}`,
            first_seq: 0,
          });
        }),
        http.post('/api/v1/chat/turns/:turnId/steer', async ({ request }) => {
          const body = (await request.json()) as { conversation_id: string };
          return HttpResponse.json({
            turn_id: turnIdRef.current,
            conversation_id: body.conversation_id,
            accepted: true,
          });
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          // The first turn is controllable; follow-up (recovery) turns
          // auto-complete so each queued recovery fires the next sequentially.
          if (!firstOpened) {
            firstOpened = true;
            return new HttpResponse(
              new ReadableStream<Uint8Array>({
                start(controller) {
                  controller.enqueue(
                    enc2.encode(
                      `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnIdRef.current, seq: 0 })}\n\n`
                    )
                  );
                  resolveFirst(controller);
                },
              }),
              { headers: { 'Content-Type': 'text/event-stream' } }
            );
          }
          return new HttpResponse(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(
                  enc2.encode(
                    `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnIdRef.current, seq: 0 })}\n\n`
                  )
                );
                controller.enqueue(
                  enc2.encode(
                    `event: turn_ended\ndata: ${JSON.stringify({ turn_id: turnIdRef.current, status: 'complete', seq: 1 })}\n\n`
                  )
                );
                controller.close();
              },
            }),
            { headers: { 'Content-Type': 'text/event-stream' } }
          );
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;
      await waitFor(() => {
        expect(turnsPosts).toBe(1);
      }, WAIT);

      // Two accepted steers during the same turn.
      const steerInput = await screen.findByTestId('steer-input', undefined, WAIT);
      await user.type(steerInput, 'first steer');
      await user.click(screen.getByTestId('steer-button'));
      await user.clear(steerInput);
      await user.type(steerInput, 'second steer');
      await user.click(screen.getByTestId('steer-button'));

      // The turn ends without echoing either steer; BOTH are recovered as
      // follow-ups (sequentially), so two extra kickoffs fire.
      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 1 })
      );
      controller.close();

      await waitFor(() => {
        expect(turnsPosts).toBe(3);
      }, WAIT);
    },
    { timeout: 30000 }
  );
});
