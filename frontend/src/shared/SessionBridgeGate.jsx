import React, { useEffect, useState } from 'react';
import { startSessionBridge } from '../api/browserTokenClient';

/**
 * Delays rendering authenticated children until the session→JWT bridge has
 * attempted to set its cookie, so mount-time API calls don't race the gateway
 * ahead of the Set-Cookie on a fresh session. Proceeds once the attempt
 * settles — including a 401 (expired OIDC session), where the existing auth
 * flow owns the re-login UX.
 */
export default function SessionBridgeGate({ children }) {
  const [bridgeSettled, setBridgeSettled] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void startSessionBridge().finally(() => {
      if (!cancelled) {
        setBridgeSettled(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!bridgeSettled) {
    return (
      <div className="min-h-screen flex items-center justify-center text-muted-foreground">
        Loading…
      </div>
    );
  }
  return children;
}
