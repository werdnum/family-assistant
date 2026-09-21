import { render, screen, waitFor } from '@testing-library/react';
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

  it('merges a multi-iteration agentic turn into one group', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('/api/v1/shared-conversations/agentic/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:00Z',
              content: '',
              tool_calls: [
                { id: 'call_1', type: 'function', function: { name: 'get_note', arguments: '{}' } },
              ],
            },
            {
              internal_id: '2',
              role: 'tool',
              timestamp: '2026-08-10T12:00:01Z',
              tool_call_id: 'call_1',
              content: 'note body',
            },
            {
              internal_id: '3',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:02Z',
              content: '',
              tool_calls: [
                {
                  id: 'call_2',
                  type: 'function',
                  function: { name: 'list_notes', arguments: '{}' },
                },
              ],
            },
            {
              internal_id: '4',
              role: 'tool',
              timestamp: '2026-08-10T12:00:03Z',
              tool_call_id: 'call_2',
              content: 'note list',
            },
            {
              internal_id: '5',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:04Z',
              content: 'Done.',
            },
          ],
        })
      )
    );
    renderSharedPage('agentic');

    expect(await screen.findByText('Done.')).toBeInTheDocument();

    // One turn reads as one box, not one per LLM iteration.
    expect(screen.getAllByTestId('tool-group')).toHaveLength(1);
    expect(screen.getByText('2 notes')).toBeInTheDocument();

    await user.click(screen.getByTestId('tool-group-trigger'));

    expect(screen.getByText('get_note')).toBeInTheDocument();
    expect(screen.getByText('list_notes')).toBeInTheDocument();
  });

  it('keeps assistant text as a boundary between tool groups', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/boundary/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:00Z',
              content: '',
              tool_calls: [
                { id: 'call_1', type: 'function', function: { name: 'get_note', arguments: '{}' } },
              ],
            },
            {
              internal_id: '2',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:01Z',
              content: 'Checking your calendar next.',
            },
            {
              internal_id: '3',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:02Z',
              content: '',
              tool_calls: [
                {
                  id: 'call_2',
                  type: 'function',
                  function: { name: 'search_calendar_events', arguments: '{}' },
                },
              ],
            },
          ],
        })
      )
    );
    renderSharedPage('boundary');

    expect(await screen.findByText('Checking your calendar next.')).toBeInTheDocument();
    expect(screen.getAllByTestId('tool-group')).toHaveLength(2);
  });

  it('does not merge tool-only rows from different turns', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/turns/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'assistant',
              turn_id: 'turn_a',
              timestamp: '2026-08-10T12:00:00Z',
              content: '',
              tool_calls: [
                { id: 'call_1', type: 'function', function: { name: 'get_note', arguments: '{}' } },
              ],
            },
            {
              internal_id: '2',
              role: 'assistant',
              turn_id: 'turn_b',
              timestamp: '2026-08-10T12:00:01Z',
              content: '',
              tool_calls: [
                {
                  id: 'call_2',
                  type: 'function',
                  function: { name: 'list_notes', arguments: '{}' },
                },
              ],
            },
          ],
        })
      )
    );
    renderSharedPage('turns');

    await waitFor(() => expect(screen.getAllByTestId('tool-group')).toHaveLength(2));
  });

  it('still merges tool-only rows on history that predates turn ids', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/no-turns/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:00Z',
              content: '',
              tool_calls: [
                { id: 'call_1', type: 'function', function: { name: 'get_note', arguments: '{}' } },
              ],
            },
            {
              internal_id: '2',
              role: 'assistant',
              turn_id: null,
              timestamp: '2026-08-10T12:00:01Z',
              content: '',
              tool_calls: [
                {
                  id: 'call_2',
                  type: 'function',
                  function: { name: 'list_notes', arguments: '{}' },
                },
              ],
            },
          ],
        })
      )
    );
    renderSharedPage('no-turns');

    await waitFor(() => expect(screen.getAllByTestId('tool-group')).toHaveLength(1));
  });

  it('skips an orphan tool result that carries no output', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/empty-orphan/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: '1',
              role: 'tool',
              timestamp: '2026-08-10T12:00:00Z',
              tool_call_id: 'call_missing',
              content: '',
            },
            {
              internal_id: '2',
              role: 'assistant',
              timestamp: '2026-08-10T12:00:01Z',
              content: 'All done.',
            },
          ],
        })
      )
    );
    renderSharedPage('empty-orphan');

    expect(await screen.findByText('All done.')).toBeInTheDocument();
    expect(screen.queryByTestId('tool-group')).not.toBeInTheDocument();
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
