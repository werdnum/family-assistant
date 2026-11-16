import { screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import type { ReviewRejectionNotice } from '../types';

const CONVERSATION = 'conv-review-rejection';
const CARD_TEXT = 'Script review stopped “Porch light” from running.';

function serveCard(notice: ReviewRejectionNotice) {
  const rows = [
    {
      internal_id: 1,
      turn_id: null,
      role: 'assistant',
      content: CARD_TEXT,
      timestamp: '2026-11-16T09:00:00Z',
      review_rejection: notice,
    },
  ];
  server.use(
    http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) =>
      HttpResponse.json({
        conversation_id: params.conversationId,
        messages: params.conversationId === CONVERSATION ? rows : [],
        count: rows.length,
        total_messages: rows.length,
        has_more_before: false,
        has_more_after: false,
        active_turns: [],
      })
    )
  );
}

async function openCard() {
  await renderChatApp({
    waitForReady: true,
    initialEntries: [`/chat?conversation_id=${CONVERSATION}`],
  });
  return within(await screen.findByTestId('review-rejection-card'));
}

describe('review rejection card', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState(null, '', `/chat?conversation_id=${CONVERSATION}`);
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('says why the automation did not run and links each artifact to its review', async () => {
    serveCard({
      stage: 'firing',
      automation_name: 'Porch light',
      reason: 'The script sends calendar data to an address it reads from an email.',
      artifacts: [
        { kind: 'event', id: 3, name: 'Porch light', review_url: '/artifacts?kind=event&id=3' },
        {
          kind: 'script',
          id: 9,
          name: 'notify_porch',
          review_url: '/artifacts?kind=script&id=9',
        },
      ],
    });
    const card = await openCard();
    expect(card.getByText(/Script review stopped .Porch light. from running/)).toBeInTheDocument();
    expect(card.getByText(/address it reads from an email/)).toBeInTheDocument();
    expect(card.getByRole('link', { name: 'Review “Porch light”' })).toHaveAttribute(
      'href',
      '/artifacts?kind=event&id=3'
    );
    expect(card.getByRole('link', { name: 'Review “notify_porch”' })).toHaveAttribute(
      'href',
      '/artifacts?kind=script&id=9'
    );
    // The card replaces the row's text rather than repeating it as a reply.
    expect(screen.queryByText(CARD_TEXT)).not.toBeInTheDocument();
  });

  it('says so when nothing can be reviewed', async () => {
    serveCard({
      stage: 'firing',
      automation_name: 'One-off check',
      reason: 'Not aligned.',
      artifacts: [],
    });
    const card = await openCard();
    expect(card.queryByRole('link')).not.toBeInTheDocument();
    expect(card.getByText(/Nothing here can be reviewed/)).toBeInTheDocument();
  });

  it('says an automation was not approved as it was saved', async () => {
    serveCard({
      stage: 'admission',
      automation_name: 'Porch light',
      reason: 'The script was written from an email it quotes.',
      artifacts: [
        { kind: 'script', id: 9, name: 'porch_light', review_url: '/artifacts?kind=script&id=9' },
      ],
    });
    const card = await openCard();
    expect(
      card.getByText(/Script review didn.t approve .Porch light. when it was saved/)
    ).toBeInTheDocument();
    expect(card.getByRole('link', { name: 'Review \u201cporch_light\u201d' })).toHaveAttribute(
      'href',
      '/artifacts?kind=script&id=9'
    );
  });
});
