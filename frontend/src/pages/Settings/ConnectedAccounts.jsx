import React, { useEffect, useState } from 'react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

const SCOPE_LABELS = {
  'https://www.googleapis.com/auth/gmail.readonly': 'Gmail read access',
  'https://www.googleapis.com/auth/drive.readonly': 'Drive read access',
  'https://www.googleapis.com/auth/drive.metadata.readonly': 'Drive metadata access',
  openid: 'OpenID',
  email: 'Email identity',
};

const scopeLabel = (scope) => SCOPE_LABELS[scope] ?? scope;

const missingHumanLabel = (scope) => {
  if (scope.includes('gmail')) {
    return 'Gmail access not granted';
  }
  if (scope.includes('drive.readonly')) {
    return 'Drive access not granted';
  }
  if (scope.includes('drive.metadata')) {
    return 'Drive metadata access not granted';
  }
  return `Missing: ${scopeLabel(scope)}`;
};

const formatDate = (dateString) => {
  if (!dateString) {
    return null;
  }
  return new Date(dateString).toLocaleString();
};

const ConnectedAccounts = () => {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [successBanner, setSuccessBanner] = useState(null);
  const [disconnecting, setDisconnecting] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const fetchStatus = async () => {
    try {
      setError(null);
      const response = await fetch('/api/integrations/google', {
        credentials: 'include',
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch Google integration status: ${response.statusText}`);
      }

      const data = await response.json();
      setStatus(data);
    } catch (err) {
      console.error('Error fetching Google integration status:', err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const disconnect = async () => {
    setDisconnecting(true);
    setError(null);

    try {
      const response = await fetch('/api/integrations/google', {
        method: 'DELETE',
        credentials: 'include',
      });

      if (!response.ok && response.status !== 404) {
        const errorData = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(errorData.detail || `Failed to disconnect: ${response.statusText}`);
      }

      setConfirmDisconnect(false);
      await fetchStatus();
    } catch (err) {
      console.error('Error disconnecting Google account:', err);
      setError(err.message);
    } finally {
      setDisconnecting(false);
    }
  };

  const handleConnect = () => {
    window.location.href = '/api/integrations/google/authorize';
  };

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const googleParam = params.get('google');

    if (googleParam === 'connected') {
      setSuccessBanner('Google account connected successfully.');
    } else if (googleParam === 'error') {
      const message = params.get('message') ?? 'An error occurred during Google sign-in.';
      setError(message);
    }

    if (googleParam) {
      params.delete('google');
      params.delete('message');
      const cleanSearch = params.toString();
      const cleanUrl =
        window.location.pathname + (cleanSearch ? `?${cleanSearch}` : '') + window.location.hash;
      window.history.replaceState({}, '', cleanUrl);
    }

    fetchStatus();
  }, []);

  useEffect(() => {
    document.title = 'Connected Accounts - Family Assistant';

    if (!loading) {
      document.getElementById('app-root')?.setAttribute('data-app-ready', 'true');
    } else {
      document.getElementById('app-root')?.removeAttribute('data-app-ready');
    }

    return () => {
      document.getElementById('app-root')?.removeAttribute('data-app-ready');
    };
  }, [loading]);

  if (loading) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <h1 className="text-2xl font-bold mb-6">Connected Accounts</h1>
        <div className="text-muted-foreground">Loading...</div>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-2xl">
      <h1 className="text-2xl font-bold mb-6">Connected Accounts</h1>

      {successBanner && (
        <Alert className="mb-4 border-green-200 bg-green-50">
          <AlertDescription className="text-green-800">{successBanner}</AlertDescription>
        </Alert>
      )}

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">Google Account</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {status && !status.enabled && (
            <div className="space-y-4">
              <Alert>
                <AlertDescription>
                  {status.reason ?? 'Google integration is not available in this deployment.'}
                </AlertDescription>
              </Alert>

              {status.connected && (
                <div className="space-y-3">
                  <div>
                    <span className="text-sm font-medium">Account: </span>
                    <span className="text-sm">{status.provider_account_email ?? 'Unknown'}</span>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Your Google account is still connected. New connections are unavailable while
                    the integration is disabled, but you can remove your stored connection.
                  </p>
                  <Button
                    variant="destructive"
                    onClick={() => setConfirmDisconnect(true)}
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
                Connect your Google account to let the assistant search your Gmail and Drive on your
                behalf.
              </p>
              <Button onClick={handleConnect}>Connect Google account</Button>
            </div>
          )}

          {status && status.enabled && status.connected && (
            <div className="space-y-4">
              {status.status === 'needs_reauth' && (
                <Alert variant="destructive">
                  <AlertDescription>
                    Your Google connection needs to be renewed. Please reconnect your account.
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
                          {missingHumanLabel(scope)}
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
                <div>
                  <span className="text-sm font-medium">Account: </span>
                  <span className="text-sm">{status.provider_account_email ?? 'Unknown'}</span>
                </div>

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
                        <Badge key={scope} variant="secondary" className="text-xs">
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

              <div className="flex gap-2">
                <Button variant="outline" onClick={handleConnect}>
                  Reconnect
                </Button>
                <Button
                  variant="destructive"
                  onClick={() => setConfirmDisconnect(true)}
                  disabled={disconnecting}
                >
                  Disconnect
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {confirmDisconnect && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: 'rgba(0,0,0,0.4)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 50,
          }}
        >
          <div
            style={{
              background: 'var(--background, white)',
              borderRadius: '0.5rem',
              padding: '1.5rem',
              maxWidth: '28rem',
              width: '90%',
              boxShadow: '0 4px 24px rgba(0,0,0,0.2)',
            }}
          >
            <h3 className="text-lg font-semibold mb-2">Disconnect Google account?</h3>
            <p className="text-sm text-muted-foreground mb-4">
              This will remove your Google connection. The assistant will no longer be able to
              access your Gmail or Drive. You can reconnect at any time.
            </p>
            <div className="flex gap-2 justify-end">
              <Button variant="destructive" onClick={disconnect} disabled={disconnecting}>
                {disconnecting ? 'Disconnecting...' : 'Yes, disconnect'}
              </Button>
              <Button variant="secondary" onClick={() => setConfirmDisconnect(false)}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default ConnectedAccounts;
