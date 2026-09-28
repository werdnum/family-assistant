import { render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import {
  delegationStatusLabel,
  formatStartedAgo,
  PendingDelegationsChip,
} from '../PendingDelegationsChip';
import type { PendingDelegation } from '../usePendingDelegations';

const delegation = (overrides: Partial<PendingDelegation> = {}): PendingDelegation => ({
  delegation_id: 'deleg-1',
  target_profile_id: 'research',
  status: 'running',
  request_preview: 'Find the best hiking trails near Hobart',
  created_at: '2026-09-28T01:00:00+00:00',
  started_at: '2026-09-28T01:00:05+00:00',
  completed_at: null,
  cancel_requested: false,
  children_total: 0,
  children_finished: 0,
  ...overrides,
});

describe('delegationStatusLabel', () => {
  it.each([
    [{ status: 'queued' }, 'Queued'],
    [{ status: 'running' }, 'Working on it'],
    [{ status: 'awaiting_children', children_total: 3, children_finished: 1 }, '1 of 3 done'],
    [{ status: 'completed' }, 'Finishing up'],
    [{ status: 'failed' }, 'Finishing up'],
    [{ status: 'running', cancel_requested: true }, 'Cancelling'],
  ] as const)('%o is labelled %s', (overrides, label) => {
    expect(delegationStatusLabel(delegation(overrides))).toBe(label);
  });
});

describe('formatStartedAgo', () => {
  const start = Date.parse('2026-09-28T01:00:00Z');
  it.each([
    [30_000, 'started just now'],
    [2 * 60_000, 'started 2 min ago'],
    [125 * 60_000, 'started 2 h ago'],
  ])('%d ms later reads %s', (elapsed, text) => {
    expect(formatStartedAgo('2026-09-28T01:00:00+00:00', start + elapsed)).toBe(text);
  });
});

describe('PendingDelegationsChip', () => {
  it('renders nothing when no work is pending', () => {
    const { container } = render(<PendingDelegationsChip delegations={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('names the profile and keeps the request as a tooltip', () => {
    render(
      <PendingDelegationsChip delegations={[delegation({ target_profile_id: 'complex_tasks' })]} />
    );
    const chip = screen.getByTestId('pending-delegation');
    expect(chip).toHaveTextContent('Complex_tasks · Working on it');
    expect(chip).toHaveAttribute('title', 'Find the best hiking trails near Hobart');
  });
});

describe('pending delegations in the chat', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState(null, '', '/chat?conversation_id=conv-pending');
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('shows the open conversation’s background work above the composer', async () => {
    const requested: string[] = [];
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/pending-delegations', ({ params }) => {
        requested.push(String(params.conversationId));
        return HttpResponse.json({
          conversation_id: params.conversationId,
          delegations:
            params.conversationId === 'conv-pending'
              ? [
                  delegation({
                    status: 'awaiting_children',
                    target_profile_id: 'council',
                    children_total: 3,
                    children_finished: 2,
                  }),
                ]
              : [],
        });
      })
    );

    await renderChatApp({
      waitForReady: true,
      initialEntries: ['/chat?conversation_id=conv-pending'],
    });

    const tray = await screen.findByTestId('pending-delegations');
    expect(within(tray).getByTestId('pending-delegation')).toHaveTextContent(
      'Council · 2 of 3 done'
    );
    expect(requested).toContain('conv-pending');
    // Sits with the composer so it stays visible however far the thread scrolls.
    expect(screen.getByTestId('composer-container')).toContainElement(tray);
  });

  it('shows nothing when the conversation is not waiting on anything', async () => {
    let fetched = false;
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/pending-delegations', ({ params }) => {
        fetched = true;
        return HttpResponse.json({ conversation_id: params.conversationId, delegations: [] });
      })
    );

    await renderChatApp({
      waitForReady: true,
      initialEntries: ['/chat?conversation_id=conv-pending'],
    });

    await waitFor(() => expect(fetched).toBe(true));
    expect(screen.queryByTestId('pending-delegations')).not.toBeInTheDocument();
  });
});
