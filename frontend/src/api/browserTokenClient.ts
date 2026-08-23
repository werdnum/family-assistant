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
const MAX_BROWSER_TIMER_DELAY_MS = 2_147_483_647;

interface BrowserTokenResponse {
  token?: string;
  expires_in?: number;
  enabled?: boolean;
}

class SessionExpiredError extends Error {}

class BridgeNotConfiguredError extends Error {}

class BridgeForbiddenError extends Error {}

let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let inFlight: Promise<BridgeSettlement> | null = null;
let retryDelayMs = RETRY_BASE_DELAY_MS;
let bridgeGeneration = 0;
let refreshDueAtMs: number | null = null;

/** Outcome of a single bridge attempt. */
export type BridgeSettlement =
  | 'refreshed'
  | 'session-expired'
  | 'not-configured'
  | 'unreachable'
  | 'credentials-stale'
  | 'forbidden';

const settlementListeners = new Set<(settlement: BridgeSettlement) => void>();

function notifySettlement(settlement: BridgeSettlement): void {
  settlementListeners.forEach((listener) => {
    listener(settlement);
  });
}

/**
 * Observe every bridge attempt's outcome (including scheduled retries).
 *
 * @returns An unsubscribe function.
 */
export function addBridgeSettlementListener(
  listener: (settlement: BridgeSettlement) => void
): () => void {
  settlementListeners.add(listener);
  return () => {
    settlementListeners.delete(listener);
  };
}

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
  if (response.status === 404) {
    // JWT auth is not configured on this server (unset signing key): the
    // gateway policy is disabled too, so no cookie is needed — proceed.
    throw new BridgeNotConfiguredError('Bridge endpoint not available');
  }
  if (response.status === 403) {
    throw new BridgeForbiddenError('Browser session authentication is not configured');
  }
  if (!response.ok) {
    throw new Error(`Failed to fetch browser token: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

type AttemptResult =
  | 'refreshed'
  | 'session-expired'
  | 'not-configured'
  | 'transient-failure'
  | 'forbidden';

async function attemptRefresh(cycleGeneration: number): Promise<AttemptResult> {
  const startedAt = Date.now();
  let payload: BrowserTokenResponse;
  try {
    payload = await requestBrowserToken();
  } catch (error) {
    if (error instanceof SessionExpiredError) {
      return 'session-expired';
    }
    if (error instanceof BridgeNotConfiguredError) {
      return 'not-configured';
    }
    if (error instanceof BridgeForbiddenError) {
      return 'forbidden';
    }
    return 'transient-failure';
  }

  if (payload.enabled === false) {
    return 'not-configured';
  }
  const expiresIn = payload.expires_in;
  if (typeof expiresIn !== 'number' || !Number.isFinite(expiresIn) || expiresIn <= 0) {
    return 'transient-failure';
  }
  const elapsed = Date.now() - startedAt;
  // Always schedule positive near-expiry work, even for very short TTLs where
  // the margin would otherwise go non-positive and strand the cookie. Refresh
  // long-lived cookies early rather than overflowing browsers' signed 32-bit
  // timer delay and creating a tight request loop.
  const delayMs = Math.min(
    Math.max(expiresIn * 1000 - REFRESH_MARGIN_MS - elapsed, 1_000),
    MAX_BROWSER_TIMER_DELAY_MS
  );
  if (Number.isFinite(delayMs) && cycleGeneration === bridgeGeneration) {
    refreshDueAtMs = Date.now() + delayMs;
    refreshTimer = setTimeout(() => {
      void startSessionBridge();
    }, delayMs);
  }
  return 'refreshed';
}

async function runBridgeCycle(cycleGeneration: number): Promise<BridgeSettlement> {
  const result = await attemptRefresh(cycleGeneration);
  if (cycleGeneration !== bridgeGeneration) {
    return result === 'transient-failure' ? 'unreachable' : result;
  }
  if (result === 'transient-failure') {
    refreshTimer = setTimeout(() => {
      void startSessionBridge();
    }, retryDelayMs);
    retryDelayMs = Math.min(retryDelayMs * 2, RETRY_MAX_DELAY_MS);
    const settlement =
      refreshDueAtMs !== null && Date.now() >= refreshDueAtMs ? 'credentials-stale' : 'unreachable';
    notifySettlement(settlement);
    return settlement;
  }
  if (result === 'refreshed') {
    retryDelayMs = RETRY_BASE_DELAY_MS;
    notifySettlement('refreshed');
    return 'refreshed';
  }
  if (result === 'not-configured') {
    refreshDueAtMs = null;
    retryDelayMs = RETRY_BASE_DELAY_MS;
    notifySettlement('not-configured');
    return 'not-configured';
  }
  if (result === 'forbidden') {
    refreshDueAtMs = null;
    notifySettlement('forbidden');
    return 'forbidden';
  }
  refreshDueAtMs = null;
  notifySettlement('session-expired');
  return 'session-expired';
}

/**
 * Central re-authentication transition for a bridge 401: a full-page
 * navigation to the current URL lets the backend's AuthMiddleware redirect to
 * OIDC login with the intended destination preserved. Exported for tests.
 */
export function reloadForReauthentication(): void {
  window.location.reload();
}

/**
 * Call the bridge now and schedule the next run (near expiry, or after a
 * transient failure). Concurrent calls share one request; a previously
 * scheduled timer is replaced. Resolves with this attempt's outcome once it
 * settles, so callers can gate rendering on it without waiting out retries.
 */
export function startSessionBridge(): Promise<BridgeSettlement> {
  if (inFlight) {
    return inFlight;
  }
  clearRefreshTimer();
  const cycleGeneration = bridgeGeneration;
  const tracked = runBridgeCycle(cycleGeneration).finally(() => {
    if (inFlight === tracked) {
      inFlight = null;
    }
  });
  inFlight = tracked;
  return tracked;
}

/**
 * Refresh an overdue credential after a suspended/frozen page resumes.
 *
 * Returns null while the current cookie is still inside its safe lifetime.
 * Callers that receive a promise must hold API consumers behind their gate
 * until it settles, because browser timers may have been suspended past expiry.
 */
export function refreshSessionBridgeAfterResume(): Promise<BridgeSettlement> | null {
  if (refreshDueAtMs === null || Date.now() < refreshDueAtMs) {
    return null;
  }
  return startSessionBridge();
}

/** Cancel pending work and detach any request that is already in flight. */
export function resetSessionBridge(): void {
  bridgeGeneration += 1;
  clearRefreshTimer();
  inFlight = null;
  retryDelayMs = RETRY_BASE_DELAY_MS;
  refreshDueAtMs = null;
}
