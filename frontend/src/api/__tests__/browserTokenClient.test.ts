import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../test/setup.js';
import { resetSessionBridge, startSessionBridge } from '../browserTokenClient';

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

    await expect(startSessionBridge()).resolves.toBeUndefined();

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

  it('retries once after a failure and then succeeds', async () => {
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

    const bridge = startSessionBridge();
    await vi.advanceTimersByTimeAsync(5_000);
    await bridge;

    expect(attempts).toBe(2);
    expect(vi.getTimerCount()).toBe(1);
  });

  it('backs off after repeated failures instead of retrying indefinitely', async () => {
    server.use(
      http.get('/api/auth/browser-token', () => {
        tokenRequests += 1;
        return HttpResponse.error();
      })
    );

    const bridge = startSessionBridge();
    await vi.advanceTimersByTimeAsync(5_000);
    await bridge;

    expect(tokenRequests).toBe(2);
    expect(vi.getTimerCount()).toBe(0);

    await vi.advanceTimersByTimeAsync(60_000);
    expect(tokenRequests).toBe(2);
  });
});
