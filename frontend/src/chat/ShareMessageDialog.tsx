import React, { useEffect, useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { HomeTurnTarget } from './homeTurnActions';
import { messageShareEndpoint, ShareDialog } from './ShareConversationButton';
import { useConversationShareLink } from './useConversationShareLink';

interface ShareMessageDialogProps {
  conversationId: string;
  target: HomeTurnTarget;
  /** A reply streamed here has no stored id yet; the server names it by turn. */
  resolveMessageId: (target: HomeTurnTarget) => Promise<number>;
  onClose: () => void;
}

/** The share dialog for one message, once the message's stored id is known. */
export const ShareMessageDialog: React.FC<ShareMessageDialogProps> = ({
  conversationId,
  target,
  resolveMessageId,
  onClose,
}) => {
  const [messageId, setMessageId] = useState(target.internalId ?? null);
  const [error, setError] = useState<string | null>(null);
  const shareLink = useConversationShareLink(messageId === null ? null : String(messageId));

  useEffect(() => {
    if (target.internalId !== undefined) {
      return;
    }
    let current = true;
    resolveMessageId(target).then(
      (resolved) => {
        if (current) {
          setMessageId(resolved);
        }
      },
      (reason: unknown) => {
        if (current) {
          setError(reason instanceof Error ? reason.message : 'Could not find this message.');
        }
      }
    );
    return () => {
      current = false;
    };
  }, [target, resolveMessageId]);

  if (error) {
    return (
      <Dialog open onOpenChange={(open) => !open && onClose()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Share message</DialogTitle>
            <DialogDescription>{error}</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>
    );
  }
  if (messageId === null) {
    return null;
  }
  return (
    <ShareDialog
      endpoint={messageShareEndpoint(conversationId, messageId)}
      kind="message"
      shareLink={shareLink}
      open
      onOpenChange={(open) => !open && onClose()}
    />
  );
};
