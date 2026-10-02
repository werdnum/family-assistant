import { Share2 } from 'lucide-react';
import React, { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import type { ConversationShareLink } from './useConversationShareLink';

interface ShareConversationButtonProps {
  conversationId: string | null;
  hasPersistedMessages: boolean;
  shareLink: ConversationShareLink;
}

type ShareStatus = 'loading' | 'active' | 'inactive' | 'error';

export const ShareConversationButton: React.FC<ShareConversationButtonProps> = ({
  conversationId,
  hasPersistedMessages,
  shareLink,
}) =>
  conversationId && hasPersistedMessages ? (
    <ConversationShareDialog
      key={conversationId}
      conversationId={conversationId}
      shareLink={shareLink}
    />
  ) : null;

const ConversationShareDialog: React.FC<{
  conversationId: string;
  shareLink: ConversationShareLink;
}> = ({ conversationId, shareLink }) => {
  const [shareStatus, setShareStatus] = useState<ShareStatus>('loading');
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { url: shareUrl, setUrl: setShareUrl } = shareLink;
  const [copyFailed, setCopyFailed] = useState(false);
  const [statusRequestVersion, setStatusRequestVersion] = useState(0);
  const urlInputRef = useRef<HTMLInputElement>(null);
  const createButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetch(`/api/v1/chat/conversations/${conversationId}/share`, {
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : Promise.reject(response)))
      .then((data: { active: boolean }) => {
        setShareStatus(data.active ? 'active' : 'inactive');
        if (!data.active) {
          setShareUrl(null);
        }
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          console.error('Failed to load conversation share status:', error);
          setShareStatus('error');
        }
      });
    return () => controller.abort();
  }, [conversationId, statusRequestVersion, setShareUrl]);

  useEffect(() => {
    if (copyFailed) {
      urlInputRef.current?.focus();
      urlInputRef.current?.select();
    }
  }, [copyFailed]);

  useEffect(() => {
    if (shareStatus === 'inactive' && feedback) {
      createButtonRef.current?.focus();
    }
  }, [shareStatus, feedback]);

  const copyLink = async (url: string) => {
    setFeedback(null);
    setError(null);
    setCopyFailed(false);
    setBusy(true);
    try {
      await navigator.clipboard.writeText(url);
      setFeedback('Link copied');
    } catch (error) {
      console.error('Failed to copy conversation share link:', error);
      setCopyFailed(true);
      urlInputRef.current?.focus();
      urlInputRef.current?.select();
      setError('Could not copy the link. Copy the selected URL below or retry copying.');
    } finally {
      setBusy(false);
    }
  };

  const createShare = async () => {
    setBusy(true);
    setFeedback(null);
    setError(null);
    setCopyFailed(false);
    setShareUrl(null);
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
      setShareUrl(absoluteUrl);
      await copyLink(absoluteUrl);
    } catch (error) {
      console.error('Failed to share conversation:', error);
      setError('Could not create the link. Try again; creating a link replaces any previous link.');
    } finally {
      setBusy(false);
    }
  };

  const revokeShare = async () => {
    setBusy(true);
    setFeedback(null);
    setError(null);
    try {
      const response = await fetch(`/api/v1/chat/conversations/${conversationId}/share`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error(`Revoke request failed with status ${response.status}`);
      }
      setShareStatus('inactive');
      setShareUrl(null);
      setCopyFailed(false);
      setFeedback('Sharing stopped. The link no longer works.');
    } catch (error) {
      console.error('Failed to stop sharing conversation:', error);
      setError('Could not stop sharing. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const active = shareStatus === 'active';

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          aria-label="Share conversation"
          title="Share conversation"
        >
          <Share2 className="h-4 w-4" />
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Share conversation</DialogTitle>
          <DialogDescription>
            Anyone with the link who is signed in as an authorized Family Assistant user can read
            this conversation, including messages added later. They cannot reply or approve tool
            calls.
          </DialogDescription>
        </DialogHeader>
        {shareStatus === 'loading' ? (
          <p role="status">Loading sharing status…</p>
        ) : shareStatus === 'error' ? (
          <>
            <p role="alert" className="text-sm text-destructive">
              Could not load sharing status
            </p>
            <Button
              variant="outline"
              onClick={() => {
                setShareStatus('loading');
                setStatusRequestVersion((version) => version + 1);
              }}
            >
              Retry loading sharing status
            </Button>
          </>
        ) : (
          <>
            <p className="text-sm">
              {active
                ? 'Sharing is on. Replacing the link immediately stops the previous link from working, even if copying fails. Stopping sharing makes the current link unavailable.'
                : 'Sharing is off. Create a link to share a read-only view.'}
            </p>
            {active && !shareUrl && (
              <p className="text-sm text-muted-foreground">
                The existing URL cannot be retrieved. Replace it to get a new link.
              </p>
            )}
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <p role="status" className="text-sm">
              {feedback}
            </p>
            {shareUrl && (
              <div className="space-y-2">
                <label htmlFor={`share-url-${conversationId}`} className="text-sm font-medium">
                  Share link
                </label>
                <Input
                  id={`share-url-${conversationId}`}
                  ref={urlInputRef}
                  readOnly
                  value={shareUrl}
                  onFocus={(event) => event.currentTarget.select()}
                />
                <div className="flex flex-wrap items-center gap-3">
                  <Button onClick={() => copyLink(shareUrl)} disabled={busy}>
                    {copyFailed ? 'Retry copy' : 'Copy link'}
                  </Button>
                  <a href={shareUrl} target="_blank" rel="noreferrer" className="text-sm underline">
                    Open read-only view
                  </a>
                </div>
                <p className="text-xs text-muted-foreground">
                  This URL is kept only while this conversation stays open here. Copying it does not
                  replace it; another window or device can replace or revoke it.
                </p>
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                ref={createButtonRef}
                variant={active ? 'outline' : 'default'}
                onClick={createShare}
                disabled={busy}
              >
                {active ? 'Replace and copy link' : 'Create and copy link'}
              </Button>
              {active && (
                <Button variant="outline" onClick={revokeShare} disabled={busy}>
                  Stop sharing
                </Button>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
};
