import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../../test/setup.js';
import LinkWalletCard from '../LinkWalletCard';

const linkDisabledResponse = {
  enabled: false,
  reason: 'Link Agent Wallet is not configured on this server.',
  require_taint_enforcement_waived: false,
  connected: false,
  provider_account_email: null,
  status: null,
  granted_scopes: [],
  configured_scopes: ['payment_methods.agentic', 'userinfo:read'],
  missing_configured_scopes: [],
  last_used_at: null,
};

const linkNotConnectedResponse = {
  enabled: true,
  reason: null,
  require_taint_enforcement_waived: false,
  connected: false,
  provider_account_email: null,
  status: null,
  granted_scopes: [],
  configured_scopes: ['payment_methods.agentic', 'userinfo:read'],
  missing_configured_scopes: [],
  last_used_at: null,
};

const linkConnectedResponse = {
  enabled: true,
  reason: null,
  require_taint_enforcement_waived: false,
  connected: true,
  provider_account_email: 'wallet@example.com',
  status: 'active',
  granted_scopes: ['payment_methods.agentic', 'userinfo:read'],
  configured_scopes: ['payment_methods.agentic', 'userinfo:read'],
  missing_configured_scopes: [],
  last_used_at: '2026-11-20T14:30:00Z',
};

const deviceStartResponse = {
  flow_id: 'flow-opaque-123',
  verification_uri: 'https://app.link.com/device/setup',
  verification_uri_complete: 'https://app.link.com/device/setup?code=apple-river-stone-lamp',
  user_code: 'apple-river-stone-lamp',
  expires_in: 600,
  interval: 0.01,
};

describe('LinkWalletCard', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders disabled state with reason and no action buttons', async () => {
    server.use(http.get('/api/integrations/link', () => HttpResponse.json(linkDisabledResponse)));

    render(<LinkWalletCard />);

    expect(
      await screen.findByText('Link Agent Wallet is not configured on this server.')
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /connect/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /disconnect/i })).not.toBeInTheDocument();
  });

  it('renders disabled-but-connected state and lets user disconnect via dialog', async () => {
    const user = userEvent.setup();
    let connected = true;
    let deleteCount = 0;

    server.use(
      http.get('/api/integrations/link', () =>
        HttpResponse.json({
          ...linkConnectedResponse,
          enabled: false,
          connected,
          provider_account_email: connected ? 'wallet@example.com' : null,
          reason: 'Link integration is disabled: taint enforcement required.',
        })
      ),
      http.delete('/api/integrations/link', () => {
        deleteCount += 1;
        connected = false;
        return new HttpResponse(null, { status: 204 });
      })
    );

    render(<LinkWalletCard />);

    expect(
      await screen.findByText('Link integration is disabled: taint enforcement required.')
    ).toBeInTheDocument();
    expect(screen.getByText('wallet@example.com')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /connect link/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(
      screen.getByRole('alertdialog', { name: 'Disconnect Link account?' })
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Yes, disconnect' }));

    await waitFor(() => {
      expect(deleteCount).toBe(1);
    });
    await waitFor(() => {
      expect(screen.queryByText('wallet@example.com')).not.toBeInTheDocument();
    });
  });

  it('runs device-code start, polls pending with updated interval, and refreshes on connected', async () => {
    const user = userEvent.setup();
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout');
    let connected = false;
    let pollAttempts = 0;
    const polledFlowIds = [];

    server.use(
      http.get('/api/integrations/link', () =>
        HttpResponse.json(connected ? linkConnectedResponse : linkNotConnectedResponse)
      ),
      http.post('/api/integrations/link/device/start', () =>
        HttpResponse.json({
          ...deviceStartResponse,
          interval: 0.02,
        })
      ),
      http.post('/api/integrations/link/device/poll', async ({ request }) => {
        const body = await request.json();
        polledFlowIds.push(body.flow_id);
        pollAttempts += 1;

        if (pollAttempts === 1) {
          return HttpResponse.json({ status: 'pending', interval: 0.03 });
        }

        connected = true;
        return HttpResponse.json({
          status: 'connected',
          provider_account_email: 'wallet@example.com',
        });
      })
    );

    render(<LinkWalletCard />);

    await user.click(await screen.findByRole('button', { name: 'Connect Link account' }));

    const link = await screen.findByRole('link', { name: /open link to authorize/i });
    expect(link).toHaveAttribute('href', deviceStartResponse.verification_uri_complete);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noreferrer');
    expect(screen.getByText(deviceStartResponse.user_code)).toBeInTheDocument();
    expect(screen.getByText('Waiting for approval in Link...')).toBeInTheDocument();

    expect(
      await screen.findByText('Link account (wallet@example.com) connected successfully.')
    ).toBeInTheDocument();

    expect(polledFlowIds).toEqual(['flow-opaque-123', 'flow-opaque-123']);
    const scheduledDelays = setTimeoutSpy.mock.calls.map((call) => call[1]);
    expect(scheduledDelays).toContain(20);
    expect(scheduledDelays).toContain(30);

    expect(await screen.findByText('wallet@example.com')).toBeInTheDocument();
    expect(screen.getByText('Agentic payments')).toBeInTheDocument();
    expect(screen.getByText('Account profile')).toBeInTheDocument();
    expect(screen.queryByText('Waiting for approval in Link...')).not.toBeInTheDocument();
  });

  it('falls back to verification_uri when verification_uri_complete is absent', async () => {
    const user = userEvent.setup();

    server.use(
      http.get('/api/integrations/link', () => HttpResponse.json(linkNotConnectedResponse)),
      http.post('/api/integrations/link/device/start', () =>
        HttpResponse.json({
          ...deviceStartResponse,
          verification_uri_complete: null,
          interval: 60,
        })
      )
    );

    render(<LinkWalletCard />);

    await user.click(await screen.findByRole('button', { name: 'Connect Link account' }));

    const link = await screen.findByRole('link', { name: /open link to authorize/i });
    expect(link).toHaveAttribute('href', deviceStartResponse.verification_uri);
  });

  it('stops polling and shows error alert when device code expires', async () => {
    const user = userEvent.setup();
    let pollCount = 0;

    server.use(
      http.get('/api/integrations/link', () => HttpResponse.json(linkNotConnectedResponse)),
      http.post('/api/integrations/link/device/start', () =>
        HttpResponse.json(deviceStartResponse)
      ),
      http.post('/api/integrations/link/device/poll', () => {
        pollCount += 1;
        return HttpResponse.json({
          status: 'expired',
          detail: 'Device authorization code has expired.',
        });
      })
    );

    render(<LinkWalletCard />);

    await user.click(await screen.findByRole('button', { name: 'Connect Link account' }));

    expect(await screen.findByText('Device authorization code has expired.')).toBeInTheDocument();
    expect(screen.queryByText('Waiting for approval in Link...')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect Link account' })).toBeInTheDocument();
    expect(pollCount).toBe(1);
  });

  it('stops polling and shows error alert when authorization is denied', async () => {
    const user = userEvent.setup();

    server.use(
      http.get('/api/integrations/link', () => HttpResponse.json(linkNotConnectedResponse)),
      http.post('/api/integrations/link/device/start', () =>
        HttpResponse.json(deviceStartResponse)
      ),
      http.post('/api/integrations/link/device/poll', () =>
        HttpResponse.json({
          status: 'denied',
          detail: 'Authorization was declined in Link.',
        })
      )
    );

    render(<LinkWalletCard />);

    await user.click(await screen.findByRole('button', { name: 'Connect Link account' }));

    expect(await screen.findByText('Authorization was declined in Link.')).toBeInTheDocument();
    expect(screen.queryByText('Waiting for approval in Link...')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect Link account' })).toBeInTheDocument();
  });

  it('stops polling and shows error alert when poll request fails', async () => {
    const user = userEvent.setup();

    server.use(
      http.get('/api/integrations/link', () => HttpResponse.json(linkNotConnectedResponse)),
      http.post('/api/integrations/link/device/start', () =>
        HttpResponse.json(deviceStartResponse)
      ),
      http.post('/api/integrations/link/device/poll', () =>
        HttpResponse.json({ detail: 'Link service unavailable.' }, { status: 502 })
      )
    );

    render(<LinkWalletCard />);

    await user.click(await screen.findByRole('button', { name: 'Connect Link account' }));

    expect(await screen.findByText('Link service unavailable.')).toBeInTheDocument();
    expect(screen.queryByText('Waiting for approval in Link...')).not.toBeInTheDocument();
  });

  it('cancels polling and clears pending flow when Cancel is clicked', async () => {
    const user = userEvent.setup();
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout');
    let pollCount = 0;

    server.use(
      http.get('/api/integrations/link', () => HttpResponse.json(linkNotConnectedResponse)),
      http.post('/api/integrations/link/device/start', () =>
        HttpResponse.json({
          ...deviceStartResponse,
          interval: 0.1,
        })
      ),
      http.post('/api/integrations/link/device/poll', () => {
        pollCount += 1;
        return HttpResponse.json({ status: 'pending', interval: 0.1 });
      })
    );

    render(<LinkWalletCard />);

    await user.click(await screen.findByRole('button', { name: 'Connect Link account' }));
    expect(await screen.findByText('Waiting for approval in Link...')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByText('Waiting for approval in Link...')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect Link account' })).toBeInTheDocument();
    expect(clearTimeoutSpy).toHaveBeenCalled();
    expect(pollCount).toBe(0);
  });

  it('cleans up polling timer on unmount', async () => {
    const user = userEvent.setup();
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout');
    let pollCount = 0;

    server.use(
      http.get('/api/integrations/link', () => HttpResponse.json(linkNotConnectedResponse)),
      http.post('/api/integrations/link/device/start', () =>
        HttpResponse.json({
          ...deviceStartResponse,
          interval: 0.1,
        })
      ),
      http.post('/api/integrations/link/device/poll', () => {
        pollCount += 1;
        return HttpResponse.json({ status: 'pending', interval: 0.1 });
      })
    );

    const { unmount } = render(<LinkWalletCard />);

    await user.click(await screen.findByRole('button', { name: 'Connect Link account' }));
    expect(await screen.findByText('Waiting for approval in Link...')).toBeInTheDocument();

    unmount();

    expect(clearTimeoutSpy).toHaveBeenCalled();
    expect(pollCount).toBe(0);
  });

  it('renders connected state with email, last used, scope badges, and disconnect confirmation', async () => {
    const user = userEvent.setup();
    let deleteCount = 0;
    let getCount = 0;

    server.use(
      http.get('/api/integrations/link', () => {
        getCount += 1;
        return HttpResponse.json(
          deleteCount > 0 ? linkNotConnectedResponse : linkConnectedResponse
        );
      }),
      http.delete('/api/integrations/link', () => {
        deleteCount += 1;
        return new HttpResponse(null, { status: 204 });
      })
    );

    render(<LinkWalletCard />);

    expect(await screen.findByText('wallet@example.com')).toBeInTheDocument();
    expect(screen.getByText('Last used:')).toBeInTheDocument();
    expect(screen.getByText('Agentic payments')).toBeInTheDocument();
    expect(screen.getByText('Account profile')).toBeInTheDocument();

    // Open dialog, verify safe focus & Escape dismissal
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(
      screen.getByRole('alertdialog', { name: 'Disconnect Link account?' })
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toHaveFocus();

    // Reopen and confirm disconnect
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    await user.click(screen.getByRole('button', { name: 'Yes, disconnect' }));

    await waitFor(() => {
      expect(deleteCount).toBe(1);
    });
    expect(getCount).toBeGreaterThanOrEqual(2);
    expect(await screen.findByRole('button', { name: 'Connect Link account' })).toBeInTheDocument();
  });

  it('renders needs_reauth alert and allows reconnecting via device flow', async () => {
    const user = userEvent.setup();
    let statusValue = 'needs_reauth';

    server.use(
      http.get('/api/integrations/link', () =>
        HttpResponse.json({
          ...linkConnectedResponse,
          status: statusValue,
        })
      ),
      http.post('/api/integrations/link/device/start', () =>
        HttpResponse.json(deviceStartResponse)
      ),
      http.post('/api/integrations/link/device/poll', () => {
        statusValue = 'active';
        return HttpResponse.json({
          status: 'connected',
          provider_account_email: 'wallet@example.com',
        });
      })
    );

    render(<LinkWalletCard />);

    expect(await screen.findByText(/needs to be renewed/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Reconnect Link account' }));

    expect(
      await screen.findByText('Link account (wallet@example.com) connected successfully.')
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText(/needs to be renewed/i)).not.toBeInTheDocument();
    });
  });
});
