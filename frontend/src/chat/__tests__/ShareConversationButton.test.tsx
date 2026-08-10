import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { vi } from 'vitest';
import { server } from '../../test/setup.js';
import { ShareConversationButton } from '../ShareConversationButton';

describe('ShareConversationButton', () => {
  it('copies a rotated link and can revoke it', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    let active = false;
    server.use(
      http.get('/api/v1/chat/conversations/conversation-1/share', () =>
        HttpResponse.json({ active })
      ),
      http.post('/api/v1/chat/conversations/conversation-1/share', () => {
        active = true;
        return HttpResponse.json({ share_url: '/shared/conversations/new-token' });
      }),
      http.delete('/api/v1/chat/conversations/conversation-1/share', () => {
        active = false;
        return new HttpResponse(null, { status: 204 });
      })
    );
    render(<ShareConversationButton conversationId="conversation-1" hasMessages={true} />);

    await user.click(await screen.findByRole('button', { name: 'Share conversation' }));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        `${window.location.origin}/shared/conversations/new-token`
      );
    });
    expect(screen.getByText('Link copied')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Stop sharing conversation' }));
    expect(await screen.findByText('Sharing stopped')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Stop sharing conversation' })
    ).not.toBeInTheDocument();
  });
});
