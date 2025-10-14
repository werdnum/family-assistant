import React, { useState, useEffect, FormEvent, ChangeEvent } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';
import styles from './TokenManagement.module.css';

interface ApiToken {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  is_revoked: boolean;
}

interface CreatedTokenData extends ApiToken {
  full_token: string;
}

interface FormData {
  name: string;
  expires_at: string;
}

const TokenManagement: React.FC = () => {
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreateForm, setShowCreateForm] = useState<boolean>(false);
  const [createdToken, setCreatedToken] = useState<CreatedTokenData | null>(null);
  const [creating, setCreating] = useState<boolean>(false);
  const [revoking, setRevoking] = useState<Record<string, boolean>>({});
  const [tokenToRevoke, setTokenToRevoke] = useState<ApiToken | null>(null);

  // Form state
  const [formData, setFormData] = useState<FormData>({
    name: '',
    expires_at: '',
  });

  // Fetch tokens
  const fetchTokens = async () => {
    try {
      setError(null);
      const response = await fetch('/api/me/tokens', {
        credentials: 'include',
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch tokens: ${response.statusText}`);
      }

      const data = await response.json();
      setTokens(data);
    } catch (err: any) {
      console.error('Error fetching tokens:', err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Create new token
  const createToken = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setCreating(true);
    setError(null);

    try {
      const payload: { name: string; expires_at?: string } = { name: formData.name };
      if (formData.expires_at) {
        payload.expires_at = new Date(formData.expires_at).toISOString();
      }

      const response = await fetch('/api/me/tokens', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(errorData.detail || `Failed to create token: ${response.statusText}`);
      }

      const newToken = await response.json();
      setCreatedToken(newToken);
      setFormData({ name: '', expires_at: '' });
      setShowCreateForm(false);
      fetchTokens(); // Refresh the list
    } catch (err: any) {
      console.error('Error creating token:', err);
      setError(err.message);
    } finally {
      setCreating(false);
    }
  };

  // Revoke token
  const revokeToken = async (tokenId: string) => {
    setRevoking((prev) => ({ ...prev, [tokenId]: true }));
    setError(null);

    try {
      const response = await fetch(`/api/me/tokens/${tokenId}`, {
        method: 'DELETE',
        credentials: 'include',
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(errorData.detail || `Failed to revoke token: ${response.statusText}`);
      }

      // Refresh the tokens list
      fetchTokens();
      setTokenToRevoke(null);
    } catch (err: any) {
      console.error('Error revoking token:', err);
      setError(err.message);
    } finally {
      setRevoking((prev) => ({ ...prev, [tokenId]: false }));
    }
  };

  // Copy to clipboard
  const copyToClipboard = async (text: string) => {
    try {
      await window.navigator.clipboard.writeText(text);
      // You could add a toast notification here
    } catch (err) {
      console.error('Failed to copy to clipboard:', err);
      // Fallback for older browsers
      const textArea = document.createElement('textarea');
      textArea.value = text;
      document.body.appendChild(textArea);
      textArea.focus();
      textArea.select();
      try {
        document.execCommand('copy');
      } catch (fallbackErr) {
        console.error('Fallback copy failed:', fallbackErr);
      }
      document.body.removeChild(textArea);
    }
  };

  // Format date for display
  const formatDate = (dateString: string | null): string => {
    if (!dateString) {
      return 'Never';
    }
    return new Date(dateString).toLocaleString();
  };

  // Check if token is expired
  const isExpired = (expiresAt: string | null): boolean => {
    if (!expiresAt) {
      return false;
    }
    return new Date(expiresAt) <= new Date();
  };

  useEffect(() => {
    fetchTokens();
  }, []);

  // Set page title and coordinate data-app-ready with loading state
  useEffect(() => {
    document.title = 'API Tokens - Family Assistant';

    if (!loading) {
      document.getElementById('app-root')?.setAttribute('data-app-ready', 'true');
    } else {
      document.getElementById('app-root')?.removeAttribute('data-app-ready');
    }

    return () => {
      document.getElementById('app-root')?.removeAttribute('data-app-ready');
    };
  }, [loading]);

  // Handle form input changes
  const handleInputChange = (e: ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  if (loading) {
    return (
      <div className={styles.container}>
        <h1>API Token Management</h1>
        <div className={styles.loading}>Loading tokens...</div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>API Token Management</h1>
        <Button onClick={() => setShowCreateForm(!showCreateForm)} disabled={creating}>
          Create New Token
        </Button>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Created Token Display */}
      {createdToken && (
        <Card className="mb-6 border-green-200 bg-green-50">
          <CardHeader>
            <CardTitle className="text-green-800">Token Created Successfully!</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-green-700">
              <strong>Copy this token now - it won't be shown again:</strong>
            </p>
            <div className="flex gap-2 p-3 bg-gray-100 rounded-md">
              <code className="flex-1 text-sm break-all">{createdToken.full_token}</code>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => copyToClipboard(createdToken.full_token)}
              >
                Copy
              </Button>
            </div>
            <Button variant="ghost" onClick={() => setCreatedToken(null)}>
              Dismiss
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Create Token Form */}
      {showCreateForm && (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle>Create New Token</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={createToken} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="name">Token Name</Label>
                <Input
                  type="text"
                  id="name"
                  name="name"
                  value={formData.name}
                  onChange={handleInputChange}
                  required
                  placeholder="Enter a descriptive name for this token"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="expires_at">Expires At (Optional)</Label>
                <Input
                  type="datetime-local"
                  id="expires_at"
                  name="expires_at"
                  value={formData.expires_at}
                  onChange={handleInputChange}
                />
                <p className="text-sm text-muted-foreground">Leave blank for no expiration</p>
              </div>

              <div className="flex gap-2">
                <Button type="submit" disabled={creating || !formData.name.trim()}>
                  {creating ? 'Creating...' : 'Create Token'}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    setShowCreateForm(false);
                    setFormData({ name: '', expires_at: '' });
                  }}
                >
                  Cancel
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      {/* Tokens List */}
      <div className={styles.tokensSection}>
        <h2>Your Tokens ({tokens.length})</h2>

        {tokens.length === 0 ? (
          <div className={styles.emptyState}>
            <p>No API tokens found. Create your first token to get started.</p>
          </div>
        ) : (
          <div className={styles.tokensList}>
            {tokens.map((token) => (
              <div
                key={token.id}
                className={`${styles.tokenCard} ${token.is_revoked ? styles.revoked : ''} ${isExpired(token.expires_at) ? styles.expired : ''}`}
              >
                <div className={styles.tokenInfo}>
                  <h3>{token.name}</h3>
                  <div className={styles.tokenDetails}>
                    <span className={styles.prefix}>
                      Prefix: <code>{token.prefix}...</code>
                    </span>
                    <span className={styles.created}>Created: {formatDate(token.created_at)}</span>
                    <span className={styles.lastUsed}>
                      Last Used: {formatDate(token.last_used_at)}
                    </span>
                    {token.expires_at && (
                      <span
                        className={`${styles.expires} ${isExpired(token.expires_at) ? styles.expiredLabel : ''}`}
                      >
                        Expires: {formatDate(token.expires_at)}
                        {isExpired(token.expires_at) && ' (EXPIRED)'}
                      </span>
                    )}
                    <span
                      className={`${styles.status} ${token.is_revoked ? styles.revokedStatus : styles.activeStatus}`}
                    >
                      {token.is_revoked ? 'REVOKED' : 'ACTIVE'}
                    </span>
                  </div>
                </div>

                {!token.is_revoked && (
                  <div className={styles.tokenActions}>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => setTokenToRevoke(token)}
                      disabled={revoking[token.id]}
                    >
                      {revoking[token.id] ? 'Revoking...' : 'Revoke'}
                    </Button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Revocation Confirmation Modal */}
      {tokenToRevoke && (
        <div className={styles.modal}>
          <div className={styles.modalContent}>
            <h3>Confirm Token Revocation</h3>
            <p>
              Are you sure you want to revoke the token <strong>"{tokenToRevoke.name}"</strong>?
            </p>
            <p>This action cannot be undone and will immediately invalidate the token.</p>

            <div className={styles.modalActions}>
              <Button
                variant="destructive"
                onClick={() => revokeToken(tokenToRevoke.id)}
                disabled={revoking[tokenToRevoke.id]}
              >
                {revoking[tokenToRevoke.id] ? 'Revoking...' : 'Yes, Revoke Token'}
              </Button>
              <Button variant="secondary" onClick={() => setTokenToRevoke(null)}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default TokenManagement;
