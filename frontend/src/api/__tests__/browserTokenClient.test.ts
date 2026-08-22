import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../test/setup.js';
import {
  addBridgeSettlementListener,
  resetSessionBridge,
  startSessionBridge,
} from '../browserTokenClient';

let tokenRequests: number;

function tokenHandler(expiresIn = 3600) {
  return http.get('/api/auth/browser-token', () => {
    tokenRequests += 1;
    return HttpResponse.json({ token: 'test-jwt', expires_in: expiresIn });
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  tokenRequests = 0;
});

afterEach(() => {
  resetSessionBridge();
  vi.useRealTimers();
});

describe('startSessionBridge', () => {
  it('calls the bridge once on load and schedules the near-expiry timer', async () => {
    server.use(tokenHandler(3600));

    await startSessionBridge();

    expect(tokenRequests).toBe(1);
    expect(vi.getTimerCount()).toBe(1);
  });
  it('re-fires the bridge shortly before expiry', async () => {
    server.use(tokenHandler(3600));

    await startSessionBridge();
    await vi.advanceTimersByTimeAsync(3600 * 1000 - 60_000);

    expect(tokenRequests).toBe(2);
    expect(vi.getTimerCount()).toBe(1);
  });

  it('handles a 401 by skipping silently without scheduling anything', async () => {
    server.use(
      http.get('/api/auth/browser-token', () => {
        tokenRequests += 1;
        return HttpResponse.json({ detail: 'Unauthorized' }, { status: 401 });
      })
    );

    await expect(startSessionBridge()).resolves.toBe('session-expired');

    expect(tokenRequests).toBe(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not issue overlapping requests for concurrent starts', async () => {
    let resolveFirst!: () => void;
    server.use(
      http.get('/api/auth/browser-token', async () => {
        tokenRequests += 1;
        if (tokenRequests === 1) {
          await new Promise<void>((resolve) => {
            resolveFirst = resolve;
          });
        }
        return HttpResponse.json({ token: 'test-jwt', expires_in: 3600 });
      })
    );

    const first = startSessionBridge();
    const second = startSessionBridge();
    await vi.advanceTimersByTimeAsync(0);
    resolveFirst();
    await Promise.all([first, second]);

    expect(tokenRequests).toBe(1);
  });

  it('resolves as unreachable after a failed attempt, with a retry scheduled', async () => {
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

    await expect(startSessionBridge()).resolves.toBe('unreachable');

    expect(attempts).toBe(1);
    expect(vi.getTimerCount()).toBe(1);

    await vi.advanceTimersByTimeAsync(30_000);

    expect(attempts).toBe(2);
    expect(vi.getTimerCount()).toBe(1);
  });

  it('reschedules transient failures with exponential backoff capped at ten minutes', async () => {
    server.use(
      http.get('/api/auth/browser-token', () => {
        tokenRequests += 1;
        return HttpResponse.error();
      })
    );

    await startSessionBridge();

    expect(tokenRequests).toBe(1);
    await vi.advanceTimersByTimeAsync(30_000 - 1);
    expect(tokenRequests).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(tokenRequests).toBe(2);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(tokenRequests).toBe(3);

    await vi.advanceTimersByTimeAsync(120_000 - 1);
    expect(tokenRequests).toBe(3);
    await vi.advanceTimersByTimeAsync(1);
    expect(tokenRequests).toBe(4);

    await vi.advanceTimersByTimeAsync(240_000);
    expect(tokenRequests).toBe(5);

    await vi.advanceTimersByTimeAsync(480_000);
    expect(tokenRequests).toBe(6);

    await vi.advanceTimersByTimeAsync(600_000);
    expect(tokenRequests).toBe(7);

    await vi.advanceTimersByTimeAsync(600_000);
    expect(tokenRequests).toBe(8);
  });

  it('returns to the near-expiry cadence once the bridge succeeds again', async () => {
    server.use(
      http.get('/api/auth/browser-token', () => {
        tokenRequests += 1;
        if (tokenRequests <= 2) {
          return HttpResponse.error();
        }
        return HttpResponse.json({ token: 'test-jwt', expires_in: 3600 });
      })
    );

    await startSessionBridge();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(tokenRequests).toBe(2);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(tokenRequests).toBe(3);
    expect(vi.getTimerCount()).toBe(1);

    await vi.advanceTimersByTimeAsync(3600 * 1000 - 60_000);
    expect(tokenRequests).toBe(4);
  });

  it('notifies settlement listeners with each attempt outcome', async () => {
    server.use(
      http.get('/api/auth/browser-token', () => {
        tokenRequests += 1;
        if (tokenRequests <= 2) {
          return HttpResponse.error();
        }
        return HttpResponse.json({ token: 'test-jwt', expires_in: 3600 });
      })
    );
    const settlements: string[] = [];
    const unsubscribe = addBridgeSettlementListener((settlement) => {
      settlements.push(settlement);
    });

    await startSessionBridge();
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.advanceTimersByTimeAsync(60_000);

    expect(settlements).toEqual(['unreachable', 'unreachable', 'refreshed']);
    unsubscribe();
    expect(vi.getTimerCount()).toBe(1);

    await vi.advanceTimersByTimeAsync(3600 * 1000 - 60_000);
    expect(settlements).toEqual(['unreachable', 'unreachable', 'refreshed']);
  });
});
