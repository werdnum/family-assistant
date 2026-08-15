import { render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { server } from '../../test/setup.js';
import SharedConversationPage from '../SharedConversationPage';

function renderSharedPage(token: string) {
  return render(
    <MemoryRouter initialEntries={[`/shared/conversations/${token}`]}>
      <Routes>
        <Route path="/shared/conversations/:token" element={<SharedConversationPage />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('SharedConversationPage', () => {
  it('renders a read-only transcript from the share endpoint', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/share-token/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'user',
              timestamp: '2026-08-10T12:00:00Z',
              content: 'Which gift should I choose?',
            },
            {
              internal_id: '2',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:01Z',
              content: 'The blue one.',
            },
          ],
        })
      )
    );
    renderSharedPage('share-token');

    expect(await screen.findByText('Which gift should I choose?')).toBeInTheDocument();
    expect(screen.getByText('The blue one.')).toBeInTheDocument();
    expect(screen.getByText('Read only')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Message Family Assistant...')).not.toBeInTheDocument();
  });

  it('collapses tool calls and folds their results into the group', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('/api/v1/shared-conversations/tools/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:00Z',
              content: '',
              tool_calls: [
                {
                  id: 'call_1',
                  type: 'function',
                  function: { name: 'get_note', arguments: '{"title":"Gifts"}' },
                },
              ],
            },
            {
              internal_id: '2',
              role: 'tool',
              timestamp: '2026-08-10T12:00:01Z',
              tool_call_id: 'call_1',
              content: 'Raw tool output',
            },
            {
              internal_id: '3',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:02Z',
              content: 'The blue one.',
            },
          ],
        })
      )
    );
    renderSharedPage('tools');

    expect(await screen.findByText('The blue one.')).toBeInTheDocument();

    // The tool result is not dumped into the transcript as its own message.
    expect(screen.queryByText('Raw tool output')).not.toBeInTheDocument();
    expect(screen.queryByText('tool')).not.toBeInTheDocument();

    const content = screen.getByTestId('tool-group-content');
    expect(content).toHaveAttribute('data-state', 'closed');

    await user.click(screen.getByTestId('tool-group-trigger'));

    expect(content).toHaveAttribute('data-state', 'open');
    expect(screen.getByText('get_note')).toBeInTheDocument();
    expect(screen.getByText('{"title":"Gifts"}')).toBeInTheDocument();
    expect(screen.getByText('Raw tool output')).toBeInTheDocument();
  });

  it('summarises a single tool call without mangling the category name', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/summary/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:00Z',
              content: '',
              tool_calls: [
                {
                  id: 'call_1',
                  type: 'function',
                  function: { name: 'search_calendar_events', arguments: '{}' },
                },
              ],
            },
          ],
        })
      )
    );
    renderSharedPage('summary');

    expect(await screen.findByText('1 calendar')).toBeInTheDocument();
  });

  it('collapses a tool result whose call is missing from the transcript', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/orphan/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'tool',
              timestamp: '2026-08-10T12:00:00Z',
              tool_call_id: 'call_missing',
              content: 'Orphaned tool output',
            },
          ],
        })
      )
    );
    renderSharedPage('orphan');

    const content = await screen.findByTestId('tool-group-content');
    expect(content).toHaveAttribute('data-state', 'closed');
    expect(screen.queryByText('Orphaned tool output')).not.toBeInTheDocument();
  });

  it('surfaces attachments produced by a tool alongside the response', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/attachments/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:00Z',
              content: 'Here is the chart.',
              tool_calls: [
                {
                  id: 'call_1',
                  type: 'function',
                  function: { name: 'generate_chart', arguments: '{}' },
                },
              ],
            },
            {
              internal_id: '2',
              role: 'tool',
              timestamp: '2026-08-10T12:00:01Z',
              tool_call_id: 'call_1',
              content: 'chart generated',
              attachments: [
                {
                  attachment_id: 'att_1',
                  name: 'chart.png',
                  mime_type: 'image/png',
                  content_url: '/api/v1/shared-conversations/attachments/attachments/att_1',
                },
              ],
            },
          ],
        })
      )
    );
    renderSharedPage('attachments');

    expect(await screen.findByAltText('chart.png')).toBeInTheDocument();
  });

  it('shows an unavailable state for a revoked link', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/revoked/messages', () =>
        HttpResponse.json({ detail: 'Not found' }, { status: 404 })
      )
    );
    renderSharedPage('revoked');

    expect(await screen.findByText('This shared conversation is unavailable')).toBeInTheDocument();
  });

  it('shows a retryable error for server failures and recovers', async () => {
    const user = userEvent.setup();
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    let attempts = 0;
    server.use(
      http.get('/api/v1/shared-conversations/retry/messages', () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json({ detail: 'Unavailable' }, { status: 503 })
          : HttpResponse.json({
              messages: [
                {
                  internal_id: '3',
                  role: 'assistant',
                  timestamp: '2026-08-10T12:00:00Z',
                  content: 'Recovered transcript',
                },
              ],
            });
      })
    );
    renderSharedPage('retry');

    expect(await screen.findByText('Could not load the shared conversation')).toBeInTheDocument();
    expect(screen.queryByText('This shared conversation is unavailable')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('Recovered transcript')).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it('shows an authentication message for an expired session', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/sign-in/messages', () =>
        HttpResponse.json({ detail: 'Not authenticated' }, { status: 401 })
      )
    );
    renderSharedPage('sign-in');

    expect(await screen.findByText('Sign in to view this conversation')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sign in again' })).toHaveAttribute(
      'href',
      window.location.href
    );
  });
});
