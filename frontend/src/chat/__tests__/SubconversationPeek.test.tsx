import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import type { PendingDelegation } from '../usePendingDelegations';

const makeDelegation = (overrides: Partial<PendingDelegation> = {}): PendingDelegation => ({
  delegation_id: 'deleg-1',
  target_profile_id: 'engineer',
  status: 'running',
  request_preview: 'Diagnose why the morning brief did not fire',
  created_at: '2026-11-20T09:00:00+00:00',
  started_at: '2026-11-20T09:00:02+00:00',
  completed_at: null,
  cancel_requested: false,
  children_total: 0,
  children_finished: 0,
  subconversation_id: 'sub-1111',
  ...overrides,
});

function createSseStream() {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const stream = new ReadableStream<Uint8Array>({
    start(ctrl) {
      controller = ctrl;
    },
  });
  return {
    stream,
    send(eventType: string, data: Record<string, unknown>) {
      const frame =
        eventType === 'message'
          ? `data: ${JSON.stringify(data)}\n\n`
          : `event: ${eventType}\ndata: ${JSON.stringify(data)}\n\n`;
      controller?.enqueue(encoder.encode(frame));
    },
    close() {
      controller?.close();
    },
  };
}

describe('subconversation peek sheet', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState(null, '', '/chat?conversation_id=conv-peek');
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('opens a read-only live streaming sheet when clicking a pending delegation chip', async () => {
    const sse = createSseStream();
    let streamRequestedFromSeq: string | null = null;
    let messageReloadCount = 0;

    const runningDelegation = makeDelegation();

    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/pending-delegations', ({ params }) => {
        return HttpResponse.json({
          conversation_id: params.conversationId,
          delegations: params.conversationId === 'conv-peek' ? [runningDelegation] : [],
        });
      }),
      http.get(
        '/api/v1/chat/conversations/:conversationId/subconversations/:subconversationId/messages',
        ({ params }) => {
          messageReloadCount += 1;
          if (messageReloadCount === 1) {
            return HttpResponse.json({
              conversation_id: params.conversationId,
              subconversation_id: params.subconversationId,
              delegation: runningDelegation,
              delegation_error: null,
              request_text: 'Diagnose why the morning brief did not fire',
              child_delegations: [],
              messages: [
                {
                  internal_id: 101,
                  role: 'user',
                  content: 'Diagnose why the morning brief did not fire',
                  timestamp: '2026-11-20T09:00:02+00:00',
                  turn_id: 'sub-turn-1',
                },
              ],
              count: 1,
              total_messages: 1,
              has_more_before: false,
              has_more_after: false,
              active_turns: [
                {
                  turn_id: 'sub-turn-1',
                  started_at: '2026-11-20T09:00:02+00:00',
                  first_seq: 0,
                  latest_seq: 2,
                  status: 'running',
                },
              ],
            });
          }
          return HttpResponse.json({
            conversation_id: params.conversationId,
            subconversation_id: params.subconversationId,
            delegation: {
              ...runningDelegation,
              status: 'completed',
              completed_at: '2026-11-20T09:01:00+00:00',
            },
            delegation_error: null,
            request_text: 'Diagnose why the morning brief did not fire',
            child_delegations: [],
            messages: [
              {
                internal_id: 101,
                role: 'user',
                content: 'Diagnose why the morning brief did not fire',
                timestamp: '2026-11-20T09:00:02+00:00',
                turn_id: 'sub-turn-1',
              },
              {
                internal_id: 102,
                role: 'assistant',
                content: 'Found the cause: the cron schedule was paused.',
                timestamp: '2026-11-20T09:01:00+00:00',
                turn_id: 'sub-turn-1',
                processing_profile_id: 'engineer',
              },
            ],
            count: 2,
            total_messages: 2,
            has_more_before: false,
            has_more_after: false,
            active_turns: [],
          });
        }
      ),
      http.get(
        '/api/v1/chat/conversations/:conversationId/subconversations/:subconversationId/stream',
        ({ request }) => {
          streamRequestedFromSeq = new URL(request.url).searchParams.get('from_seq');
          return new HttpResponse(sse.stream, {
            headers: { 'Content-Type': 'text/event-stream' },
          });
        }
      )
    );

    await renderChatApp({
      waitForReady: true,
      initialEntries: ['/chat?conversation_id=conv-peek'],
    });

    const chip = await screen.findByTestId('pending-delegation');
    expect(chip.tagName).toBe('BUTTON');
    fireEvent.click(chip);

    const sheet = await screen.findByTestId('subconversation-peek-sheet');
    expect(within(sheet).getByTestId('subconversation-peek-readonly-badge')).toHaveTextContent(
      'Read-only'
    );
    // Read-only view must NOT render a chat composer input inside the sheet.
    expect(within(sheet).queryByTestId('chat-input')).not.toBeInTheDocument();

    // User prompt from persisted history is visible.
    await waitFor(() => {
      expect(within(sheet).getByText('Diagnose why the morning brief did not fire')).toBeVisible();
    });
    expect(streamRequestedFromSeq).toBe('0');

    // Push live streaming tokens + tool call over SSE.
    sse.send('turn_started', { seq: 0, turn_id: 'sub-turn-1' });
    sse.send('message', { seq: 1, turn_id: 'sub-turn-1', content: 'Checking error logs… ' });
    sse.send('message', {
      seq: 2,
      turn_id: 'sub-turn-1',
      tool_call: {
        id: 'tc-logs',
        type: 'function',
        function: { name: 'read_error_logs', arguments: '{"limit":5}' },
      },
    });
    sse.send('message', {
      seq: 3,
      turn_id: 'sub-turn-1',
      tool_call_id: 'tc-logs',
      result: 'No recent errors',
      outcome: 'succeeded',
    });
    sse.send('message', {
      seq: 4,
      turn_id: 'sub-turn-1',
      content: 'Found the cause: the cron schedule was paused.',
    });

    await waitFor(() => {
      expect(
        within(sheet).getByText(/Found the cause: the cron schedule was paused\./)
      ).toBeVisible();
    });
    expect(within(sheet).getByTestId('subconversation-peek-status-bar')).toHaveTextContent(
      'Streaming live'
    );

    // End the turn and verify it reloads persisted history and transitions to Completed.
    sse.send('turn_ended', { seq: 5, turn_id: 'sub-turn-1', status: 'complete' });
    sse.close();

    await waitFor(() => {
      expect(within(sheet).getByTestId('subconversation-peek-status-badge')).toHaveTextContent(
        'Completed'
      );
    });
    expect(messageReloadCount).toBeGreaterThanOrEqual(2);
  });

  it('supports drilling into nested child delegations and navigating back', async () => {
    const parentDelegation = makeDelegation({
      delegation_id: 'council-1',
      target_profile_id: 'council',
      status: 'awaiting_children',
      children_total: 1,
      children_finished: 0,
      subconversation_id: 'sub-council',
    });
    const childDelegation = makeDelegation({
      delegation_id: 'member-1',
      target_profile_id: 'research',
      status: 'running',
      request_preview: 'Check option A',
      subconversation_id: 'sub-member-1',
    });

    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/pending-delegations', ({ params }) =>
        HttpResponse.json({
          conversation_id: params.conversationId,
          delegations: params.conversationId === 'conv-peek' ? [parentDelegation] : [],
        })
      ),
      http.get(
        '/api/v1/chat/conversations/:conversationId/subconversations/:subconversationId/messages',
        ({ params }) => {
          const subId = String(params.subconversationId);
          if (subId === 'sub-council') {
            return HttpResponse.json({
              conversation_id: params.conversationId,
              subconversation_id: subId,
              delegation: parentDelegation,
              delegation_error: null,
              request_text: 'Convene the council',
              child_delegations: [childDelegation],
              messages: [
                {
                  internal_id: 201,
                  role: 'user',
                  content: 'Convene the council',
                  timestamp: '2026-11-20T09:00:02+00:00',
                },
              ],
              count: 1,
              total_messages: 1,
              has_more_before: false,
              has_more_after: false,
              active_turns: [],
            });
          }
          return HttpResponse.json({
            conversation_id: params.conversationId,
            subconversation_id: subId,
            delegation: { ...childDelegation, status: 'completed' },
            delegation_error: null,
            request_text: 'Check option A',
            child_delegations: [],
            messages: [
              {
                internal_id: 301,
                role: 'assistant',
                content: 'Option A looks great.',
                timestamp: '2026-11-20T09:00:10+00:00',
              },
            ],
            count: 1,
            total_messages: 1,
            has_more_before: false,
            has_more_after: false,
            active_turns: [],
          });
        }
      )
    );

    await renderChatApp({
      waitForReady: true,
      initialEntries: ['/chat?conversation_id=conv-peek'],
    });

    fireEvent.click(await screen.findByTestId('pending-delegation'));
    const sheet = await screen.findByTestId('subconversation-peek-sheet');

    // Child delegation chip appears inside the peek sheet footer.
    const childChip = await within(sheet).findByTestId('pending-delegation');
    expect(childChip).toHaveTextContent('Research');
    fireEvent.click(childChip);

    await waitFor(() => {
      expect(within(sheet).getByText('Option A looks great.')).toBeVisible();
    });

    // Back button pops back to parent council subconversation.
    fireEvent.click(within(sheet).getByTestId('subconversation-peek-back'));
    await waitFor(() => {
      expect(within(sheet).getByText('Convene the council')).toBeVisible();
    });
  });
});
