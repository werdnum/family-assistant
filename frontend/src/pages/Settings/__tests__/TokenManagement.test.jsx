import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../../test/setup.js';
import TokenManagement from '../TokenManagement';

const token = {
  id: 1,
  name: 'My integration',
  prefix: 'fa_test',
  created_at: '2025-01-01T00:00:00Z',
  last_used_at: null,
  expires_at: '2025-02-01T00:00:00Z',
  is_revoked: false,
};

describe('TokenManagement', () => {
  afterEach(() => vi.restoreAllMocks());

  it('clears a copy failure when the next clipboard attempt succeeds', async () => {
    let created = false;
    server.use(
      http.get('/api/me/tokens', () => HttpResponse.json(created ? [token] : [])),
      http.post('/api/me/tokens', () => {
        created = true;
        return HttpResponse.json({ ...token, full_token: 'fa_fixture_token' });
      })
    );
    const user = userEvent.setup();
    vi.spyOn(window.navigator.clipboard, 'writeText')
      .mockRejectedValueOnce(new Error('Clipboard unavailable'))
      .mockResolvedValueOnce(undefined);
    render(<TokenManagement />);
    await user.click(await screen.findByRole('button', { name: 'Create New Token' }));
    await user.click(screen.getByLabelText('Token Name'));
    await user.paste('My integration');
    await user.click(screen.getByRole('button', { name: 'Create Token', exact: true }));
    await screen.findByRole('heading', { name: 'Your Tokens (1)' });
    await user.click(screen.getByRole('button', { name: 'Copy', exact: true }));
    expect(await screen.findByText(/Could not copy the token/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Copy', exact: true }));
    expect(await screen.findByRole('button', { name: 'Copied!' })).toBeInTheDocument();
    expect(screen.queryByText(/Could not copy the token/)).not.toBeInTheDocument();
  });

  it('labels an expired token as expired rather than active', async () => {
    server.use(http.get('/api/me/tokens', () => HttpResponse.json([token])));
    render(<TokenManagement />);
    expect(await screen.findByText('EXPIRED')).toBeInTheDocument();
    expect(screen.queryByText('ACTIVE')).not.toBeInTheDocument();
  });

  it('keeps a revocation failure visible inside the open confirmation', async () => {
    server.use(
      http.get('/api/me/tokens', () => HttpResponse.json([token])),
      http.delete('/api/me/tokens/1', () =>
        HttpResponse.json({ detail: 'Could not revoke token' }, { status: 500 })
      )
    );
    const user = userEvent.setup();
    render(<TokenManagement />);
    await user.click(await screen.findByRole('button', { name: 'Revoke' }));
    await user.click(screen.getByRole('button', { name: 'Yes, Revoke Token' }));
    expect(
      await within(screen.getByRole('alertdialog')).findByText('Could not revoke token')
    ).toBeInTheDocument();
  });

  it('focuses Cancel and closes the revoke dialog with Escape', async () => {
    server.use(http.get('/api/me/tokens', () => HttpResponse.json([token])));
    const user = userEvent.setup();
    render(<TokenManagement />);
    await user.click(await screen.findByRole('button', { name: 'Revoke' }));
    expect(
      screen.getByRole('alertdialog', { name: 'Confirm Token Revocation' })
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });
});
