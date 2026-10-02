import { render, screen } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../../../test/setup.js';
import ConversationsList from '../ConversationsList';

const conversation = (index) => ({
  conversation_id: `web_conv_${index}`,
  last_message: `Message ${index}`,
  last_timestamp: '2026-08-01T00:00:00Z',
  message_count: 1,
});

describe('ConversationsList', () => {
  it('shows the total and paginates from the response count', async () => {
    server.use(
      http.get('/api/v1/chat/conversations', () =>
        HttpResponse.json({
          conversations: Array.from({ length: 20 }, (_, i) => conversation(i)),
          count: 45,
        })
      )
    );

    render(
      <MemoryRouter initialEntries={['/history']}>
        <ConversationsList />
      </MemoryRouter>
    );

    expect(await screen.findByText('Found 45 conversations')).toBeInTheDocument();
    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument();
  });
  it('shows a retry action without claiming no conversations exist after a failure', async () => {
    server.use(
      http.get('/api/v1/chat/conversations', () => HttpResponse.json({}, { status: 503 }))
    );
    render(
      <MemoryRouter>
        <ConversationsList />
      </MemoryRouter>
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to fetch conversations');
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
    expect(
      screen.queryByText('No conversations found matching your criteria.')
    ).not.toBeInTheDocument();
  });
});
