import { render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { server } from '../../test/setup.js';
import SharedConversationPage from '../SharedConversationPage';

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
    render(
      <MemoryRouter initialEntries={['/shared/conversations/share-token']}>
        <Routes>
          <Route path="/shared/conversations/:token" element={<SharedConversationPage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText('Which gift should I choose?')).toBeInTheDocument();
    expect(screen.getByText('The blue one.')).toBeInTheDocument();
    expect(screen.getByText('Read only')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Message Family Assistant...')).not.toBeInTheDocument();
  });

  it('shows an unavailable state for a revoked link', async () => {
    server.use(
      http.get('/api/v1/shared-conversations/revoked/messages', () =>
        HttpResponse.json({ detail: 'Not found' }, { status: 404 })
      )
    );
    render(
      <MemoryRouter initialEntries={['/shared/conversations/revoked']}>
        <Routes>
          <Route path="/shared/conversations/:token" element={<SharedConversationPage />} />
        </Routes>
      </MemoryRouter>
    );

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
    render(
      <MemoryRouter initialEntries={['/shared/conversations/retry']}>
        <Routes>
          <Route path="/shared/conversations/:token" element={<SharedConversationPage />} />
        </Routes>
      </MemoryRouter>
    );

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
    render(
      <MemoryRouter initialEntries={['/shared/conversations/sign-in']}>
        <Routes>
          <Route path="/shared/conversations/:token" element={<SharedConversationPage />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText('Sign in to view this conversation')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sign in again' })).toHaveAttribute(
      'href',
      window.location.href
    );
  });
});
