import React, { useEffect, useState } from 'react';
import { addBridgeSettlementListener, startSessionBridge } from '../api/browserTokenClient';

/**
 * Delays rendering authenticated children until the session→JWT bridge has
 * actually established credentials: a successful response, or an explicit 401
 * (expired OIDC session — proceeding is correct there because the existing
 * auth flow owns the re-login UX). On transient failures the backoff loop in
 * browserTokenClient keeps retrying and children stay gated behind a
 * "Connecting…" placeholder, so their mount effects don't race ahead of the
 * Set-Cookie and hit unrecoverable 401s.
 */
export default function SessionBridgeGate({ children }) {
  const [phase, setPhase] = useState('loading');

  useEffect(() => {
    let cancelled = false;
    let unsubscribe = () => {};

    const openGate = () => {
      if (!cancelled) {
        setPhase('ready');
      }
    };

    void startSessionBridge().then((settlement) => {
      if (cancelled) {
        return;
      }
      if (settlement !== 'unreachable') {
        openGate();
        return;
      }
      setPhase('connecting');
      unsubscribe = addBridgeSettlementListener((next) => {
        if (next !== 'unreachable') {
          unsubscribe();
          openGate();
        }
      });
    });

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
