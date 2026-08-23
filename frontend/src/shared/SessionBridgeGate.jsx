import React, { useEffect, useState } from 'react';
import {
  addBridgeSettlementListener,
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
      // Once the gate has opened, later settlements must never close it —
      // only an expired session (handled above) does.
      setPhase((prev) => {
        if (prev === 'ready') {
          return prev;
        }
        return settlement !== 'unreachable' ? 'ready' : 'connecting';
      });
    });

    void startSessionBridge();

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  if (phase !== 'ready') {
    return (
      <div className="min-h-screen flex items-center justify-center text-muted-foreground">
        {phase === 'connecting' ? 'Connecting…' : 'Loading…'}
      </div>
    );
  }
  return children;
}
