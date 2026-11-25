import React, { useEffect, useRef, useState } from 'react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

const API = '/api/integrations/link';
const DEFAULT_POLL_INTERVAL_SECONDS = 5;

const SCOPE_LABELS = {
  'payment_methods.agentic': 'Agentic payments',
  'userinfo:read': 'Account profile',
};

const scopeLabel = (scope) => SCOPE_LABELS[scope] ?? scope;

const formatDate = (dateString) => {
  if (!dateString) {
    return null;
  }
  return new Date(dateString).toLocaleString();
};

const readError = async (response, fallback) => {
  const body = await response.json().catch(() => ({}));
  return body.detail || response.statusText || fallback;
};

const intervalToMs = (intervalSeconds) => {
  const seconds =
    typeof intervalSeconds === 'number' && Number.isFinite(intervalSeconds) && intervalSeconds > 0
      ? intervalSeconds
      : DEFAULT_POLL_INTERVAL_SECONDS;
  return seconds * 1000;
};

const LinkWalletCard = () => {
  const disconnectButtonRef = useRef(null);
  const mountedRef = useRef(true);
  const pollSessionRef = useRef(0);
  const pollTimerRef = useRef(null);
  const pollAbortRef = useRef(null);

  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [successBanner, setSuccessBanner] = useState(null);
  const [deviceFlow, setDeviceFlow] = useState(null);
  const [starting, setStarting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const stopPolling = () => {
    pollSessionRef.current += 1;
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (pollAbortRef.current) {
      pollAbortRef.current.abort();
      pollAbortRef.current = null;
    }
  };

  const fetchStatus = async () => {
    try {
      const response = await fetch(API, { credentials: 'include' });
      if (!response.ok) {
        throw new Error(
          await readError(
            response,
            `Failed to fetch Link integration status: ${response.statusText}`
          )
        );
      }
      const data = await response.json();
      if (mountedRef.current) {
        setStatus(data);
      }
    } catch (err) {
      console.error('Error fetching Link integration status:', err);
      if (mountedRef.current) {
        setError(err.message);
      }
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    fetchStatus();
    return () => {
      mountedRef.current = false;
      stopPolling();
    };
  }, []);

  const schedulePoll = (sessionId, flowId, intervalSeconds) => {
    if (!mountedRef.current || pollSessionRef.current !== sessionId) {
      return;
    }

    pollTimerRef.current = window.setTimeout(async () => {
      pollTimerRef.current = null;
      if (!mountedRef.current || pollSessionRef.current !== sessionId) {
        return;
      }

      const controller = new AbortController();
      pollAbortRef.current = controller;

      try {
        const response = await fetch(`${API}/device/poll`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ flow_id: flowId }),
          signal: controller.signal,
        });

        if (pollAbortRef.current === controller) {
          pollAbortRef.current = null;
        }

        if (!mountedRef.current || pollSessionRef.current !== sessionId) {
          return;
        }

        if (!response.ok) {
          throw new Error(await readError(response, 'Failed to check Link connection status.'));
        }

        const data = await response.json();
        if (!mountedRef.current || pollSessionRef.current !== sessionId) {
          return;
        }

        if (data.status === 'pending') {
          const nextInterval =
            typeof data.interval === 'number' && Number.isFinite(data.interval) && data.interval > 0
              ? data.interval
              : intervalSeconds;
          schedulePoll(sessionId, flowId, nextInterval);
          return;
        }

        if (data.status === 'connected') {
          stopPolling();
          setDeviceFlow(null);
          setSuccessBanner(
            data.provider_account_email
              ? `Link account (${data.provider_account_email}) connected successfully.`
              : 'Link account connected successfully.'
          );
          await fetchStatus();
          return;
        }

        if (data.status === 'expired') {
          stopPolling();
          setDeviceFlow(null);
          setError(data.detail || 'The Link sign-in code expired. Please try again.');
          return;
        }

        if (data.status === 'denied') {
          stopPolling();
          setDeviceFlow(null);
          setError(data.detail || 'Link authorization was denied.');
          return;
        }

        stopPolling();
        setDeviceFlow(null);
        setError(data.detail || 'Unexpected response while connecting Link account.');
      } catch (err) {
        if (pollAbortRef.current === controller) {
          pollAbortRef.current = null;
        }
        if (
          err?.name === 'AbortError' ||
          !mountedRef.current ||
          pollSessionRef.current !== sessionId
        ) {
          return;
        }
        stopPolling();
        setDeviceFlow(null);
        setError(err.message || 'Failed to check Link connection status.');
      }
    }, intervalToMs(intervalSeconds));
  };

  const startDeviceFlow = async () => {
    stopPolling();
    setStarting(true);
    setError(null);
    setSuccessBanner(null);

    try {
      const response = await fetch(`${API}/device/start`, {
        method: 'POST',
        credentials: 'include',
      });

      if (!mountedRef.current) {
        return;
      }

      if (!response.ok) {
        throw new Error(await readError(response, 'Failed to start Link connection.'));
      }

      const data = await response.json();
      if (!mountedRef.current) {
        return;
      }

      const sessionId = pollSessionRef.current + 1;
      pollSessionRef.current = sessionId;
      setDeviceFlow(data);
      schedulePoll(sessionId, data.flow_id, data.interval);
    } catch (err) {
      if (mountedRef.current) {
        setError(err.message || 'Failed to start Link connection.');
      }
    } finally {
      if (mountedRef.current) {
        setStarting(false);
      }
    }
  };

  const cancelDeviceFlow = () => {
    stopPolling();
    setDeviceFlow(null);
  };

  const disconnect = async () => {
    setDisconnecting(true);
    setError(null);

    try {
      const response = await fetch(API, {
        method: 'DELETE',
        credentials: 'include',
      });

      if (!response.ok && response.status !== 404) {
        throw new Error(await readError(response, `Failed to disconnect: ${response.statusText}`));
      }

      stopPolling();
      if (mountedRef.current) {
        setDeviceFlow(null);
        setSuccessBanner(null);
        setConfirmDisconnect(false);
      }
      await fetchStatus();
    } catch (err) {
      console.error('Error disconnecting Link account:', err);
      if (mountedRef.current) {
        setError(err.message);
      }
    } finally {
      if (mountedRef.current) {
        setDisconnecting(false);
      }
    }
  };

  const renderDeviceFlow = () => {
    if (!deviceFlow) {
      return null;
    }

    const verificationUrl = deviceFlow.verification_uri_complete || deviceFlow.verification_uri;

    return (
      <div className="space-y-3 rounded-md border p-4">
        <ol className="list-decimal list-inside text-sm space-y-2">
          <li>
            <a
              className="underline font-medium"
              href={verificationUrl}
              target="_blank"
              rel="noreferrer"
            >
              Open Link to authorize
            </a>{' '}
            in a new tab.
          </li>
          <li>Confirm that the code shown in Link matches:</li>
        </ol>
        <div className="rounded bg-muted px-3 py-2 text-center font-mono text-base font-semibold tracking-wide select-all">
          {deviceFlow.user_code}
        </div>
        <div className="flex items-center justify-between gap-2 pt-1">
          <span className="text-sm text-muted-foreground" role="status" aria-live="polite">
            Waiting for approval in Link...
          </span>
          <Button type="button" variant="outline" size="sm" onClick={cancelDeviceFlow}>
            Cancel
          </Button>
        </div>
      </div>
    );
  };

  return (
    <>
      <Card className="mt-6">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">Link Agent Wallet</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {successBanner && (
            <Alert className="border-green-200 bg-green-50 dark:border-green-800 dark:bg-green-950/30">
              <AlertDescription className="text-green-800 dark:text-green-200">
                {successBanner}
              </AlertDescription>
            </Alert>
          )}

          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {status && !status.enabled && (
            <div className="space-y-4">
              <Alert>
                <AlertDescription>
                  {status.reason ?? 'Link Agent Wallet is not available in this deployment.'}
                </AlertDescription>
              </Alert>

              {status.connected && (
                <div className="space-y-3">
                  {status.provider_account_email && (
                    <div>
                      <span className="text-sm font-medium">Account: </span>
                      <span className="text-sm break-all">{status.provider_account_email}</span>
                    </div>
                  )}
                  <p className="text-sm text-muted-foreground">
                    Your Link account is still connected. New connections are unavailable while the
                    integration is disabled, but you can remove your stored connection.
                  </p>
                  <Button
                    variant="destructive"
                    ref={disconnectButtonRef}
                    onClick={() => {
                      setError(null);
                      setConfirmDisconnect(true);
                    }}
                    disabled={disconnecting}
                  >
                    Disconnect
                  </Button>
                </div>
              )}
            </div>
          )}

          {status && status.enabled && !status.connected && (
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground">
                Connect your Stripe Link Agent Wallet to let the assistant use your saved payment
                methods for approved purchases.
              </p>
              {deviceFlow ? (
                renderDeviceFlow()
              ) : (
                <Button onClick={startDeviceFlow} disabled={starting}>
                  {starting ? 'Starting...' : 'Connect Link account'}
                </Button>
              )}
            </div>
          )}

          {status && status.enabled && status.connected && (
            <div className="space-y-4">
              {status.status === 'needs_reauth' && (
                <Alert variant="destructive">
                  <AlertDescription>
                    Your Link connection needs to be renewed. Please reconnect your account.
                  </AlertDescription>
                </Alert>
              )}

              {status.missing_configured_scopes && status.missing_configured_scopes.length > 0 && (
                <Alert>
                  <AlertDescription>
                    <p className="font-medium mb-1">Some permissions were not granted:</p>
                    <ul className="list-disc list-inside space-y-1">
                      {status.missing_configured_scopes.map((scope) => (
                        <li key={scope} className="text-sm">
                          {scopeLabel(scope)}
                        </li>
                      ))}
                    </ul>
                    <p className="text-sm mt-2">
                      Reconnect and approve all requested permissions to restore full functionality.
                    </p>
                  </AlertDescription>
                </Alert>
              )}

              <div className="space-y-2">
                {status.provider_account_email && (
                  <div>
                    <span className="text-sm font-medium">Account: </span>
                    <span className="text-sm break-all">{status.provider_account_email}</span>
                  </div>
                )}

                {status.last_used_at && (
                  <div>
                    <span className="text-sm font-medium">Last used: </span>
                    <span className="text-sm text-muted-foreground">
                      {formatDate(status.last_used_at)}
                    </span>
                  </div>
                )}

                {status.granted_scopes && status.granted_scopes.length > 0 && (
                  <div>
                    <p className="text-sm font-medium mb-1">Granted permissions:</p>
                    <div className="flex flex-wrap gap-1">
                      {status.granted_scopes.map((scope) => (
                        <Badge
                          key={scope}
                          variant="secondary"
                          className="text-xs max-w-full break-all whitespace-normal"
                        >
                          {scopeLabel(scope)}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}

                {status.require_taint_enforcement_waived && (
                  <p className="text-xs text-muted-foreground">
                    Note: taint enforcement has been waived by the operator for this deployment.
                  </p>
                )}
              </div>

              {deviceFlow ? (
                renderDeviceFlow()
              ) : (
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant={status.status === 'needs_reauth' ? 'default' : 'outline'}
                    onClick={startDeviceFlow}
                    disabled={starting || disconnecting}
                  >
                    {starting
                      ? 'Starting...'
                      : status.status === 'needs_reauth'
                        ? 'Reconnect Link account'
                        : 'Reconnect'}
                  </Button>
                  <Button
                    variant="destructive"
                    ref={disconnectButtonRef}
                    onClick={() => {
                      setError(null);
                      setConfirmDisconnect(true);
                    }}
                    disabled={disconnecting || starting}
                  >
                    Disconnect
                  </Button>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <AlertDialog
        open={confirmDisconnect}
        onOpenChange={(open) => {
          if (!disconnecting) {
            setConfirmDisconnect(open);
          }
        }}
      >
        <AlertDialogContent
          className="max-w-[calc(100vw-2rem)] sm:max-w-lg rounded-lg"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            disconnectButtonRef.current?.focus();
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect Link account?</AlertDialogTitle>
            <AlertDialogDescription>
              This will remove your Link Agent Wallet connection. The assistant will no longer be
              able to use your Link payment methods. You can reconnect at any time.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={disconnecting}>Cancel</AlertDialogCancel>
            <Button variant="destructive" onClick={disconnect} disabled={disconnecting}>
              {disconnecting ? 'Disconnecting...' : 'Yes, disconnect'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
};

export default LinkWalletCard;
