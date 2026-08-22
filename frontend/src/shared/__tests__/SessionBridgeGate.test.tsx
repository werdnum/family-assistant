import { render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';
import { server } from '../../test/setup.js';
import { resetSessionBridge } from '../../api/browserTokenClient';
import SessionBridgeGate from '../SessionBridgeGate';

afterEach(() => {
  resetSessionBridge();
});

describe('SessionBridgeGate', () => {
  it('shows a loading state until the bridge settles, then renders children', async () => {
    server.use(
      http.get('/api/auth/browser-token', () =>
        HttpResponse.json({ token: 'test-jwt', expires_in: 3600 })
      )
    );

    render(
      <SessionBridgeGate>
        <div>app content</div>
      </SessionBridgeGate>
    );

    expect(screen.queryByText('app content')).not.toBeInTheDocument();
    expect(screen.getByText('Loading…')).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText('app content')).toBeInTheDocument());
  });

  it('renders children when the bridge returns 401', async () => {
    server.use(
      http.get('/api/auth/browser-token', () =>
        HttpResponse.json({ detail: 'Unauthorized' }, { status: 401 })
      )
    );

    render(
      <SessionBridgeGate>
        <div>app content</div>
      </SessionBridgeGate>
    );

    await waitFor(() => expect(screen.getByText('app content')).toBeInTheDocument());
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
  });

  it('renders children when the bridge fails transiently', async () => {
    server.use(http.get('/api/auth/browser-token', () => HttpResponse.error()));

    render(
      <SessionBridgeGate>
        <div>app content</div>
      </SessionBridgeGate>
    );

    await waitFor(() => expect(screen.getByText('app content')).toBeInTheDocument());
  });
});
