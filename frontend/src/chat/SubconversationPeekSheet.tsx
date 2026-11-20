import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import {
  AlertCircleIcon,
  ArrowLeftIcon,
  CheckCircle2Icon,
  Loader2Icon,
  LockIcon,
  RadioIcon,
} from 'lucide-react';
import React, { useCallback, useMemo } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { type ChatControls, ChatControlsContext } from './chatControls';
import { LiveBrowserView } from './LiveBrowserView';
import { delegationStatusLabel, formatStartedAgo } from './PendingDelegationsChip';
import { getProfileDisplayName } from './profileNames';
import { useProfiles } from './profilesContext';
import { convertSubconversationMessage } from './subconversationMessages';
import { Thread } from './Thread';
import { ThreadErrorBoundary } from './ThreadErrorBoundary';
import { ToolConfirmationProvider } from './ToolConfirmationContext';
import type { PendingDelegation } from './usePendingDelegations';
import { useSubconversationPeek } from './useSubconversationPeek';

interface SubconversationPeekSheetProps {
  conversationId: string | null;
  /** Stack of peeked delegations (supports drilling into nested child delegations). */
  delegationStack: PendingDelegation[];
  onPushDelegation: (delegation: PendingDelegation) => void;
  onPopDelegation: () => void;
  onClose: () => void;
}

const noopHandleConfirmation = async () => {
  // Read-only peek view never resolves confirmations inline.
};

const noopSubmitSteer = async () => 'finished' as const;

const noopOnNew = async () => {
  // Read-only peek view does not accept new messages.
};

function peekStatusLabel(
  delegation: PendingDelegation | null,
  isStreaming: boolean
): { label: string; tone: 'active' | 'done' | 'failed' } {
  if (!delegation) {
    return isStreaming
      ? { label: 'Working on it', tone: 'active' }
      : { label: 'Live view', tone: 'active' };
  }
  if (delegation.status === 'completed') {
    return { label: 'Completed', tone: 'done' };
  }
  if (delegation.status === 'failed') {
    return { label: 'Failed', tone: 'failed' };
  }
  return { label: delegationStatusLabel(delegation), tone: 'active' };
}

/**
 * Slide-over read-only live view into a running delegated subconversation.
 * Reuses `<Thread readOnly />` so messages, markdown, tool call groups, and
 * inline attachments look identical to a regular conversation while streaming.
 */
export const SubconversationPeekSheet: React.FC<SubconversationPeekSheetProps> = ({
  conversationId,
  delegationStack,
  onPushDelegation,
  onPopDelegation,
  onClose,
}) => {
  const seedDelegation =
    delegationStack.length > 0 ? delegationStack[delegationStack.length - 1] : null;
  const subconversationId = seedDelegation?.subconversation_id ?? null;
  const isOpen = Boolean(conversationId && seedDelegation && subconversationId);

  const peek = useSubconversationPeek(
    isOpen ? conversationId : null,
    isOpen ? subconversationId : null,
    seedDelegation
  );

  const { profilesById } = useProfiles();
  const activeDelegation = peek.delegation ?? seedDelegation;
  const displayName = activeDelegation
    ? getProfileDisplayName(activeDelegation.target_profile_id)
    : 'Subconversation';
  const statusInfo = peekStatusLabel(activeDelegation, peek.isStreaming);
  const isBrowserProfile = Boolean(
    activeDelegation?.target_profile_id &&
      (profilesById[activeDelegation.target_profile_id]?.has_browser_view ||
        activeDelegation.target_profile_id.includes('browser'))
  );

  const convertMessage = useCallback(convertSubconversationMessage, []);

  const runtime = useExternalStoreRuntime({
    messages: peek.messages,
    isRunning: peek.isStreaming,
    isSendDisabled: true,
    onNew: noopOnNew,
    // @ts-expect-error - assistant-ui convertMessage return type mismatch
    convertMessage,
  });

  const peekControls = useMemo<ChatControls>(
    () => ({
      submitSteer: noopSubmitSteer,
      steerError: null,
      hasOlderMessages: peek.hasOlderMessages,
      olderMessagesStatus: peek.olderMessagesStatus,
      loadOlderMessages: peek.loadOlderMessages,
      pendingDelegations: peek.childDelegations,
      onPeekDelegation: onPushDelegation,
      conversationLoadStatus: peek.loadStatus,
      retryConversationLoad: peek.retryLoad,
      sendAsThread: null,
    }),
    [
      peek.hasOlderMessages,
      peek.olderMessagesStatus,
      peek.loadOlderMessages,
      peek.childDelegations,
      peek.loadStatus,
      peek.retryLoad,
      onPushDelegation,
    ]
  );

  const confirmationContextValue = useMemo(
    () => ({
      pendingConfirmations: new Map(),
      handleConfirmation: noopHandleConfirmation,
    }),
    []
  );

  const startedAtIso = activeDelegation?.started_at ?? activeDelegation?.created_at ?? null;
  const startedLabel = startedAtIso ? formatStartedAgo(startedAtIso, Date.now()) : null;

  const emptyState = (
    <div
      className="flex flex-col gap-4 px-4 py-8 md:px-6 max-w-3xl mx-auto"
      data-testid="subconversation-peek-empty"
    >
      {peek.requestText && (
        <div className="rounded-2xl bg-muted/60 px-4 py-3 text-sm text-foreground">
          <p className="mb-1 text-xs font-medium text-muted-foreground">Delegated request</p>
          <p className="whitespace-pre-wrap break-words">{peek.requestText}</p>
        </div>
      )}
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        {statusInfo.tone === 'active' ? (
          <>
            <Loader2Icon className="h-4 w-4 animate-spin" aria-hidden="true" />
            <span>
              {activeDelegation?.status === 'awaiting_remote'
                ? `${displayName} is running remotely…`
                : `Waiting for ${displayName} to begin…`}
            </span>
          </>
        ) : (
          <span>No messages recorded in this subconversation.</span>
        )}
      </div>
    </div>
  );

  const readOnlyFooter = (
    <div className="max-w-3xl mx-auto w-full flex flex-col gap-2">
      {peek.delegationError && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive"
          data-testid="subconversation-peek-error"
        >
          <AlertCircleIcon className="h-3.5 w-3.5 mt-0.5 flex-shrink-0" aria-hidden="true" />
          <span className="break-words">{peek.delegationError}</span>
        </div>
      )}
      <div
        className="flex items-center justify-between gap-2 text-xs text-muted-foreground"
        data-testid="subconversation-peek-status-bar"
      >
        <span className="inline-flex items-center gap-1.5">
          <LockIcon className="h-3.5 w-3.5" aria-hidden="true" />
          Read-only view
        </span>
        {peek.isStreaming ? (
          <span className="inline-flex items-center gap-1.5 text-primary font-medium">
            <RadioIcon className="h-3.5 w-3.5 animate-pulse" aria-hidden="true" />
            Streaming live
          </span>
        ) : peek.isListening ? (
          <span className="inline-flex items-center gap-1.5">
            <Loader2Icon className="h-3 w-3 animate-spin" aria-hidden="true" />
            Listening for updates
          </span>
        ) : statusInfo.tone === 'done' ? (
          <span className="inline-flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400">
            <CheckCircle2Icon className="h-3.5 w-3.5" aria-hidden="true" />
            Completed
          </span>
        ) : statusInfo.tone === 'failed' ? (
          <span className="inline-flex items-center gap-1.5 text-destructive">
            <AlertCircleIcon className="h-3.5 w-3.5" aria-hidden="true" />
            Failed
          </span>
        ) : null}
      </div>
    </div>
  );

  return (
    <Sheet
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) {
          onClose();
        }
      }}
    >
      <SheetContent
        side="right"
        className="w-full sm:max-w-2xl p-0 flex flex-col gap-0"
        data-testid="subconversation-peek-sheet"
      >
        <SheetHeader className="flex-shrink-0 border-b px-4 py-3 pr-12 text-left space-y-1">
          <div className="flex items-center gap-2 min-w-0">
            {delegationStack.length > 1 && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 px-2 -ml-1 flex-shrink-0"
                onClick={onPopDelegation}
                aria-label="Back to parent subconversation"
                data-testid="subconversation-peek-back"
              >
                <ArrowLeftIcon className="h-4 w-4" />
              </Button>
            )}
            <SheetTitle className="text-base truncate">{displayName}</SheetTitle>
            <Badge
              variant="secondary"
              className="inline-flex items-center gap-1 text-[11px] font-normal flex-shrink-0"
              data-testid="subconversation-peek-readonly-badge"
            >
              <LockIcon className="h-3 w-3" aria-hidden="true" />
              Read-only
            </Badge>
            <Badge
              variant={
                statusInfo.tone === 'failed'
                  ? 'destructive'
                  : statusInfo.tone === 'done'
                    ? 'secondary'
                    : 'outline'
              }
              className="inline-flex items-center gap-1 text-[11px] font-normal flex-shrink-0"
              data-testid="subconversation-peek-status-badge"
            >
              {statusInfo.tone === 'active' && (
                <Loader2Icon className="h-3 w-3 animate-spin" aria-hidden="true" />
              )}
              {statusInfo.tone === 'done' && (
                <CheckCircle2Icon className="h-3 w-3" aria-hidden="true" />
              )}
              {statusInfo.tone === 'failed' && (
                <AlertCircleIcon className="h-3 w-3" aria-hidden="true" />
              )}
              {statusInfo.label}
            </Badge>
          </div>
          <SheetDescription className="text-xs truncate">
            {startedLabel
              ? `${startedLabel}${activeDelegation?.request_preview ? ` · ${activeDelegation.request_preview}` : ''}`
              : (activeDelegation?.request_preview ?? 'Live read-only subconversation view')}
          </SheetDescription>
        </SheetHeader>

        <LiveBrowserView
          frame={peek.browserFrame}
          isRunning={statusInfo.tone === 'active'}
          isBrowserProfile={isBrowserProfile}
          compact
        />

        <div className="flex flex-1 flex-col min-h-0 overflow-hidden">
          {isOpen && (
            <AssistantRuntimeProvider runtime={runtime}>
              <ThreadErrorBoundary>
                <ChatControlsContext.Provider value={peekControls}>
                  <ToolConfirmationProvider value={confirmationContextValue}>
                    <Thread readOnly emptyState={emptyState} readOnlyFooter={readOnlyFooter} />
                  </ToolConfirmationProvider>
                </ChatControlsContext.Provider>
              </ThreadErrorBoundary>
            </AssistantRuntimeProvider>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
};
