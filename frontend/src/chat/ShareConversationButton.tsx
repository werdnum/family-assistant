import { Link2Off, Share2 } from 'lucide-react';
import React, { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';

interface ShareConversationButtonProps {
  conversationId: string | null;
  hasMessages: boolean;
}

export const ShareConversationButton: React.FC<ShareConversationButtonProps> = ({
  conversationId,
  hasMessages,
}) => {
  const [active, setActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [manualShareUrl, setManualShareUrl] = useState<string | null>(null);
  const mutationStartedRef = useRef(false);

  useEffect(() => {
    setActive(false);
    setFeedback(null);
    setManualShareUrl(null);
    mutationStartedRef.current = false;
    if (!conversationId || !hasMessages) {
      return;
    }
    const controller = new AbortController();
    void fetch(`/api/v1/chat/conversations/${conversationId}/share`, {
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : Promise.reject(response)))
      .then((data: { active: boolean }) => {
        if (!mutationStartedRef.current) {
          setActive(data.active);
        }
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          console.error('Failed to load conversation share status:', error);
        }
      });
    return () => controller.abort();
  }, [conversationId, hasMessages]);

  if (!conversationId || !hasMessages) {
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
      setActive(true);
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
      setActive(false);
      setFeedback('Sharing stopped');
    } catch (error) {
      console.error('Failed to stop sharing conversation:', error);
      setFeedback('Could not stop sharing');
    } finally {
      setBusy(false);
    }
  };

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
        disabled={busy}
        aria-label={active ? 'Replace shared conversation link' : 'Share conversation'}
        title={active ? 'Replace shared conversation link' : 'Share conversation'}
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
