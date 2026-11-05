import { Share2 } from 'lucide-react';
import React, { type ReactNode, useEffect, useId, useRef, useState } from 'react';
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

/** What a link shares, which decides what the dialog promises about it. */
export type ShareKind = 'conversation' | 'home' | 'thread' | 'message';

interface ShareConversationButtonProps {
  conversationId: string | null;
  hasPersistedMessages: boolean;
  shareLink: ConversationShareLink;
  /** A conversation unless it is Home or a thread. */
  kind?: Exclude<ShareKind, 'message'>;
}

interface ShareDialogProps {
  /** The share resource: GET for status, POST to rotate, DELETE to revoke. */
  endpoint: string;
  kind: ShareKind;
  shareLink: ConversationShareLink;
  /** Opens the dialog; omitted when the caller controls it with `open`. */
  trigger?: ReactNode;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

type ShareStatus = 'loading' | 'active' | 'inactive' | 'error';

const READ_ONLY = 'They cannot reply or approve tool calls.';
const WHO_CAN_READ = 'Anyone with the link who is signed in as an authorized Family Assistant user';

const SHARE_COPY: Record<ShareKind, { title: string; subject: string; description: string }> = {
  conversation: {
    title: 'Share conversation',
    subject: 'conversation',
    description: `${WHO_CAN_READ} can read this conversation, including messages added later. ${READ_ONLY}`,
  },
  home: {
    title: 'Share all of Home',
    subject: 'Home',
    description: `${WHO_CAN_READ} can read everything in Home, including messages added later. To share less, use the share action on a message or a thread. ${READ_ONLY}`,
  },
  thread: {
    title: 'Share thread',
    subject: 'thread',
    description: `${WHO_CAN_READ} can read this thread, including messages added later. ${READ_ONLY}`,
  },
  message: {
    title: 'Share message',
    subject: 'message',
    description: `${WHO_CAN_READ} can read this message, without the rest of the conversation or how the assistant worked it out. ${READ_ONLY}`,
  },
};

export const ShareConversationButton: React.FC<ShareConversationButtonProps> = ({
  conversationId,
  hasPersistedMessages,
  shareLink,
  kind = 'conversation',
}) =>
  conversationId && hasPersistedMessages ? (
    <ShareDialog
      key={conversationId}
      endpoint={conversationShareEndpoint(conversationId)}
      kind={kind}
      shareLink={shareLink}
      trigger={
        <Button
          variant="ghost"
          size="sm"
          aria-label={SHARE_COPY[kind].title}
          title={SHARE_COPY[kind].title}
        >
          <Share2 className="h-4 w-4" />
        </Button>
      }
    />
  ) : null;

export function conversationShareEndpoint(conversationId: string): string {
  return `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/share`;
}

export function messageShareEndpoint(conversationId: string, messageId: number): string {
  return `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/messages/${messageId}/share`;
}

/**
 * Create, copy, replace and revoke one read-only link. Nothing is requested
 * until it opens, so a list of cards can each hold one.
 */
export const ShareDialog: React.FC<ShareDialogProps> = ({
  endpoint,
  kind,
  shareLink,
  trigger,
  open: controlledOpen,
  onOpenChange,
}) => {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = controlledOpen ?? uncontrolledOpen;
  const [shareStatus, setShareStatus] = useState<ShareStatus>('loading');
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { url: shareUrl, setUrl: setShareUrl } = shareLink;
  const [copyFailed, setCopyFailed] = useState(false);
  const [statusRequestVersion, setStatusRequestVersion] = useState(0);
  const urlInputId = useId();
  const urlInputRef = useRef<HTMLInputElement>(null);
  const createButtonRef = useRef<HTMLButtonElement>(null);

  const needsStatus = open && shareStatus === 'loading';
  useEffect(() => {
    if (!needsStatus) {
      return;
    }
    const controller = new AbortController();
    void fetch(endpoint, {
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
  }, [endpoint, needsStatus, statusRequestVersion, setShareUrl]);

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
      const response = await fetch(endpoint, {
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
      const response = await fetch(endpoint, {
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
  const copy = SHARE_COPY[kind];

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setUncontrolledOpen(next);
        onOpenChange?.(next);
      }}
    >
      {trigger && <DialogTrigger asChild>{trigger}</DialogTrigger>}
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>{copy.description}</DialogDescription>
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
                <label htmlFor={urlInputId} className="text-sm font-medium">
                  Share link
                </label>
                <Input
                  id={urlInputId}
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
                  This URL is kept only while this {copy.subject} stays open here. Copying it does
                  not replace it; another window or device can replace or revoke it.
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
