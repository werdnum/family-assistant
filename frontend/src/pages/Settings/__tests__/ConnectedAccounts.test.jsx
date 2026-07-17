import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../../test/setup.js';
import ConnectedAccounts from '../ConnectedAccounts';

const renderPage = (initialPath = '/settings/accounts') => {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <ConnectedAccounts />
    </MemoryRouter>
  );
};

const googleDisabledResponse = {
  enabled: false,
  reason: 'Google integration is not configured on this server.',
  require_taint_enforcement_waived: false,
  connected: false,
  provider_account_email: null,
  status: null,
  granted_scopes: [],
  configured_scopes: [],
  missing_configured_scopes: [],
  last_used_at: null,
};

const googleNotConnectedResponse = {
  enabled: true,
  reason: null,
  require_taint_enforcement_waived: false,
  connected: false,
  provider_account_email: null,
  status: null,
  granted_scopes: [],
  configured_scopes: [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
  ],
  missing_configured_scopes: [],
  last_used_at: null,
};

const googleConnectedResponse = {
  enabled: true,
  reason: null,
  require_taint_enforcement_waived: false,
  connected: true,
  provider_account_email: 'user@example.com',
  status: 'active',
  granted_scopes: [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
  ],
  configured_scopes: [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
  ],
  missing_configured_scopes: [],
  last_used_at: '2026-07-15T10:00:00Z',
};

const googleNeedsReauthResponse = {
  ...googleConnectedResponse,
  status: 'needs_reauth',
};

const googleMissingScopesResponse = {
  ...googleConnectedResponse,
  granted_scopes: ['https://www.googleapis.com/auth/gmail.readonly'],
  missing_configured_scopes: ['https://www.googleapis.com/auth/drive.readonly'],
};

describe('ConnectedAccounts', () => {
  it('renders disabled state with reason when integration is not enabled', async () => {
    server.use(
      http.get('/api/integrations/google', () => HttpResponse.json(googleDisabledResponse))
    );

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText('Google integration is not configured on this server.')
      ).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: /connect/i })).not.toBeInTheDocument();
  });

  it('renders disabled-but-connected state with disconnect button and no connect button', async () => {
    const disabledConnected = {
      ...googleConnectedResponse,
      enabled: false,
      reason: 'Google integration is disabled: taint floor not met.',
    };
    server.use(http.get('/api/integrations/google', () => HttpResponse.json(disabledConnected)));

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText('Google integration is disabled: taint floor not met.')
      ).toBeInTheDocument();
    });

    expect(screen.getByText('user@example.com')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /disconnect/i })).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /connect google account/i })
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /reconnect/i })).not.toBeInTheDocument();
  });

  it('renders not-connected state with connect button', async () => {
    server.use(
      http.get('/api/integrations/google', () => HttpResponse.json(googleNotConnectedResponse))
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /connect google account/i })).toBeInTheDocument();
    });

    expect(screen.getByText(/connect your google account/i)).toBeInTheDocument();
  });

  it('renders connected state with account email, scopes, and last used', async () => {
    server.use(
      http.get('/api/integrations/google', () => HttpResponse.json(googleConnectedResponse))
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('user@example.com')).toBeInTheDocument();
    });

    expect(screen.getByText('Gmail read access')).toBeInTheDocument();
    expect(screen.getByText('Drive read access')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reconnect/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /disconnect/i })).toBeInTheDocument();
  });

  it('renders needs_reauth warning with reconnect button', async () => {
    server.use(
      http.get('/api/integrations/google', () => HttpResponse.json(googleNeedsReauthResponse))
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/needs to be renewed/i)).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: /reconnect/i })).toBeInTheDocument();
  });

  it('renders missing-scope warning listing human-readable scope names', async () => {
    server.use(
      http.get('/api/integrations/google', () => HttpResponse.json(googleMissingScopesResponse))
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByText('Drive access not granted')).toBeInTheDocument();
    });

    expect(screen.getByText(/reconnect and approve all/i)).toBeInTheDocument();
  });

  it('disconnect flow: shows confirmation modal, calls DELETE, and refetches status', async () => {
    const user = userEvent.setup();
    let deleteCallCount = 0;
    let getCallCount = 0;

    server.use(
      http.get('/api/integrations/google', () => {
        getCallCount += 1;
        return HttpResponse.json(googleConnectedResponse);
      }),
      http.delete('/api/integrations/google', () => {
        deleteCallCount += 1;
        return new HttpResponse(null, { status: 204 });
      })
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /disconnect/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /disconnect/i }));

    await waitFor(() => {
      expect(screen.getByText(/disconnect google account/i)).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /yes, disconnect/i }));

    await waitFor(() => {
      expect(deleteCallCount).toBe(1);
    });

    expect(getCallCount).toBeGreaterThanOrEqual(2);
  });

  it('shows success banner from ?google=connected query param and cleans the URL', async () => {
    server.use(
      http.get('/api/integrations/google', () => HttpResponse.json(googleConnectedResponse))
    );

    // jsdom window.location doesn't reflect MemoryRouter's initialEntries, so
    // set the search directly before rendering.
    const originalSearch = window.location.search;
    Object.defineProperty(window, 'location', {
      value: { ...window.location, search: '?google=connected', hash: '' },
      writable: true,
      configurable: true,
    });

    renderPage('/settings/accounts?google=connected');

    await waitFor(() => {
      expect(screen.getByText('Google account connected successfully.')).toBeInTheDocument();
    });

    // Restore
    Object.defineProperty(window, 'location', {
      value: { ...window.location, search: originalSearch },
      writable: true,
      configurable: true,
    });
  });
});
