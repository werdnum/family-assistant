import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { MemoryRouter, useNavigate } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import EventsList from '../../pages/Events/components/EventsList';
import ConversationsList from '../../pages/History/components/ConversationsList';
import TasksList from '../../tasks/components/TasksList';
import { server } from '../../test/setup';

const cases = [
  {
    name: 'tasks',
    Component: TasksList,
    endpoint: '/api/tasks/',
    filteredURL: '/tasks?task_type=filtered',
    initialURL: '/tasks',
    isFiltered: (params) => params.has('task_type'),
    retryLabel: 'Retry',
    marker: 'Task ID: fresh tasks',
    result: (fresh) => ({
      tasks: [
        {
          id: fresh ? 2 : 1,
          task_id: `${fresh ? 'fresh' : 'stale'} tasks`,
          task_type: 'work',
          status: 'done',
          payload: {},
        },
      ],
    }),
  },
  {
    name: 'history',
    Component: ConversationsList,
    endpoint: '/api/v1/chat/conversations',
    filteredURL: '/history?conversation_id=filtered',
    initialURL: '/history',
    isFiltered: (params) => params.has('conversation_id'),
    retryLabel: 'Try again',
    marker: 'fresh history',
    result: (fresh) => ({
      conversations: [
        {
          conversation_id: fresh ? 'new' : 'old',
          last_message: `${fresh ? 'fresh' : 'stale'} history`,
          message_count: 1,
        },
      ],
      count: 1,
    }),
  },
  {
    name: 'events',
    Component: EventsList,
    endpoint: '/api/events',
    filteredURL: '/events?hours=1',
    initialURL: '/events',
    isFiltered: (params) => params.get('hours') === '1',
    retryLabel: 'Try again',
    marker: 'fresh events',
    result: (fresh) => ({
      events: [
        {
          event_id: fresh ? 2 : 1,
          source_id: 'webhook',
          event_data: { title: `${fresh ? 'fresh' : 'stale'} events` },
        },
      ],
      total: 1,
    }),
  },
];

function BackButton() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      Back to earlier filters
    </button>
  );
}

describe('list request retries', () => {
  it.each(cases)(
    'aborts a $name retry when URL filters change',
    async ({
      Component,
      endpoint,
      filteredURL,
      initialURL,
      isFiltered,
      retryLabel,
      marker,
      result,
    }) => {
      let releaseRetry;
      let releaseFiltered;
      let retryReturned;
      const retryGate = new Promise((resolve) => {
        releaseRetry = resolve;
      });
      const filteredGate = new Promise((resolve) => {
        releaseFiltered = resolve;
      });
      const retryFinished = new Promise((resolve) => {
        retryReturned = resolve;
      });
      let attempts = 0;
      let retryStarted = false;
      let filteredStarted = false;
      server.use(
        http.get(endpoint, async ({ request }) => {
          const params = new globalThis.URL(request.url).searchParams;
          if (params.get('limit') === '1000') {
            return HttpResponse.json({ tasks: [] });
          }
          if (isFiltered(params)) {
            filteredStarted = true;
            await filteredGate;
            return HttpResponse.json(result(true));
          }
          attempts += 1;
          if (attempts === 1) {
            return HttpResponse.json({}, { status: 503 });
          }
          retryStarted = true;
          await retryGate;
          retryReturned();
          return HttpResponse.json(result(false));
        })
      );
      const user = userEvent.setup();
      render(
        <MemoryRouter initialEntries={[filteredURL, initialURL]} initialIndex={1}>
          <BackButton />
          <Component />
        </MemoryRouter>
      );
      try {
        await screen.findByRole('alert');
        await user.click(screen.getByRole('button', { name: retryLabel, exact: true }));
        await waitFor(() => expect(retryStarted).toBe(true));
        await user.click(screen.getByRole('button', { name: 'Back to earlier filters' }));
        await waitFor(() => expect(filteredStarted).toBe(true));
        await act(async () => {
          releaseRetry();
          await retryFinished;
        });
        expect(screen.getByRole('status')).toHaveTextContent('Loading');
        releaseFiltered();
        expect(await screen.findByText(marker)).toBeInTheDocument();
        expect(screen.queryByText(/stale (tasks|history|events)/)).not.toBeInTheDocument();
        expect(screen.queryByRole('status')).not.toBeInTheDocument();
      } finally {
        releaseRetry();
        releaseFiltered();
      }
    }
  );
});
