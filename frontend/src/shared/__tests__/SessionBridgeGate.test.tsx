import { render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../test/setup.js';
import { resetSessionBridge } from '../../api/browserTokenClient';
import SessionBridgeGate from '../SessionBridgeGate';

afterEach(() => {
  resetSessionBridge();
  vi.useRealTimers();
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

  it('routes a bridge 401 into the re-authentication reload', async () => {
    const browserTokenClient = await import('../../api/browserTokenClient');
    const reloadSpy = vi
      .spyOn(browserTokenClient, 'reloadForReauthentication')
      .mockImplementation(() => {});
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

    await waitFor(() => expect(reloadSpy).toHaveBeenCalled());
    expect(screen.queryByText('app content')).not.toBeInTheDocument();
    reloadSpy.mockRestore();
  });

  it('stays gated across transient failures until a retry succeeds', async () => {
    vi.useFakeTimers();
    try {
      server.use(
        http.get('/api/auth/browser-token', () => {
          return HttpResponse.json({ token: 'test-jwt', expires_in: 3600 }, { status: 503 });
        })
      );

      render(
        <SessionBridgeGate>
          <div>app content</div>
        </SessionBridgeGate>
      );

      expect(screen.getByText('Loading…')).toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);
      expect(screen.getByText('Connecting…')).toBeInTheDocument();
      expect(screen.queryByText('app content')).not.toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(30_000);
      await vi.advanceTimersByTimeAsync(60_000);
      await vi.advanceTimersByTimeAsync(0);

      expect(screen.getByText('Connecting…')).toBeInTheDocument();
      expect(screen.queryByText('app content')).not.toBeInTheDocument();
    } finally {
      resetSessionBridge();
      vi.useRealTimers();
    }
  });

  it('mounts children as soon as a post-failure retry succeeds', async () => {
    vi.useFakeTimers();
    try {
      let attempts = 0;
      server.use(
        http.get('/api/auth/browser-token', () => {
          attempts += 1;
          if (attempts === 1) {
            return HttpResponse.error();
          }
          return HttpResponse.json({ token: 'test-jwt', expires_in: 3600 });
        })
      );

      render(
        <SessionBridgeGate>
          <div>app content</div>
        </SessionBridgeGate>
      );

      await vi.advanceTimersByTimeAsync(0);
      expect(screen.queryByText('app content')).not.toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(30_000);
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(0);

      expect(attempts).toBe(2);
      expect(screen.getByText('app content')).toBeInTheDocument();
    } finally {
      resetSessionBridge();
      vi.useRealTimers();
    }
  });
});
