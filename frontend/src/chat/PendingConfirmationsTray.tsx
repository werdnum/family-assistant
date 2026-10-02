import { AlertCircle } from 'lucide-react';
import React from 'react';
import { ConfirmationCard } from './ConfirmationCard';
import type { PendingToolConfirmation } from './ToolConfirmationContext';

interface PendingConfirmationsTrayProps {
  confirmations: PendingToolConfirmation[];
  loadError?: string | null;
  onConfirm: (requestId: string, approved: boolean) => Promise<void>;
  currentConversationId?: string | null;
  onOpenConversation?: (conversationId: string) => void;
}

const INTERFACE_LABELS: Record<string, string> = {
  telegram: 'Telegram',
  email: 'email',
  web: 'the web app',
  api: 'the API',
  ios: 'the iOS app',
};

function describeOrigin(
  confirmation: PendingToolConfirmation,
  currentConversationId?: string | null,
  onOpenConversation?: (conversationId: string) => void
): React.ReactNode {
  const conversationId = confirmation.conversation_id;
  if (typeof conversationId === 'string' && conversationId === currentConversationId) {
    return 'From this conversation';
  }
  const interfaceType = confirmation.origin_interface_type;
  if (
    typeof conversationId === 'string' &&
    interfaceType === 'web' &&
    onOpenConversation !== undefined
  ) {
    return (
      <>
        From another conversation ·{' '}
        <button
          type="button"
          className="font-medium text-foreground underline underline-offset-2 hover:no-underline"
          onClick={() => onOpenConversation(conversationId)}
        >
          Open it
        </button>
      </>
    );
  }
  if (typeof interfaceType === 'string' && interfaceType) {
    return `From ${INTERFACE_LABELS[interfaceType] ?? interfaceType}`;
  }
  return null;
}

export const PendingConfirmationsTray: React.FC<PendingConfirmationsTrayProps> = ({
  confirmations,
  loadError,
  onConfirm,
  currentConversationId,
  onOpenConversation,
}) => {
  if (confirmations.length === 0 && !loadError) {
    return null;
  }

  return (
    <section
      className="border-b bg-muted/40"
      aria-label="Pending approvals"
      data-testid="pending-confirmations-tray"
    >
      <div className="mx-auto flex max-h-[45vh] w-full max-w-5xl flex-col gap-2 overflow-y-auto px-4 py-3">
        {confirmations.length > 0 && (
          <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {confirmations.length === 1
              ? 'Waiting for your approval'
              : `${confirmations.length} actions waiting for your approval`}
          </h3>
        )}
        {loadError && (
          <div role="alert" className="flex items-center gap-2 text-xs text-destructive">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            {loadError}
          </div>
        )}
        {confirmations.map((confirmation) => (
          <ConfirmationCard
            key={confirmation.request_id}
            confirmation={confirmation}
            showToolName
            compact
            origin={describeOrigin(confirmation, currentConversationId, onOpenConversation)}
            onDecision={(approved) => onConfirm(confirmation.request_id, approved)}
          />
        ))}
      </div>
    </section>
  );
};
