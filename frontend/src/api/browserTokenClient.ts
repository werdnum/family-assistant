/**
 * Client for the session→JWT bridge endpoint.
 *
 * GET /api/auth/browser-token authenticates via the OIDC session cookie and
 * sets the short-lived API JWT as an HttpOnly cookie scoped to /api, so that
 * browser-managed requests (fetch, EventSource, <img>) authenticate without
 * per-request machinery. This module calls the bridge when the app loads and
 * again shortly before the JWT expires, keeping that cookie fresh. The JWT
 * itself is never read or stored here — only its lifetime, from `expires_in`.
 */

const REFRESH_MARGIN_MS = 60_000;
const RETRY_DELAY_MS = 5_000;

interface BrowserTokenResponse {
  token: string;
  expires_in: number;
}

class SessionExpiredError extends Error {}

let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let inFlight: Promise<void> | null = null;

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

async function runRefresh(attempt = 0): Promise<void> {
  const startedAt = Date.now();
  let payload: BrowserTokenResponse;
  try {
    payload = await requestBrowserToken();
  } catch (error) {
    if (error instanceof SessionExpiredError) {
      return;
    }
    if (attempt === 0) {
      await delay(RETRY_DELAY_MS);
      return runRefresh(attempt + 1);
    }
    return;
  }

  const elapsed = Date.now() - startedAt;
  const delayMs = payload.expires_in * 1000 - REFRESH_MARGIN_MS - elapsed;
  if (Number.isFinite(delayMs) && delayMs > 0) {
    refreshTimer = setTimeout(() => {
      void startSessionBridge();
    }, delayMs);
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/**
 * Call the bridge now and schedule the near-expiry re-run. Concurrent calls
 * share one request; a previously scheduled timer is replaced.
 */
export function startSessionBridge(): Promise<void> {
  if (inFlight) {
    return inFlight;
  }
  clearRefreshTimer();
  inFlight = runRefresh().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

/** Cancel the pending refresh; used on logout / re-auth. */
export function resetSessionBridge(): void {
  clearRefreshTimer();
  inFlight = null;
}
