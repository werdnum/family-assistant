import React, { useState, useEffect } from 'react';
import styles from './TokenManagement.module.css';

const TokenManagement = () => {
  const [tokens, setTokens] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createdToken, setCreatedToken] = useState(null);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState({});
  const [tokenToRevoke, setTokenToRevoke] = useState(null);

  // Form state
  const [formData, setFormData] = useState({
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
    } catch (err) {
      console.error('Error fetching tokens:', err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Create new token
  const createToken = async (e) => {
    e.preventDefault();
    setCreating(true);
    setError(null);

    try {
      const payload = { name: formData.name };
      if (formData.expires_at) {
        payload.expires_at = formData.expires_at;
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
    } catch (err) {
      console.error('Error creating token:', err);
      setError(err.message);
    } finally {
      setCreating(false);
    }
  };

  // Revoke token
  const revokeToken = async (tokenId) => {
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
    } catch (err) {
      console.error('Error revoking token:', err);
      setError(err.message);
    } finally {
      setRevoking((prev) => ({ ...prev, [tokenId]: false }));
    }
  };

  // Copy to clipboard
  const copyToClipboard = async (text) => {
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
  const formatDate = (dateString) => {
    if (!dateString) {
      return 'Never';
    }
    return new Date(dateString).toLocaleString();
  };

  // Check if token is expired
  const isExpired = (expiresAt) => {
    if (!expiresAt) {
      return false;
    }
    return new Date(expiresAt) <= new Date();
  };

  useEffect(() => {
    fetchTokens();
  }, []);

  // Handle form input changes
  const handleInputChange = (e) => {
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
        <button
          className={styles.createButton}
          onClick={() => setShowCreateForm(!showCreateForm)}
          disabled={creating}
        >
          Create New Token
        </button>
      </div>

      {error && <div className={styles.error}>{error}</div>}

      {/* Created Token Display */}
      {createdToken && (
        <div className={styles.createdToken}>
          <h3>Token Created Successfully!</h3>
          <p>
            <strong>Copy this token now - it won't be shown again:</strong>
          </p>
          <div className={styles.tokenDisplay}>
            <code>{createdToken.full_token}</code>
            <button
              className={styles.copyButton}
              onClick={() => copyToClipboard(createdToken.full_token)}
            >
              Copy
            </button>
          </div>
          <button className={styles.dismissButton} onClick={() => setCreatedToken(null)}>
            Dismiss
          </button>
        </div>
      )}

      {/* Create Token Form */}
      {showCreateForm && (
        <div className={styles.createForm}>
          <h3>Create New Token</h3>
          <form onSubmit={createToken}>
            <div className={styles.formGroup}>
              <label htmlFor="name">Token Name:</label>
              <input
                type="text"
                id="name"
                name="name"
                value={formData.name}
                onChange={handleInputChange}
                required
                placeholder="Enter a descriptive name for this token"
              />
            </div>

            <div className={styles.formGroup}>
              <label htmlFor="expires_at">Expires At (Optional):</label>
              <input
                type="datetime-local"
                id="expires_at"
                name="expires_at"
                value={formData.expires_at}
                onChange={handleInputChange}
                placeholder="Leave blank for no expiration"
              />
            </div>

            <div className={styles.formActions}>
              <button
                type="submit"
                disabled={creating || !formData.name.trim()}
                className={styles.submitButton}
              >
                {creating ? 'Creating...' : 'Create Token'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setShowCreateForm(false);
                  setFormData({ name: '', expires_at: '' });
                }}
                className={styles.cancelButton}
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
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
                    <button
                      className={styles.revokeButton}
                      onClick={() => setTokenToRevoke(token)}
                      disabled={revoking[token.id]}
                    >
                      {revoking[token.id] ? 'Revoking...' : 'Revoke'}
                    </button>
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
              <button
                className={styles.confirmRevokeButton}
                onClick={() => revokeToken(tokenToRevoke.id)}
                disabled={revoking[tokenToRevoke.id]}
              >
                {revoking[tokenToRevoke.id] ? 'Revoking...' : 'Yes, Revoke Token'}
              </button>
              <button className={styles.cancelButton} onClick={() => setTokenToRevoke(null)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default TokenManagement;
