import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

interface OpenStream {
  ready: Promise<ReadableStreamDefaultController<Uint8Array>>;
  turnIdRef: { current: string };
  conversationIdRef: { current: string };
}

// Serves the send-and-watch stream for the turn this test kicks off and parks it
// after turn_started, handing the controller to the test so each SSE event is
// delivered only when the test enqueues it. Streams for any other conversation
// (e.g. one left over from an earlier test) stay parked and never resolve it.
function installOpenStream(): OpenStream {
  const encoder = new TextEncoder();
  const turnIdRef = { current: '' };
  const conversationIdRef = { current: '' };
  let resolveController: (c: ReadableStreamDefaultController<Uint8Array>) => void;
  const ready = new Promise<ReadableStreamDefaultController<Uint8Array>>((resolve) => {
    resolveController = resolve;
  });

  server.use(
    http.post('/api/v1/chat/turns', async ({ request }) => {
      const body = (await request.json()) as { turn_id: string; conversation_id?: string };
      turnIdRef.current = body.turn_id;
      conversationIdRef.current = body.conversation_id || `web_conv_confirm_${body.turn_id}`;
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

  return { ready, turnIdRef, conversationIdRef };
}

function installConfirmToolCapture(): Array<Record<string, unknown>> {
  const bodies: Array<Record<string, unknown>> = [];
  server.use(
    http.post('/api/v1/chat/confirm_tool', async ({ request }) => {
      bodies.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json({ success: true, message: 'Tool execution decided' });
    })
  );
  return bodies;
}

const enc = new TextEncoder();
const sse = (event: string, data: unknown): Uint8Array =>
  enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

const WAIT = { timeout: 15000 } as const;

const NOTE_ARGS = { title: 'Groceries', content: 'Milk and eggs' };
const CONFIRMATION_PROMPT = 'Add the note "Groceries"?';

function toolCallEvent(turnId: string, toolCallId: string): Uint8Array {
  return sse('tool_call', {
    turn_id: turnId,
    tool_call: {
      id: toolCallId,
      function: { name: 'add_or_update_note', arguments: JSON.stringify(NOTE_ARGS) },
    },
  });
}

function confirmationRequestEvent(turnId: string, toolCallId: string): Uint8Array {
  return sse('tool_confirmation_request', {
    turn_id: turnId,
    request_id: 'req-1',
    tool_name: 'add_or_update_note',
    tool_call_id: toolCallId,
    confirmation_prompt: CONFIRMATION_PROMPT,
    timeout_seconds: 30,
    args: NOTE_ARGS,
  });
}

async function sendMessage(user: ReturnType<typeof userEvent.setup>, text: string): Promise<void> {
  await user.type(screen.getByPlaceholderText('Message Family Assistant...'), text);
  await user.keyboard('{Enter}');
}

describe('ToolWithConfirmation', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.pushState({}, '', '/chat');
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    { button: 'Approve', approved: true },
    { button: 'Reject', approved: false },
  ])(
    'shows the confirmation inline on the tool call and sends the decision when $button is clicked',
    async ({ button, approved }) => {
      const { ready, turnIdRef, conversationIdRef } = installOpenStream();
      const confirmBodies = installConfirmToolCapture();

      const user = userEvent.setup();
      await renderChatApp({ waitForReady: true });
      await sendMessage(user, 'Add a note about groceries');

      const controller = await ready;
      controller.enqueue(toolCallEvent(turnIdRef.current, 'call-1'));
      controller.enqueue(confirmationRequestEvent(turnIdRef.current, 'call-1'));

      expect(await screen.findByText('Confirmation Required:', undefined, WAIT)).toBeVisible();
      const toolCall = screen.getByTestId('tool-call');
      expect(within(toolCall).getByText('Groceries')).toBeInTheDocument();
      expect(screen.getByText(CONFIRMATION_PROMPT)).toBeInTheDocument();
      expect(screen.getByTestId('tool-group-content')).toHaveAttribute('data-state', 'open');
      expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled();
      expect(screen.queryByTestId('pending-confirmations-tray')).not.toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: button }));

      await waitFor(() => {
        expect(confirmBodies).toEqual([
          { request_id: 'req-1', approved, conversation_id: conversationIdRef.current },
        ]);
      }, WAIT);
      expect(conversationIdRef.current).not.toBe('');
      await waitFor(() => {
        expect(screen.queryByText('Confirmation Required:')).not.toBeInTheDocument();
      }, WAIT);
      expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument();

      controller.enqueue(sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete' }));
      controller.close();
    },
    30000
  );

  it('keeps the card and announces an error when the decision fails, then retries', async () => {
    const { ready, turnIdRef } = installOpenStream();
    let failNext = true;
    const confirmBodies: Array<Record<string, unknown>> = [];
    server.use(
      http.post('/api/v1/chat/confirm_tool', async ({ request }) => {
        confirmBodies.push((await request.json()) as Record<string, unknown>);
        if (failNext) {
          failNext = false;
          return HttpResponse.json({ detail: 'unavailable' }, { status: 503 });
        }
        return HttpResponse.json({ success: true, message: 'Tool execution decided' });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });
    await sendMessage(user, 'Add a note about groceries');

    const controller = await ready;
    controller.enqueue(toolCallEvent(turnIdRef.current, 'call-1'));
    controller.enqueue(confirmationRequestEvent(turnIdRef.current, 'call-1'));

    await screen.findByText('Confirmation Required:', undefined, WAIT);
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    const alert = await screen.findByRole('alert', undefined, WAIT);
    expect(alert).toHaveTextContent('Could not send this decision. Try again.');
    expect(screen.getByText('Confirmation Required:')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => {
      expect(screen.queryByText('Confirmation Required:')).not.toBeInTheDocument();
    }, WAIT);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(confirmBodies).toHaveLength(2);

    controller.enqueue(sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete' }));
    controller.close();
  }, 30000);

  it('counts down the confirmation timeout and shows Expired once it lapses', async () => {
    const { ready, turnIdRef } = installOpenStream();

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });
    await sendMessage(user, 'Add a note about groceries');

    const controller = await ready;
    controller.enqueue(toolCallEvent(turnIdRef.current, 'call-1'));
    await screen.findByTestId('tool-call', undefined, WAIT);

    vi.useFakeTimers({ toFake: ['Date'] });
    controller.enqueue(confirmationRequestEvent(turnIdRef.current, 'call-1'));

    expect(await screen.findByText('Expires in 30s', undefined, WAIT)).toBeInTheDocument();

    vi.advanceTimersByTime(10_000);
    expect(await screen.findByText('Expires in 20s', undefined, WAIT)).toBeInTheDocument();

    vi.advanceTimersByTime(20_000);
    expect(await screen.findByText('Expired', undefined, WAIT)).toBeInTheDocument();
    expect(screen.queryByText(/Expires in/)).not.toBeInTheDocument();

    vi.useRealTimers();
    controller.enqueue(sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete' }));
    controller.close();
  }, 30000);

  it('expands the tool group while the tool runs and collapses it once the result arrives', async () => {
    const { ready, turnIdRef } = installOpenStream();

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });
    await sendMessage(user, 'Please add a note');

    const controller = await ready;
    controller.enqueue(toolCallEvent(turnIdRef.current, 'call-status-test'));

    const toolCall = await screen.findByTestId('tool-call', undefined, WAIT);
    expect(within(toolCall).getByText('Groceries')).toBeInTheDocument();
    expect(screen.getByTestId('tool-group-content')).toHaveAttribute('data-state', 'open');
    expect(screen.queryByText('Note added successfully')).not.toBeInTheDocument();

    controller.enqueue(
      sse('tool_result', {
        turn_id: turnIdRef.current,
        tool_call_id: 'call-status-test',
        result: 'Note added successfully',
      })
    );
    controller.enqueue(sse('text', { turn_id: turnIdRef.current, content: 'Done!' }));
    controller.enqueue(sse('turn_ended', { turn_id: turnIdRef.current, status: 'complete' }));
    controller.close();

    expect(await screen.findByText('Done!', undefined, WAIT)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId('tool-group-content')).toHaveAttribute('data-state', 'closed');
    }, WAIT);
    expect(screen.queryByText('Note added successfully')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('tool-group-trigger'));

    expect(await screen.findByText('Note added successfully', undefined, WAIT)).toBeInTheDocument();
  }, 30000);
});
