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
import type { BackendConversationMessage } from './types';
import { useConversationShareLink } from './useConversationShareLink';

interface ShareMessageDialogProps {
  conversationId: string;
  target: HomeTurnTarget;
  /** A reply streamed here has no stored id yet; the server names it by turn. */
  resolveMessageId: (target: HomeTurnTarget) => Promise<number>;
  onClose: () => void;
}

/**
 * The stored reply of a turn: its last assistant row with something to show.
 * Null when there is none yet, never another row of the turn, because a link
 * shows exactly the row it names and the turn's first row is the question.
 */
export function sharedReplyId(rows: BackendConversationMessage[], turnId: string): number | null {
  const replies = rows.filter(
    (row) =>
      row.turn_id === turnId &&
      row.role === 'assistant' &&
      (Boolean(row.content) || (row.attachments?.length ?? 0) > 0)
  );
  const reply = replies[replies.length - 1];
  return reply ? Number(reply.internal_id) : null;
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
