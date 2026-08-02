import { fireEvent, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import { useStreamingResponse } from '../useStreamingResponse.js';

// A controllable SSE stream: the handler emits turn_started and then parks,
// handing its controller to the test so it can drive cancelled / user_input
// events on demand. This keeps the turn "running" (isStreaming true) so the
// Stop/Steer action is mounted. While running the main composer doubles as the
// steer input, so steering = type into the chat input then click the steer
// action (which replaces Stop once there's text).
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

// installOpenStream serves any conversation, so a stray stream request left over
// from an earlier test can resolve its controller — and the test then enqueues
// events into a stream nothing is reading. This variant hands back only the
// stream belonging to the kickoff it saw, and parks anything else.
function installOwnOpenStream(): {
  ready: Promise<ReadableStreamDefaultController<Uint8Array>>;
  turnIdRef: { current: string };
  turnsPostsRef: { current: number };
} {
  const encoder = new TextEncoder();
  const turnIdRef = { current: 'mock-turn' };
  const conversationIdRef = { current: '' };
  const turnsPostsRef = { current: 0 };
  let resolveController: (c: ReadableStreamDefaultController<Uint8Array>) => void;
  const ready = new Promise<ReadableStreamDefaultController<Uint8Array>>((resolve) => {
    resolveController = resolve;
  });

  server.use(
    http.post('/api/v1/chat/turns', async ({ request }) => {
      turnsPostsRef.current += 1;
      const body = (await request.json()) as { turn_id: string; conversation_id?: string };
      turnIdRef.current = body.turn_id;
      conversationIdRef.current = body.conversation_id || `web_conv_${Date.now()}`;
      return HttpResponse.json({
        turn_id: body.turn_id,
        conversation_id: conversationIdRef.current,
        first_seq: 0,
      });
    }),
    http.get('/api/v1/chat/conversations/:conversationId/stream', ({ params }) => {
      const mine = String(params.conversationId) === conversationIdRef.current;
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          if (!mine) {
            return;
          }
          controller.enqueue(
            encoder.encode(
              `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnIdRef.current, seq: 0 })}\n\n`
            )
          );
          resolveController(controller);
        },
      });
      return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
    })
  );

  return { ready, turnIdRef, turnsPostsRef };
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
    // Reset the URL so a conversation switch in one test (which pushes a
    // ?conversation_id=... URL) can't leak into the next test's initial
    // conversation id.
    window.history.pushState({}, '', '/chat');
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
      let steerBody: { conversation_id: string; prompt: string; input_id: string } | null = null;
      server.use(
        http.post('/api/v1/chat/turns/:turnId/steer', async ({ request }) => {
          steerBody = (await request.json()) as {
            conversation_id: string;
            prompt: string;
            input_id: string;
          };
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

      // While running, the main composer doubles as the steer input and the
      // Stop action becomes Steer once there's text.
      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'focus on tomorrow');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));

      await waitFor(() => {
        expect(steerBody).not.toBeNull();
      }, WAIT);
      expect(steerBody!.prompt).toBe('focus on tomorrow');
      expect(steerBody!.conversation_id).toBeTruthy();

      // The server echoes the injected message as a user_input event, naming the
      // submission it consumed; it should render as a user bubble mid-stream.
      controller.enqueue(
        sse('user_input', {
          turn_id: turnIdRef.current,
          content: 'focus on tomorrow',
          input_id: steerBody!.input_id,
          seq: 1,
        })
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
    'clicking Steer injects mid-turn without also submitting a new turn',
    async () => {
      // The Steer button lives inside the composer form; if it defaults to a
      // submit button, a mouse click both steers and fires the form's submit
      // (a second kickoff POST). It must be type="button" so only the steer
      // endpoint is hit.
      const { ready, turnIdRef } = installOpenStream();
      let turnsPosts = 0;
      let steerPosts = 0;
      let steerInputId = '';
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
          steerPosts += 1;
          const body = (await request.json()) as { conversation_id: string; input_id: string };
          steerInputId = body.input_id;
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

      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'focus on tomorrow');
      const steerButton = await screen.findByTestId('steer-button', undefined, WAIT);
      // The button lives inside the composer form, so it must opt out of the
      // default type="submit" or a click would also submit a new message.
      expect(steerButton).toHaveAttribute('type', 'button');
      await user.click(steerButton);

      await waitFor(() => {
        expect(steerPosts).toBe(1);
      }, WAIT);
      // The composer clears on an accepted steer; give any errant form submit a
      // chance to fire a second kickoff before asserting it never happened.
      await waitFor(() => expect(steerInput).toHaveValue(''), WAIT);
      expect(turnsPosts).toBe(1);

      // Echo the steer as a user_input event so it counts as delivered; without
      // this the un-echoed-steer recovery would re-send it as a fresh turn.
      controller.enqueue(
        sse('user_input', {
          turn_id: turnIdRef.current,
          content: 'focus on tomorrow',
          input_id: steerInputId,
          seq: 1,
        })
      );
      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 2 })
      );
      controller.close();
      expect(turnsPosts).toBe(1);
    },
    { timeout: 30000 }
  );

  it(
    'Enter during IME composition does not steer; a committed Enter does',
    async () => {
      const { ready, turnIdRef } = installOpenStream();
      let steerPosts = 0;
      let steerInputId = '';
      server.use(
        http.post('/api/v1/chat/turns/:turnId/steer', async ({ request }) => {
          steerPosts += 1;
          const body = (await request.json()) as { conversation_id: string; input_id: string };
          steerInputId = body.input_id;
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

      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'focus on tomorrow');

      // Enter while an IME composition is active must NOT steer — it commits the
      // composing text. The composer keeps its text and fires no steer request.
      fireEvent.keyDown(steerInput, { key: 'Enter', isComposing: true });
      await waitFor(() => expect(steerInput).toHaveValue('focus on tomorrow'), WAIT);
      expect(steerPosts).toBe(0);

      // A normal (non-composing) Enter steers as usual.
      fireEvent.keyDown(steerInput, { key: 'Enter' });
      await waitFor(() => {
        expect(steerPosts).toBe(1);
      }, WAIT);

      controller.enqueue(
        sse('user_input', {
          turn_id: turnIdRef.current,
          content: 'focus on tomorrow',
          input_id: steerInputId,
          seq: 1,
        })
      );
      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 2 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );

  it(
    'hides the attachment UI while a turn is running',
    async () => {
      const { ready, turnIdRef } = installOpenStream();
      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      // Idle: the add-attachment affordance is available.
      expect(screen.getByTestId('add-attachment-button')).toBeInTheDocument();

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;

      // Running (steer mode): no attachment button, since a steer is text-only and
      // a picked file would otherwise be silently dropped from the steer.
      await waitFor(() => {
        expect(screen.queryByTestId('add-attachment-button')).not.toBeInTheDocument();
      }, WAIT);

      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 1 })
      );
      controller.close();

      // Idle again: the attachment button returns.
      await waitFor(() => {
        expect(screen.getByTestId('add-attachment-button')).toBeInTheDocument();
      }, WAIT);
    },
    { timeout: 30000 }
  );

  it(
    'clears composer text when switching conversations',
    async () => {
      // The main composer doubles as the steer input and its runtime is shared
      // across conversations, so text left in it (e.g. a half-typed steer for a
      // running turn) must not leak into a newly selected conversation. The clear
      // is driven purely by the conversation-id change, so this exercises it
      // without an active stream.
      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const input = screen.getByTestId('chat-input');
      await user.type(input, 'half-typed steer');
      expect(input).toHaveValue('half-typed steer');

      await user.click(await screen.findByTestId('new-chat-button', undefined, WAIT));
      await waitFor(() => expect(screen.getByTestId('chat-input')).toHaveValue(''), WAIT);
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

      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'do it differently');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));

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
    'abandons an un-echoed accepted steer when the user stops the turn',
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
        http.post('/api/v1/chat/turns/:turnId/steer', async ({ request }) => {
          const body = (await request.json()) as { conversation_id: string };
          return HttpResponse.json({
            turn_id: turnIdRef.current,
            conversation_id: body.conversation_id,
            accepted: true,
          });
        }),
        http.post('/api/v1/chat/turns/:turnId/cancel', async ({ request }) => {
          const body = (await request.json()) as { conversation_id: string };
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
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;
      await waitFor(() => {
        expect(turnsPosts).toBe(1);
      }, WAIT);

      // Steer (accepted, awaiting echo) then Stop before the echo arrives.
      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'changed my mind');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));
      // The accepted steer clears the composer, so the action reverts to Stop.
      await waitFor(() => expect(steerInput).toHaveValue(''), WAIT);
      await user.click(await screen.findByTestId('stop-button', undefined, WAIT));

      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'cancelled', seq: 1 })
      );
      controller.close();

      await waitFor(() => {
        expect(screen.getByText('Stopped.')).toBeInTheDocument();
      }, WAIT);
      // The completion handler ran (Stopped. rendered) and, because the turn was
      // stopped, scheduled no follow-up — so the abandoned steer is not resent.
      expect(turnsPosts).toBe(1);
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

      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'use the newer plan');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));

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
    'retries a steer through the turn-registration race (404 then accepted)',
    async () => {
      const { ready, turnIdRef } = installOpenStream();
      let steerCalls = 0;
      let turnsPosts = 0;
      const steerInputIds: string[] = [];
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
          steerCalls += 1;
          const body = (await request.json()) as { conversation_id: string; input_id: string };
          steerInputIds.push(body.input_id);
          if (steerCalls === 1) {
            return HttpResponse.json({ detail: 'not found' }, { status: 404 });
          }
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
      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'focus on tomorrow');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));

      // The first steer 404s (registration race); steerStream retries and the
      // turn accepts it — no fallback new turn.
      await waitFor(() => {
        expect(steerCalls).toBeGreaterThanOrEqual(2);
      }, WAIT);

      // Both attempts carry the same id: a retry is the same submission, so the
      // echo of either one settles it.
      expect(new Set(steerInputIds).size).toBe(1);
      controller.enqueue(
        sse('user_input', {
          turn_id: turnIdRef.current,
          content: 'focus on tomorrow',
          input_id: steerInputIds[0],
          seq: 1,
        })
      );
      await waitFor(() => {
        expect(screen.getByText('focus on tomorrow')).toBeInTheDocument();
      }, WAIT);
      expect(turnsPosts).toBe(1);

      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 2 })
      );
      controller.close();
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

      // Two accepted steers during the same turn. Each accept clears the
      // composer; wait for that before typing the next so the in-flight clear
      // can't clobber it.
      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'first steer');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));
      await waitFor(() => expect(steerInput).toHaveValue(''), WAIT);
      await user.type(steerInput, 'second steer');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));
      await waitFor(() => expect(steerInput).toHaveValue(''), WAIT);

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

  // A send that arrives while the conversation already has a running turn is
  // refused with 409. The client only gets there having lost track of that turn
  // (its stream dropped, the tab was hidden and resumed), so the message is the
  // steer it meant to send — it must reach the running turn, not raise an error.
  function installConflictingTurn(
    runningTurnId: string,
    runningTurnFirstSeq: number,
    // Stream head when the steer is queued, as the real endpoint reports it.
    // The turn's echo of the adopted prompt is published after this seq.
    queuedAfterSeq = runningTurnFirstSeq
  ): {
    ready: Promise<ReadableStreamDefaultController<Uint8Array>>;
    steerBodies: Array<{ conversation_id: string; prompt: string; input_id: string }>;
    steeredTurnIds: string[];
    streamFromSeqs: Array<string | null>;
  } {
    const encoder = new TextEncoder();
    const steerBodies: Array<{ conversation_id: string; prompt: string; input_id: string }> = [];
    const steeredTurnIds: string[] = [];
    const streamFromSeqs: Array<string | null> = [];
    let resolveController: (c: ReadableStreamDefaultController<Uint8Array>) => void;
    const ready = new Promise<ReadableStreamDefaultController<Uint8Array>>((resolve) => {
      resolveController = resolve;
    });

    server.use(
      http.post('/api/v1/chat/turns', () =>
        HttpResponse.json(
          {
            detail: {
              message: 'This conversation already has a running turn.',
              active_turn_id: runningTurnId,
              active_turn_first_seq: runningTurnFirstSeq,
            },
          },
          { status: 409 }
        )
      ),
      http.post('/api/v1/chat/turns/:turnId/steer', async ({ request, params }) => {
        steeredTurnIds.push(String(params.turnId));
        steerBodies.push(
          (await request.json()) as {
            conversation_id: string;
            prompt: string;
            input_id: string;
          }
        );
        return HttpResponse.json({ accepted: true, queued_after_seq: queuedAfterSeq });
      }),
      http.get('/api/v1/chat/conversations/:conversationId/stream', ({ request }) => {
        streamFromSeqs.push(new URL(request.url).searchParams.get('from_seq'));
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                `event: turn_started\ndata: ${JSON.stringify({
                  turn_id: runningTurnId,
                  seq: runningTurnFirstSeq,
                })}\n\n`
              )
            );
            resolveController(controller);
          },
        });
        return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
      })
    );

    return { ready, steerBodies, steeredTurnIds, streamFromSeqs };
  }

  it(
    'a send refused by a running turn is delivered to that turn and follows it',
    async () => {
      const { ready, steerBodies, steeredTurnIds, streamFromSeqs } = installConflictingTurn(
        'running-turn-1',
        7
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Ah it was a dental x-ray');
      await user.keyboard('{Enter}');

      await waitFor(() => {
        expect(steerBodies).toHaveLength(1);
      }, WAIT);
      expect(steerBodies[0].prompt).toBe('Ah it was a dental x-ray');
      expect(steeredTurnIds[0]).toBe('running-turn-1');

      // Followed from just after the steer was queued (queued_after_seq 7),
      // so the bubble shows what the turn does in response to this message
      // rather than replaying the work it had already done.
      const controller = await ready;
      expect(streamFromSeqs[0]).toBe('8');

      // The running turn echoes the steer back. The composer already rendered
      // this prompt optimistically when it was sent, so the echo must not
      // produce a second copy of it.
      controller.enqueue(
        sse('user_input', {
          turn_id: 'running-turn-1',
          content: 'Ah it was a dental x-ray',
          input_id: steerBodies[0].input_id,
          seq: 8,
        })
      );
      controller.enqueue(
        sse('text', { turn_id: 'running-turn-1', content: 'Got it, dental.', seq: 9 })
      );

      await waitFor(() => {
        expect(screen.getByText('Got it, dental.')).toBeInTheDocument();
      }, WAIT);
      // Scoped to the message bubbles: the prompt also appears in the sidebar
      // as the conversation's last-message preview.
      const promptBubbles = screen
        .getAllByTestId('user-message-content')
        .filter((el) => el.textContent?.includes('Ah it was a dental x-ray'));
      expect(promptBubbles).toHaveLength(1);

      controller.enqueue(
        sse('turn_ended', { turn_id: 'running-turn-1', status: 'complete', seq: 10 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );

  it(
    'a later steer on an adopted turn still renders its own bubble',
    async () => {
      // Only the adopted send's own echo is swallowed; the dedupe must not eat
      // every subsequent user_input on that turn.
      const { ready, steerBodies } = installConflictingTurn('running-turn-2', 3);

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'adopted prompt');
      await user.keyboard('{Enter}');

      const controller = await ready;
      await waitFor(() => expect(steerBodies).toHaveLength(1), WAIT);
      controller.enqueue(
        sse('user_input', {
          turn_id: 'running-turn-2',
          content: 'adopted prompt',
          input_id: steerBodies[0].input_id,
          seq: 4,
        })
      );
      // A second input the client did not submit — another interface's steer, so
      // it carries no id of ours and must render.
      controller.enqueue(
        sse('user_input', { turn_id: 'running-turn-2', content: 'and one more thing', seq: 5 })
      );

      await waitFor(() => {
        expect(screen.getByText('and one more thing')).toBeInTheDocument();
      }, WAIT);

      controller.enqueue(
        sse('turn_ended', { turn_id: 'running-turn-2', status: 'complete', seq: 6 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );

  it(
    'follows an adopted turn from the steer, not from the work it had already done',
    async () => {
      // The adopted turn has usually been running a while. Replaying it from
      // its first event would pour that earlier answer into the bubble the
      // composer just opened for THIS message — showing the previous reply
      // again beneath it, and twice over when history had already rendered it.
      // The turn began at seq 3 but the steer was queued at seq 5.
      const { ready, steerBodies, streamFromSeqs } = installConflictingTurn('running-turn-3', 3, 5);

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'continue');
      await user.keyboard('{Enter}');

      const controller = await ready;
      expect(streamFromSeqs[0]).toBe('6');

      // Everything from here is the turn's response to the adopted message: its
      // echo (swallowed, already rendered optimistically) and the new text.
      controller.enqueue(
        sse('user_input', {
          turn_id: 'running-turn-3',
          content: 'continue',
          input_id: steerBodies[0].input_id,
          seq: 6,
        })
      );
      controller.enqueue(
        sse('text', { turn_id: 'running-turn-3', content: 'Carrying on.', seq: 7 })
      );

      await waitFor(() => {
        expect(screen.getByText('Carrying on.')).toBeInTheDocument();
      }, WAIT);
      const bubbles = screen
        .getAllByTestId('user-message-content')
        .filter((el) => el.textContent?.includes('continue'));
      expect(bubbles).toHaveLength(1);

      controller.enqueue(
        sse('turn_ended', { turn_id: 'running-turn-3', status: 'complete', seq: 8 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );

  it(
    'resends an adopted message the turn ended without ever consuming',
    async () => {
      // The adopted turn can already be streaming its final, tool-free response
      // when the steer lands, in which case the LLM loop breaks before its
      // drain and the message is never seen. Nothing else tracks it — the
      // awaiting-echo list only covers steers the composer sent — so the turn
      // has to end by resending it, or the send is silently lost.
      const kickoffPrompts: string[] = [];
      let turnsPosts = 0;
      let resolveAdopted: (c: ReadableStreamDefaultController<Uint8Array>) => void;
      const adoptedReady = new Promise<ReadableStreamDefaultController<Uint8Array>>((r) => {
        resolveAdopted = r;
      });

      server.use(
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turnsPosts += 1;
          const body = (await request.json()) as {
            turn_id: string;
            conversation_id?: string;
            prompt: string;
          };
          kickoffPrompts.push(body.prompt);
          // Only the first send collides with the running turn; the recovery
          // kickoff finds the conversation free.
          if (turnsPosts === 1) {
            return HttpResponse.json(
              {
                detail: {
                  message: 'This conversation already has a running turn.',
                  active_turn_id: 'running-turn-7',
                  active_turn_first_seq: 2,
                },
              },
              { status: 409 }
            );
          }
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || 'web_conv_unconsumed',
            first_seq: 0,
          });
        }),
        http.post('/api/v1/chat/turns/:turnId/steer', () =>
          HttpResponse.json({ accepted: true, queued_after_seq: 4 })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              resolveAdopted(controller);
            },
          });
          return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'and the dentist too');
      await user.keyboard('{Enter}');

      // The adopted turn finishes without ever echoing the steered prompt.
      const controller = await adoptedReady;
      controller.enqueue(
        sse('turn_ended', { turn_id: 'running-turn-7', status: 'complete', seq: 5 })
      );
      controller.close();

      await waitFor(() => {
        expect(turnsPosts).toBe(2);
      }, WAIT);
      expect(kickoffPrompts[1]).toBe('and the dentist too');
    },
    { timeout: 30000 }
  );

  it(
    'holds back the adopted turn output until it echoes the new prompt',
    async () => {
      // queued_after_seq marks when the steer was queued, not when the turn got
      // to it: the turn keeps finishing the response already in flight. Those
      // events answer the PREVIOUS message, so they must not land in the bubble
      // the composer just opened for this one.
      const { ready, steerBodies } = installConflictingTurn('running-turn-8', 2, 4);

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'what about the dentist');
      await user.keyboard('{Enter}');

      const controller = await ready;
      // Tail of the answer that was already streaming when we adopted the turn.
      controller.enqueue(
        sse('text', { turn_id: 'running-turn-8', content: 'FROM THE OLD ANSWER', seq: 5 })
      );
      // The turn reaches our message, then answers it.
      controller.enqueue(
        sse('user_input', {
          turn_id: 'running-turn-8',
          content: 'what about the dentist',
          input_id: steerBodies[0].input_id,
          seq: 6,
        })
      );
      controller.enqueue(
        sse('text', { turn_id: 'running-turn-8', content: 'Dentist is Thursday.', seq: 7 })
      );

      await waitFor(() => {
        expect(screen.getByText('Dentist is Thursday.')).toBeInTheDocument();
      }, WAIT);
      expect(screen.queryByText(/FROM THE OLD ANSWER/)).toBeNull();

      controller.enqueue(
        sse('turn_ended', { turn_id: 'running-turn-8', status: 'complete', seq: 8 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );

  it(
    'resends an unconsumed adopted message even when the turn fails',
    async () => {
      // A failed turn abandons ordinary steers — resending them just repeats the
      // failure and the user still has them in the thread. The adopted prompt is
      // different: the backend only persists a steer once the loop drains it, so
      // an unconsumed one exists nowhere at all and dropping it loses the send.
      const kickoffPrompts: string[] = [];
      let turnsPosts = 0;
      let resolveAdopted: (c: ReadableStreamDefaultController<Uint8Array>) => void;
      const adoptedReady = new Promise<ReadableStreamDefaultController<Uint8Array>>((r) => {
        resolveAdopted = r;
      });

      server.use(
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turnsPosts += 1;
          const body = (await request.json()) as {
            turn_id: string;
            conversation_id?: string;
            prompt: string;
          };
          kickoffPrompts.push(body.prompt);
          if (turnsPosts === 1) {
            return HttpResponse.json(
              {
                detail: {
                  message: 'This conversation already has a running turn.',
                  active_turn_id: 'running-turn-9',
                  active_turn_first_seq: 2,
                },
              },
              { status: 409 }
            );
          }
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || 'web_conv_failed_adopt',
            first_seq: 0,
          });
        }),
        http.post('/api/v1/chat/turns/:turnId/steer', () =>
          HttpResponse.json({ accepted: true, queued_after_seq: 4 })
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              resolveAdopted(controller);
            },
          });
          return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'and the dentist too');
      await user.keyboard('{Enter}');

      const controller = await adoptedReady;
      controller.enqueue(
        sse('turn_ended', {
          turn_id: 'running-turn-9',
          status: 'failed',
          error: 'The assistant stopped unexpectedly.',
          seq: 5,
        })
      );
      controller.close();

      await waitFor(() => {
        expect(turnsPosts).toBe(2);
      }, WAIT);
      expect(kickoffPrompts[1]).toBe('and the dentist too');
    },
    { timeout: 30000 }
  );

  it(
    'resends an accepted steer the turn never echoed before failing',
    async () => {
      // submitSteer clears the composer on accept, and the backend only
      // persists a steer once the loop drains it and emits the echo. A failed
      // turn that never drained it therefore leaves the client's queue holding
      // the user's only copy — dropping it deletes the message.
      const { ready, turnIdRef } = installOpenStream();
      let turnsPosts = 0;
      const kickoffPrompts: string[] = [];
      server.use(
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turnsPosts += 1;
          const body = (await request.json()) as {
            turn_id: string;
            conversation_id?: string;
            prompt: string;
          };
          turnIdRef.current = body.turn_id;
          kickoffPrompts.push(body.prompt);
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || 'web_conv_steerfail',
            first_seq: 0,
          });
        }),
        http.post('/api/v1/chat/turns/:turnId/steer', () => HttpResponse.json({ accepted: true }))
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;
      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'and book the dentist');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));
      await waitFor(() => expect(steerInput).toHaveValue(''), WAIT);

      // The turn fails without ever echoing the steer.
      controller.enqueue(
        sse('turn_ended', {
          turn_id: turnIdRef.current,
          status: 'failed',
          error: 'The assistant stopped unexpectedly.',
          seq: 1,
        })
      );
      controller.close();

      await waitFor(() => {
        expect(turnsPosts).toBe(2);
      }, WAIT);
      expect(kickoffPrompts[1]).toBe('and book the dentist');
    },
    { timeout: 30000 }
  );

  it(
    'keeps a retrying steer pinned to the turn it was submitted against',
    async () => {
      // The retry loop must not follow the live-turn ref. It moves for reasons
      // unrelated to this steer — another conversation being opened, or a turn
      // ending and the recovery queue starting a new one carrying this very
      // prompt — and following it would deliver the message into the wrong
      // thread or into a turn that is already sending it.
      const steeredTurnIds: string[] = [];
      const { result } = renderHook(() => useStreamingResponse({}));

      server.use(
        http.post('/api/v1/chat/turns/:turnId/steer', ({ params }) => {
          steeredTurnIds.push(String(params.turnId));
          // Retryable, so the loop backs off and re-reads the identity.
          return new HttpResponse(null, { status: 404 });
        }),
        http.post('/api/v1/chat/turns', async ({ request }) => {
          const body = (await request.json()) as { turn_id: string; conversation_id?: string };
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || 'web_conv_other',
            first_seq: 0,
          });
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(sse('turn_started', { turn_id: 'other-turn', seq: 0 }));
            },
          });
          return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
        })
      );

      // A turn is running in conversation A; the steer starts against it.
      void result.current.sendStreamingMessage({
        prompt: 'first',
        conversationId: 'web_conv_A',
        turnId: 'turn-in-A',
      });
      await waitFor(() => expect(result.current.isStreaming).toBe(true), WAIT);
      const steering = result.current.steerStream({ prompt: "A's message" });

      // Mid-backoff the user switches to conversation B and sends there, which
      // repoints the live-turn ref at an unrelated turn.
      await waitFor(() => expect(steeredTurnIds.length).toBeGreaterThan(0), WAIT);
      void result.current.sendStreamingMessage({
        prompt: 'second',
        conversationId: 'web_conv_B',
        turnId: 'turn-in-B',
      });

      // Every attempt went to the turn the steer was typed against, and the
      // exhausted 404s report 'finished' — the caller resends it as a normal
      // message rather than it landing somewhere it doesn't belong.
      await expect(steering).resolves.toBe('finished');
      expect(steeredTurnIds.length).toBeGreaterThan(0);
      expect(new Set(steeredTurnIds)).toEqual(new Set(['turn-in-A']));
      result.current.cancelStream();
    },
    { timeout: 30000 }
  );

  it(
    'does not resend an adopted prompt the user stopped before it was placed',
    async () => {
      // Stop pressed after the kickoff adopted the running turn but before the
      // steer landed: the cancel wins, so /steer answers 409. No stream ever
      // opened, so nothing will report turn_ended(cancelled) — and resending
      // would restart the very interaction the user just ended.
      let releaseSteer: () => void = () => {};
      const steerGate = new Promise<void>((resolve) => {
        releaseSteer = resolve;
      });
      let steerRequested: () => void = () => {};
      const steerStarted = new Promise<void>((resolve) => {
        steerRequested = resolve;
      });

      server.use(
        http.post('/api/v1/chat/turns', () =>
          HttpResponse.json(
            {
              detail: {
                message: 'This conversation already has a running turn.',
                active_turn_id: 'running-turn-14',
                active_turn_first_seq: 2,
              },
            },
            { status: 409 }
          )
        ),
        http.post('/api/v1/chat/turns/:turnId/cancel', () =>
          HttpResponse.json({
            turn_id: 'running-turn-14',
            conversation_id: 'web_conv_stopped_adopt',
            status: 'cancelling',
          })
        ),
        http.post('/api/v1/chat/turns/:turnId/steer', async () => {
          steerRequested();
          await steerGate;
          return HttpResponse.json(
            { detail: 'Turn is not running; start a new turn instead.' },
            { status: 409 }
          );
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          throw new Error('must not subscribe: the turn was cancelled');
        })
      );

      const completions: Array<{ undeliveredPrompt: string | null }> = [];
      const { result } = renderHook(() =>
        useStreamingResponse({
          onComplete: (report: { undeliveredPrompt: string | null }) => completions.push(report),
        })
      );
      const sending = result.current.sendStreamingMessage({
        prompt: 'and the dentist too',
        conversationId: 'web_conv_stopped_adopt',
      });

      await steerStarted;
      await expect(result.current.stopTurn()).resolves.toBe(true);
      releaseSteer();
      await sending;

      // Nothing to recover: the send is abandoned with the turn it joined.
      expect(completions).toHaveLength(1);
      expect(completions[0].undeliveredPrompt).toBeNull();
    },
    { timeout: 30000 }
  );

  it(
    'starts its own turn when the adopted turn is already gone by the steer',
    async () => {
      // The named turn can finish between the kickoff's 409 and the steer. Then
      // the prompt reached nothing: the kickoff was refused, the steer rejected,
      // and the composer already cleared. The conversation is idle now, so it
      // goes out as its own turn rather than being surfaced as a dead end.
      const kickoffPrompts: string[] = [];
      let turnsPosts = 0;

      server.use(
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turnsPosts += 1;
          const body = (await request.json()) as {
            turn_id: string;
            conversation_id?: string;
            prompt: string;
          };
          kickoffPrompts.push(body.prompt);
          if (turnsPosts === 1) {
            return HttpResponse.json(
              {
                detail: {
                  message: 'This conversation already has a running turn.',
                  active_turn_id: 'running-turn-10',
                  active_turn_first_seq: 2,
                },
              },
              { status: 409 }
            );
          }
          return HttpResponse.json({
            turn_id: body.turn_id,
            conversation_id: body.conversation_id || 'web_conv_lostrace',
            first_seq: 0,
          });
        }),
        // The turn finished in the gap: not steerable any more.
        http.post('/api/v1/chat/turns/:turnId/steer', () =>
          HttpResponse.json(
            { detail: 'Turn is not running; start a new turn instead.' },
            { status: 409 }
          )
        ),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                sse('turn_ended', { turn_id: 'new-turn', status: 'complete', seq: 1 })
              );
              controller.close();
            },
          });
          return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'is the dentist still on');
      await user.keyboard('{Enter}');

      await waitFor(() => {
        expect(turnsPosts).toBe(2);
      }, WAIT);
      expect(kickoffPrompts[1]).toBe('is the dentist still on');
    },
    { timeout: 30000 }
  );

  it(
    'hands back the text when an adopted steer fails ambiguously',
    async () => {
      // A 5xx may or may not have queued the steer, so it must not auto-resend.
      // The composer has already cleared and nothing persisted the prompt, so
      // the error has to carry the text or the message is only in a bubble a
      // refresh discards.
      let steers = 0;
      server.use(
        http.post('/api/v1/chat/turns', () =>
          HttpResponse.json(
            {
              detail: {
                message: 'This conversation already has a running turn.',
                active_turn_id: 'running-turn-11',
                active_turn_first_seq: 2,
              },
            },
            { status: 409 }
          )
        ),
        http.post('/api/v1/chat/turns/:turnId/steer', () => {
          steers += 1;
          return new HttpResponse(null, { status: 503 });
        })
      );

      const errors: Error[] = [];
      const { result } = renderHook(() =>
        useStreamingResponse({ onError: (e: Error) => errors.push(e) })
      );
      await result.current.sendStreamingMessage({
        prompt: 'move the dentist to Friday',
        conversationId: 'web_conv_ambiguous',
      });

      expect(steers).toBe(1);
      expect(errors).toHaveLength(1);
      // The text comes back with the error, and it renders as written.
      expect(errors[0].message).toContain('move the dentist to Friday');
      expect((errors[0] as Error & { userFacing?: boolean }).userFacing).toBe(true);
    },
    { timeout: 30000 }
  );

  it(
    'refuses to adopt a running turn when the send carried attachments',
    async () => {
      // Steering carries text only. Adopting here would answer the prompt with
      // its files silently missing, so the send fails and the user keeps them.
      let steers = 0;
      server.use(
        http.post('/api/v1/chat/turns', () =>
          HttpResponse.json(
            {
              detail: {
                message: 'This conversation already has a running turn.',
                active_turn_id: 'running-turn-4',
                active_turn_first_seq: 2,
              },
            },
            { status: 409 }
          )
        ),
        http.post('/api/v1/chat/turns/:turnId/steer', () => {
          steers += 1;
          return HttpResponse.json({ accepted: true, queued_after_seq: 2 });
        }),
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          throw new Error('must not subscribe: the send was never delivered');
        })
      );

      const errors: Error[] = [];
      const { result } = renderHook(() =>
        useStreamingResponse({ onError: (e: Error) => errors.push(e) })
      );
      await result.current.sendStreamingMessage({
        prompt: 'what is in this scan?',
        conversationId: 'web_conv_attach',
        attachments: [{ attachment_id: 'att-1' }],
      });

      expect(steers).toBe(0);
      expect(errors).toHaveLength(1);
      expect(errors[0].message).toMatch(/attachments could not be sent/);
      // Flagged for verbatim rendering: the caller shows this text as written
      // instead of the generic "I encountered an error" line, which would hide
      // the only part that says what to do.
      expect((errors[0] as Error & { userFacing?: boolean }).userFacing).toBe(true);
    },
    { timeout: 30000 }
  );

  it(
    'retargets an in-flight Stop at the turn the refused send adopted',
    async () => {
      // Stop clicked while the kickoff POST is still pending: the POST is then
      // refused and adopts the conversation's running turn, so the cancel must
      // follow. Cancelling the client-generated id we started with would 404
      // forever while the real turn kept running.
      const cancelledTurnIds: string[] = [];
      let releaseKickoff: () => void = () => {};
      const kickoffGate = new Promise<void>((resolve) => {
        releaseKickoff = resolve;
      });
      let resolveStreamController: (c: ReadableStreamDefaultController<Uint8Array>) => void;
      const streamReady = new Promise<ReadableStreamDefaultController<Uint8Array>>((resolve) => {
        resolveStreamController = resolve;
      });

      server.use(
        http.post('/api/v1/chat/turns', async () => {
          await kickoffGate;
          return HttpResponse.json(
            {
              detail: {
                message: 'This conversation already has a running turn.',
                active_turn_id: 'running-turn-5',
                active_turn_first_seq: 2,
              },
            },
            { status: 409 }
          );
        }),
        http.post('/api/v1/chat/turns/:turnId/steer', () =>
          HttpResponse.json({ accepted: true, queued_after_seq: 2 })
        ),
        http.post('/api/v1/chat/turns/:turnId/cancel', ({ params }) => {
          cancelledTurnIds.push(String(params.turnId));
          // Only the adopted turn exists server-side; the rejected kickoff id
          // is the 404 the retry loop is meant to grow out of.
          if (params.turnId !== 'running-turn-5') {
            return new HttpResponse(null, { status: 404 });
          }
          return HttpResponse.json({
            turn_id: 'running-turn-5',
            conversation_id: 'web_conv_stopadopt',
            status: 'cancelling',
          });
        }),
        // The adopted turn stays open, as it would while the cancel is still
        // being retried: a turn that ended first would clear the live identity
        // and leave nothing to retarget.
        http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(sse('turn_started', { turn_id: 'running-turn-5', seq: 2 }));
              resolveStreamController(controller);
            },
          });
          return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
        })
      );

      const { result } = renderHook(() => useStreamingResponse({}));
      const sending = result.current.sendStreamingMessage({
        prompt: 'stop that',
        conversationId: 'web_conv_stopadopt',
      });
      // Stop while the kickoff is still in flight, then let the 409 land.
      const stopping = result.current.stopTurn();
      releaseKickoff();

      await expect(stopping).resolves.toBe(true);
      // The first attempt targeted the id the kickoff was rejected under; the
      // retry followed the adoption instead of 404ing until it gave up.
      expect(cancelledTurnIds[0]).not.toBe('running-turn-5');
      expect(cancelledTurnIds[cancelledTurnIds.length - 1]).toBe('running-turn-5');

      const controller = await streamReady;
      controller.enqueue(
        sse('turn_ended', { turn_id: 'running-turn-5', status: 'cancelled', seq: 3 })
      );
      controller.close();
      await sending;
    },
    { timeout: 30000 }
  );

  it(
    'does not resend a steer the turn consumed before the retry found it finished',
    async () => {
      // The first POST queues the steer but its response is lost; steerStream
      // retries, and by then the turn has drained the message and ended, so the
      // retry gets 409 ('finished'). Falling back to a new turn there would make
      // the assistant act on the same instruction twice.
      const { ready, turnIdRef, turnsPostsRef } = installOwnOpenStream();
      let steerInputId = '';
      let steerPosts = 0;
      let releaseSteer: () => void = () => {};
      const steerGate = new Promise<void>((resolve) => {
        releaseSteer = resolve;
      });
      server.use(
        http.post('/api/v1/chat/turns/:turnId/steer', async ({ request }) => {
          steerPosts += 1;
          if (steerPosts === 1) {
            const body = (await request.json()) as { input_id: string };
            steerInputId = body.input_id;
            await steerGate;
            return new HttpResponse(null, { status: 503 });
          }
          return HttpResponse.json({ detail: 'Turn is not running.' }, { status: 409 });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;
      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'focus on tomorrow');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));

      await waitFor(() => expect(steerInputId).not.toBe(''), WAIT);
      controller.enqueue(
        sse('user_input', {
          turn_id: turnIdRef.current,
          content: 'focus on tomorrow',
          input_id: steerInputId,
          seq: 1,
        })
      );
      await waitFor(() => {
        expect(screen.getByText('focus on tomorrow')).toBeInTheDocument();
      }, WAIT);
      releaseSteer();

      await waitFor(() => expect(steerInput).toHaveValue(''), WAIT);

      // End the turn: a steer treated as undelivered is queued rather than sent
      // immediately, and drains here. Wait for the app to leave the streaming
      // state so that drain has had its chance before counting kickoffs.
      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 2 })
      );
      controller.close();
      await waitFor(() => {
        expect(screen.queryByTestId('stop-button')).toBeNull();
      }, WAIT);
      // Only the original kickoff: the steer was not resent as a new turn.
      expect(turnsPostsRef.current).toBe(1);
    },
    { timeout: 30000 }
  );

  it(
    'still recognises an adopted echo from a backend that sends no input_id',
    async () => {
      // Older backend: the echo carries no id, so it is matched on text and
      // cursor as before. Failing to recognise it would hold back the whole
      // adopted reply and resend the prompt as a second turn.
      const { ready, steerBodies } = installConflictingTurn('running-turn-13', 3, 5);

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'continue');
      await user.keyboard('{Enter}');

      const controller = await ready;
      await waitFor(() => expect(steerBodies).toHaveLength(1), WAIT);
      controller.enqueue(
        sse('user_input', { turn_id: 'running-turn-13', content: 'continue', seq: 6 })
      );
      controller.enqueue(
        sse('text', { turn_id: 'running-turn-13', content: 'Carrying on.', seq: 7 })
      );

      // The reply flows, and the prompt is rendered once.
      await waitFor(() => {
        expect(screen.getByText('Carrying on.')).toBeInTheDocument();
      }, WAIT);
      const bubbles = screen
        .getAllByTestId('user-message-content')
        .filter((el) => el.textContent?.includes('continue'));
      expect(bubbles).toHaveLength(1);

      controller.enqueue(
        sse('turn_ended', { turn_id: 'running-turn-13', status: 'complete', seq: 8 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );

  it(
    'keeps a recovered prompt in the thread when its own kickoff fails',
    async () => {
      // A recovered prompt is resent once; if that send fails outright there is
      // nothing to retry into, so it must at least stay in front of the user —
      // its bubble, and an error saying the send failed — the same as any other
      // failed send. Silently swallowing it would delete the message.
      let turnsPosts = 0;
      const conversationIdRef = { current: '' };
      let resolveAdopted: (c: ReadableStreamDefaultController<Uint8Array>) => void;
      const adoptedReady = new Promise<ReadableStreamDefaultController<Uint8Array>>((r) => {
        resolveAdopted = r;
      });

      server.use(
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turnsPosts += 1;
          const body = (await request.json()) as { conversation_id?: string };
          conversationIdRef.current = body.conversation_id ?? '';
          if (turnsPosts === 1) {
            return HttpResponse.json(
              {
                detail: {
                  message: 'This conversation already has a running turn.',
                  active_turn_id: 'running-turn-12',
                  active_turn_first_seq: 2,
                },
              },
              { status: 409 }
            );
          }
          // The recovery kickoff fails outright.
          return new HttpResponse(null, { status: 500 });
        }),
        http.post('/api/v1/chat/turns/:turnId/steer', () =>
          HttpResponse.json({ accepted: true, queued_after_seq: 4 })
        ),
        // Scoped to this test's conversation: a stray stream request left over
        // from an earlier test would otherwise hand back a controller nothing
        // is reading.
        http.get('/api/v1/chat/conversations/:conversationId/stream', ({ params }) => {
          const mine = String(params.conversationId) === conversationIdRef.current;
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              if (mine) {
                resolveAdopted(controller);
              }
            },
          });
          return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'and the dentist too');
      await user.keyboard('{Enter}');

      // The adopted turn ends without ever consuming the prompt, so it is
      // resent — and that send fails.
      const controller = await adoptedReady;
      controller.enqueue(
        sse('turn_ended', { turn_id: 'running-turn-12', status: 'complete', seq: 5 })
      );
      controller.close();

      await waitFor(() => {
        expect(turnsPosts).toBe(2);
      }, WAIT);
      await waitFor(() => {
        expect(screen.getByText(/encountered an error/i)).toBeInTheDocument();
      }, WAIT);
      const bubbles = screen
        .getAllByTestId('user-message-content')
        .filter((el) => el.textContent?.includes('and the dentist too'));
      expect(bubbles).toHaveLength(1);
    },
    { timeout: 30000 }
  );

  it(
    'clears the composer when a steer whose response was lost is echoed back',
    async () => {
      // The turn can drain the steer and publish its echo while the POST is
      // still failing. Seeing that echo is what lets the composer clear instead
      // of asking the user to resend an instruction the turn already acted on.
      const { ready, turnIdRef } = installOwnOpenStream();
      let steerInputId = '';
      // The POST is held open until the test has seen the echo rendered, so the
      // ordering under test — echo observed, THEN the request fails — is fixed
      // rather than a race against the retry backoff.
      let releaseSteer: () => void = () => {};
      const steerGate = new Promise<void>((resolve) => {
        releaseSteer = resolve;
      });
      server.use(
        http.post('/api/v1/chat/turns/:turnId/steer', async ({ request }) => {
          const body = (await request.json()) as { input_id: string };
          steerInputId = body.input_id;
          await steerGate;
          return new HttpResponse(null, { status: 503 });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;
      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'focus on tomorrow');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));

      await waitFor(() => expect(steerInputId).not.toBe(''), WAIT);
      controller.enqueue(
        sse('user_input', {
          turn_id: turnIdRef.current,
          content: 'focus on tomorrow',
          input_id: steerInputId,
          seq: 1,
        })
      );

      // The echo rendered, so the client has processed it; only now does the
      // request fail.
      await waitFor(() => {
        expect(screen.getByText('focus on tomorrow')).toBeInTheDocument();
      }, WAIT);
      releaseSteer();

      // Delivered after all: the composer clears and no steer error is shown.
      await waitFor(() => expect(steerInput).toHaveValue(''), WAIT);
      expect(screen.queryByText(/Could not steer the assistant/)).toBeNull();

      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 2 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );

  it(
    'keeps the text when the echo belongs to someone else',
    async () => {
      // Same failing POST, but the echo names a submission this client never
      // made — another interface sent the same words. Reading it as delivery
      // would clear the composer and lose the only copy of this message.
      const { ready, turnIdRef } = installOwnOpenStream();
      let steerPosts = 0;
      let releaseSteer: () => void = () => {};
      const steerGate = new Promise<void>((resolve) => {
        releaseSteer = resolve;
      });
      server.use(
        http.post('/api/v1/chat/turns/:turnId/steer', async () => {
          steerPosts += 1;
          await steerGate;
          return new HttpResponse(null, { status: 503 });
        })
      );

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });

      const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
      await user.type(messageInput, 'Plan my week');
      await user.keyboard('{Enter}');

      const controller = await ready;
      const steerInput = screen.getByTestId('chat-input');
      await user.type(steerInput, 'focus on tomorrow');
      await user.click(await screen.findByTestId('steer-button', undefined, WAIT));

      await waitFor(() => expect(steerPosts).toBeGreaterThan(0), WAIT);
      controller.enqueue(
        sse('user_input', {
          turn_id: turnIdRef.current,
          content: 'focus on tomorrow',
          input_id: 'some-other-clients-submission',
          seq: 1,
        })
      );
      await waitFor(() => {
        expect(screen.getByText('focus on tomorrow')).toBeInTheDocument();
      }, WAIT);
      releaseSteer();

      await waitFor(() => {
        expect(screen.getByText(/Could not steer the assistant/)).toBeInTheDocument();
      }, WAIT);
      expect(steerInput).toHaveValue('focus on tomorrow');

      controller.enqueue(
        sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete', seq: 2 })
      );
      controller.close();
    },
    { timeout: 30000 }
  );
});
