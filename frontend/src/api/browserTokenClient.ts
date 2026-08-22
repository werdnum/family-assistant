/**
 * Client for the session→JWT bridge endpoint.
 *
 * GET /api/auth/browser-token authenticates via the OIDC session cookie and
 * sets the short-lived API JWT as an HttpOnly cookie scoped to /api, so that
 * browser-managed requests (fetch, EventSource, <img>) authenticate without
 * per-request machinery. This module calls the bridge when the app loads and
 * again shortly before the JWT expires, keeping that cookie fresh. Transient
 * failures are retried on an exponential-backoff timer; the JWT itself is
 * never read or stored here — only its lifetime, from `expires_in`.
 */

const REFRESH_MARGIN_MS = 60_000;
const RETRY_BASE_DELAY_MS = 30_000;
const RETRY_MAX_DELAY_MS = 10 * 60_000;

interface BrowserTokenResponse {
  token: string;
  expires_in: number;
}

class SessionExpiredError extends Error {}

let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let inFlight: Promise<void> | null = null;
let retryDelayMs = RETRY_BASE_DELAY_MS;

function clearRefreshTimer(): void {
  if (refreshTimer !== null) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

async function requestBrowserToken(): Promise<BrowserTokenResponse> {
  const response = await fetch('/api/auth/browser-token');
  if (response.status === 401) {
    throw new SessionExpiredError('OIDC session expired');
  }
  if (!response.ok) {
    throw new Error(`Failed to fetch browser token: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

type AttemptResult = 'refreshed' | 'session-expired' | 'transient-failure';

async function attemptRefresh(): Promise<AttemptResult> {
  const startedAt = Date.now();
  let payload: BrowserTokenResponse;
  try {
    payload = await requestBrowserToken();
  } catch (error) {
    return error instanceof SessionExpiredError ? 'session-expired' : 'transient-failure';
  }

  const elapsed = Date.now() - startedAt;
  const delayMs = payload.expires_in * 1000 - REFRESH_MARGIN_MS - elapsed;
  if (Number.isFinite(delayMs) && delayMs > 0) {
    refreshTimer = setTimeout(() => {
      void startSessionBridge();
    }, delayMs);
  }
  return 'refreshed';
}

async function runBridgeCycle(): Promise<void> {
  const result = await attemptRefresh();
  if (result === 'refreshed') {
    retryDelayMs = RETRY_BASE_DELAY_MS;
    return;
  }
  if (result === 'transient-failure') {
    refreshTimer = setTimeout(() => {
      void startSessionBridge();
    }, retryDelayMs);
    retryDelayMs = Math.min(retryDelayMs * 2, RETRY_MAX_DELAY_MS);
  }
}

/**
 * Call the bridge now and schedule the next run (near expiry, or after a
 * transient failure). Concurrent calls share one request; a previously
 * scheduled timer is replaced. Resolves once the initial attempt settles, so
 * callers can gate rendering on it without waiting out retries.
 */
export function startSessionBridge(): Promise<void> {
  if (inFlight) {
    return inFlight;
  }
  clearRefreshTimer();
  inFlight = runBridgeCycle().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

/** Cancel the pending refresh; used on logout / re-auth. */
export function resetSessionBridge(): void {
  clearRefreshTimer();
  inFlight = null;
  retryDelayMs = RETRY_BASE_DELAY_MS;
}
