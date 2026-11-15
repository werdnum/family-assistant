import React, { useEffect, useState } from 'react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';

const API = '/api/integrations/chatgpt';

const readError = async (response) => {
  const body = await response.json().catch(() => ({}));
  return body.detail || response.statusText || 'Request failed';
};

const ChatGPTPlanCard = () => {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [authorizeUrl, setAuthorizeUrl] = useState(null);
  const [callbackUrl, setCallbackUrl] = useState('');
  const [busy, setBusy] = useState(false);

  const fetchStatus = async () => {
    const response = await fetch(API, { credentials: 'include' });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    setStatus(await response.json());
  };

  useEffect(() => {
    fetchStatus().catch((err) => setError(err.message));
  }, []);

  const run = async (action) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const startSignIn = (useSavedAccount) =>
    run(async () => {
      const response = await fetch(`${API}/sign-in`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ use_saved_account: useSavedAccount }),
      });
      if (!response.ok) {
        throw new Error(await readError(response));
      }
      const data = await response.json();
      setCallbackUrl('');
      setAuthorizeUrl(data.authorize_url);
    });

  const completeSignIn = (event) => {
    event.preventDefault();
    return run(async () => {
      const response = await fetch(`${API}/callback`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ callback_url: callbackUrl }),
      });
      if (!response.ok) {
        throw new Error(await readError(response));
      }
      setStatus(await response.json());
      setAuthorizeUrl(null);
      setNotice('ChatGPT account signed in.');
    });
  };

  const signOut = () =>
    run(async () => {
      const response = await fetch(API, { method: 'DELETE', credentials: 'include' });
      if (!response.ok) {
        throw new Error(await readError(response));
      }
      const data = await response.json();
      setNotice(
        data.revocation_confirmed
          ? 'Signed out of ChatGPT.'
          : 'Signed out here, but OpenAI did not confirm it. You can also disconnect Family Assistant in ChatGPT settings.'
      );
      await fetchStatus();
    });

  const hasSavedAccount = Boolean(status?.status);
  const signedIn = status?.status === 'active';

  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>ChatGPT plan</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {notice && (
          <Alert>
            <AlertDescription>{notice}</AlertDescription>
          </Alert>
        )}
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {status && !status.enabled && (
          <Alert>
            <AlertDescription>
              {status.reason ?? 'ChatGPT sign-in is not available in this deployment.'}
            </AlertDescription>
          </Alert>
        )}

        {status?.enabled && (
          <>
            <p className="text-sm text-muted-foreground">
              Sign in with a ChatGPT Plus or Pro account and the assistant runs its OpenAI models on
              that plan&apos;s usage, using the API key only when the plan can&apos;t serve a
              request. One account serves the whole household.
            </p>

            {status.status === 'needs_reauth' && (
              <Alert variant="destructive">
                <AlertDescription>
                  The ChatGPT sign-in has expired or was revoked. Sign in again to keep using it.
                </AlertDescription>
              </Alert>
            )}

            {status.email && (
              <div className="text-sm space-y-1">
                <div>
                  <span className="font-medium">Account: </span>
                  <span className="break-all">{status.email}</span>
                  {!signedIn && <span className="text-muted-foreground"> (signed out)</span>}
                </div>
                {status.last_used_at && (
                  <div className="text-muted-foreground">
                    Last used: {new Date(status.last_used_at).toLocaleString()}
                  </div>
                )}
                {signedIn && (
                  <a className="underline" href={status.usage_url} target="_blank" rel="noreferrer">
                    View usage in ChatGPT
                  </a>
                )}
              </div>
            )}

            {!authorizeUrl && (
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => startSignIn(true)} disabled={busy}>
                  {hasSavedAccount ? 'Sign in again with ChatGPT' : 'Continue with ChatGPT'}
                </Button>
                {hasSavedAccount && (
                  <Button variant="outline" onClick={() => startSignIn(false)} disabled={busy}>
                    Use a different account
                  </Button>
                )}
                {signedIn && (
                  <Button variant="destructive" onClick={signOut} disabled={busy}>
                    Sign out
                  </Button>
                )}
              </div>
            )}

            {authorizeUrl && (
              <form className="space-y-3" onSubmit={completeSignIn}>
                <ol className="list-decimal list-inside text-sm space-y-1">
                  <li>
                    <a className="underline" href={authorizeUrl} target="_blank" rel="noreferrer">
                      Open ChatGPT sign-in
                    </a>{' '}
                    and approve Family Assistant.
                  </li>
                  <li>
                    The tab then goes to a page that won&apos;t load (its address starts with{' '}
                    <code className="break-all">{status.redirect_uri}</code>). That&apos;s expected.
                  </li>
                  <li>Copy that page&apos;s full address and paste it here.</li>
                </ol>
                <Input
                  aria-label="Address from the sign-in tab"
                  placeholder={`${status.redirect_uri}?code=…`}
                  value={callbackUrl}
                  onChange={(event) => setCallbackUrl(event.target.value)}
                />
                <div className="flex gap-2">
                  <Button type="submit" disabled={busy || !callbackUrl.trim()}>
                    Finish signing in
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setAuthorizeUrl(null)}
                    disabled={busy}
                  >
                    Cancel
                  </Button>
                </div>
              </form>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
};

export default ChatGPTPlanCard;
