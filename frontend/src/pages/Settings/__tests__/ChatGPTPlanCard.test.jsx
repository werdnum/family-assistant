import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import React from 'react';
import { describe, expect, it } from 'vitest';
import { server } from '../../../test/setup.js';
import ChatGPTPlanCard from '../ChatGPTPlanCard';

const STATUS = {
  enabled: true,
  reason: null,
  status: null,
  email: null,
  connected_by_user_id: null,
  last_used_at: null,
  redirect_uri: 'http://127.0.0.1:1455/auth/callback',
  usage_url: 'https://chatgpt.com/settings/usage',
};

describe('ChatGPTPlanCard', () => {
  it('signs in by pasting the address the sign-in tab landed on', async () => {
    let pasted = null;
    server.use(
      http.post('/api/integrations/chatgpt/sign-in', () =>
        HttpResponse.json({ authorize_url: 'https://auth.openai.com/api/accounts/authorize?x=1' })
      ),
      http.post('/api/integrations/chatgpt/callback', async ({ request }) => {
        pasted = (await request.json()).callback_url;
        return HttpResponse.json({ ...STATUS, status: 'active', email: 'parent@example.com' });
      })
    );
    const user = userEvent.setup();
    render(<ChatGPTPlanCard />);

    await user.click(await screen.findByRole('button', { name: 'Continue with ChatGPT' }));
    await user.type(
      await screen.findByLabelText('Address from the sign-in tab'),
      'http://127.0.0.1:1455/auth/callback?code=abc&state=s'
    );
    await user.click(screen.getByRole('button', { name: 'Finish signing in' }));

    expect(await screen.findByText('ChatGPT account signed in.')).toBeInTheDocument();
    expect(pasted).toBe('http://127.0.0.1:1455/auth/callback?code=abc&state=s');
  });

  it('shows why a sign-in was refused', async () => {
    server.use(
      http.post('/api/integrations/chatgpt/sign-in', () =>
        HttpResponse.json({ authorize_url: 'https://auth.openai.com/authorize' })
      ),
      http.post('/api/integrations/chatgpt/callback', () =>
        HttpResponse.json({ detail: "ChatGPT plan usage wasn't granted." }, { status: 400 })
      )
    );
    const user = userEvent.setup();
    render(<ChatGPTPlanCard />);

    await user.click(await screen.findByRole('button', { name: 'Continue with ChatGPT' }));
    await user.type(await screen.findByLabelText('Address from the sign-in tab'), 'http://x');
    await user.click(screen.getByRole('button', { name: 'Finish signing in' }));

    expect(await screen.findByText("ChatGPT plan usage wasn't granted.")).toBeInTheDocument();
  });

  it('offers sign-out and a different account once signed in', async () => {
    server.use(
      http.get('/api/integrations/chatgpt', () =>
        HttpResponse.json({ ...STATUS, status: 'active', email: 'parent@example.com' })
      )
    );
    render(<ChatGPTPlanCard />);

    expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Use a different account' })).toBeInTheDocument();
  });

  it('explains why sign-in is unavailable', async () => {
    server.use(
      http.get('/api/integrations/chatgpt', () =>
        HttpResponse.json({
          ...STATUS,
          enabled: false,
          reason: 'ChatGPT sign-in is turned off for this deployment.',
        })
      )
    );
    render(<ChatGPTPlanCard />);

    expect(await screen.findByText(/turned off for this deployment/)).toBeInTheDocument();
  });
});
