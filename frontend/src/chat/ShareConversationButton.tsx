import { Link2Off, RefreshCw, Share2 } from 'lucide-react';
import React, { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';

interface ShareConversationButtonProps {
  conversationId: string | null;
  hasPersistedMessages: boolean;
}

type ShareStatus = 'loading' | 'active' | 'inactive' | 'error';

export const ShareConversationButton: React.FC<ShareConversationButtonProps> = ({
  conversationId,
  hasPersistedMessages,
}) => {
  const [shareStatus, setShareStatus] = useState<ShareStatus>('loading');
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [manualShareUrl, setManualShareUrl] = useState<string | null>(null);
  const [statusRequestVersion, setStatusRequestVersion] = useState(0);
  const mutationStartedRef = useRef(false);

  useEffect(() => {
    setShareStatus('loading');
    setFeedback(null);
    setManualShareUrl(null);
    mutationStartedRef.current = false;
    if (!conversationId || !hasPersistedMessages) {
      return;
    }
    const controller = new AbortController();
    void fetch(`/api/v1/chat/conversations/${conversationId}/share`, {
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : Promise.reject(response)))
      .then((data: { active: boolean }) => {
        if (!mutationStartedRef.current) {
          setShareStatus(data.active ? 'active' : 'inactive');
        }
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          console.error('Failed to load conversation share status:', error);
          if (!mutationStartedRef.current) {
            setShareStatus('error');
          }
        }
      });
    return () => controller.abort();
  }, [conversationId, hasPersistedMessages, statusRequestVersion]);

  if (!conversationId || !hasPersistedMessages) {
    return null;
  }

  const createShare = async () => {
    mutationStartedRef.current = true;
    setBusy(true);
    setFeedback(null);
    setManualShareUrl(null);
    try {
      const response = await fetch(`/api/v1/chat/conversations/${conversationId}/share`, {
        method: 'POST',
      });
      if (!response.ok) {
        throw new Error(`Share request failed with status ${response.status}`);
      }
      const data = (await response.json()) as { share_url: string };
      const absoluteUrl = new URL(data.share_url, window.location.origin).toString();
      setShareStatus('active');
      try {
        await navigator.clipboard.writeText(absoluteUrl);
        setFeedback('Link copied');
      } catch (error) {
        console.error('Failed to copy conversation share link:', error);
        setManualShareUrl(absoluteUrl);
        setFeedback('Open the link to copy it');
      }
    } catch (error) {
      console.error('Failed to share conversation:', error);
      setFeedback('Could not share conversation');
    } finally {
      setBusy(false);
    }
  };

  const revokeShare = async () => {
    mutationStartedRef.current = true;
    setBusy(true);
    setFeedback(null);
    setManualShareUrl(null);
    try {
      const response = await fetch(`/api/v1/chat/conversations/${conversationId}/share`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error(`Revoke request failed with status ${response.status}`);
      }
      setShareStatus('inactive');
      setFeedback('Sharing stopped');
    } catch (error) {
      console.error('Failed to stop sharing conversation:', error);
      setFeedback('Could not stop sharing');
    } finally {
      setBusy(false);
    }
  };

  const retryShareStatus = () => {
    mutationStartedRef.current = false;
    setStatusRequestVersion((version) => version + 1);
  };

  const active = shareStatus === 'active';

  if (shareStatus === 'error') {
    return (
      <div className="flex items-center gap-1" aria-live="polite">
        <span className="hidden text-xs text-destructive sm:inline">
          Could not load sharing status
        </span>
        <Button
          variant="ghost"
          size="sm"
          onClick={retryShareStatus}
          aria-label="Retry loading sharing status"
          title="Retry loading sharing status"
        >
          <RefreshCw className="h-4 w-4" />
        </Button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1" aria-live="polite">
      {feedback && (
        <span className="hidden text-xs text-muted-foreground sm:inline">{feedback}</span>
      )}
      {manualShareUrl && (
        <a
          href={manualShareUrl}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-primary underline"
        >
          Open
        </a>
      )}
      <Button
        variant="ghost"
        size="sm"
        onClick={createShare}
        disabled={busy || shareStatus === 'loading'}
        aria-label={
          shareStatus === 'loading'
            ? 'Loading sharing status'
            : active
              ? 'Replace shared conversation link'
              : 'Share conversation'
        }
        title={
          shareStatus === 'loading'
            ? 'Loading sharing status'
            : active
              ? 'Replace shared conversation link'
              : 'Share conversation'
        }
      >
        <Share2 className="h-4 w-4" />
      </Button>
      {active && (
        <Button
          variant="ghost"
          size="sm"
          onClick={revokeShare}
          disabled={busy}
          aria-label="Stop sharing conversation"
          title="Stop sharing conversation"
        >
          <Link2Off className="h-4 w-4" />
        </Button>
      )}
    </div>
  );
};
