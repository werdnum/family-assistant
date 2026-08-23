import React, { useEffect, useState } from 'react';
import {
  addBridgeSettlementListener,
  refreshSessionBridgeAfterResume,
  reloadForReauthentication,
  startSessionBridge,
} from '../api/browserTokenClient';

/**
 * Delays rendering authenticated children until the session→JWT bridge has
 * actually established credentials: a successful response. On transient
 * failures the backoff loop in browserTokenClient keeps retrying and children
 * stay gated behind a "Connecting…" placeholder, so their mount effects don't
 * race ahead of the Set-Cookie and hit unrecoverable 401s. A 401 (expired
 * OIDC session) routes into the central re-authentication transition: a
 * full-page navigation to the current URL lets AuthMiddleware redirect to the
 * OIDC login with the intended destination preserved.
 */
export default function SessionBridgeGate({ children }) {
  const [phase, setPhase] = useState('loading');

  useEffect(() => {
    let cancelled = false;
    // Installed for the component's whole lifetime: scheduled near-expiry
    // refreshes settle long after the initial attempt, and any of them
    // returning session-expired must route into re-authentication.
    const unsubscribe = addBridgeSettlementListener((settlement) => {
      if (cancelled) {
        return;
      }
      if (settlement === 'session-expired') {
        reloadForReauthentication();
        return;
      }
      if (settlement === 'forbidden') {
        setPhase((prev) => (prev === 'ready' ? prev : 'forbidden'));
        return;
      }
      // Once the gate has opened, later settlements must never close it —
      // only an expired session (handled above) does.
      setPhase((prev) => {
        if (prev === 'ready') {
          return prev;
        }
        return settlement !== 'unreachable' ? 'ready' : 'connecting';
      });
    });

    const refreshAfterResume = () => {
      if (document.visibilityState !== 'visible') {
        return;
      }
      const refresh = refreshSessionBridgeAfterResume();
      if (refresh) {
        // A browser can suspend the scheduled timer past cookie expiry. Unmount
        // API consumers while the overdue bridge request installs its new
        // HttpOnly cookie; the settlement listener remounts them afterward.
        setPhase('connecting');
        void refresh;
      }
    };
    document.addEventListener('visibilitychange', refreshAfterResume);
    window.addEventListener('pageshow', refreshAfterResume);

    void startSessionBridge();

    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', refreshAfterResume);
      window.removeEventListener('pageshow', refreshAfterResume);
      unsubscribe();
    };
  }, []);

  if (phase !== 'ready') {
    return (
      <div className="min-h-screen flex items-center justify-center text-muted-foreground">
        {phase === 'forbidden'
          ? 'Browser authentication is unavailable for this session.'
          : phase === 'connecting'
            ? 'Connecting…'
            : 'Loading…'}
      </div>
    );
  }
  return children;
}
