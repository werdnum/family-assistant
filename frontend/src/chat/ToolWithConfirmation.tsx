import React, { useContext, useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { ToolConfirmationContext } from './ToolConfirmationContext';
import {
  rendererStatusForOutcome,
  resolveToolOutcome,
  TOOL_OUTCOME_LABELS,
  type TerminalToolOutcome,
  type ToolOutcome,
  type ToolPartStatus,
} from './toolOutcome';

interface ToolWithConfirmationProps {
  toolName: string;
  toolCallId?: string;
  args: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  status?: ToolPartStatus;
  outcome?: TerminalToolOutcome;
  isError?: boolean;
  awaitingResult?: boolean;
  attachments?: Array<Record<string, unknown>>;
  ToolComponent: React.ComponentType<{
    toolName: string;
    args: Record<string, unknown>;
    result?: string | Record<string, unknown>;
    status?: ToolPartStatus;
    outcome?: ToolOutcome;
    attachments?: Array<Record<string, unknown>>;
  }>;
}

// Said in words for the outcomes a renderer's icon may not distinguish, so a
// call that did not succeed never reads as done.
const OUTCOME_NOTES: Partial<Record<ToolOutcome, string>> = {
  failed: TOOL_OUTCOME_LABELS.failed,
  rejected: TOOL_OUTCOME_LABELS.rejected,
  unknown: `${TOOL_OUTCOME_LABELS.unknown}: the turn ended before this call finished`,
};

export const ToolWithConfirmation: React.FC<ToolWithConfirmationProps> = ({
  toolName,
  toolCallId,
  args,
  result,
  status,
  outcome: reportedOutcome,
  isError,
  awaitingResult,
  attachments,
  ToolComponent,
}) => {
  const context = useContext(ToolConfirmationContext);
  const [timeRemaining, setTimeRemaining] = useState<number | null>(null);
  const [isResolving, setIsResolving] = useState(false);

  // Get the confirmation by tool_call_id
  const pendingConfirmation = toolCallId
    ? context?.pendingConfirmations?.get(toolCallId)
    : undefined;

  const outcome = resolveToolOutcome({
    status,
    result,
    outcome: reportedOutcome,
    isError,
    awaitingResult,
    awaitingApproval: Boolean(pendingConfirmation),
  });
  const outcomeNote = OUTCOME_NOTES[outcome];

  useEffect(() => {
    const durationSeconds =
      pendingConfirmation?.time_remaining_seconds ?? pendingConfirmation?.timeout_seconds;
    if (typeof durationSeconds === 'number') {
      const anchorTimestamp = pendingConfirmation?.received_at ?? pendingConfirmation?.created_at;
      const parsedStartedAt =
        typeof anchorTimestamp === 'string' || typeof anchorTimestamp === 'number'
          ? new Date(anchorTimestamp).getTime()
          : Number.NaN;
      const startedAt = Number.isNaN(parsedStartedAt) ? Date.now() : parsedStartedAt;
      const timeoutMs = durationSeconds * 1000;

      const calculateTimeRemaining = () => {
        const elapsedMs = Date.now() - startedAt;
        return Math.max(0, Math.floor((timeoutMs - elapsedMs) / 1000));
      };

      // Set initial value immediately
      const initialRemaining = calculateTimeRemaining();
      setTimeRemaining(initialRemaining);

      if (initialRemaining > 0) {
        const interval = setInterval(() => {
          const remaining = calculateTimeRemaining();
          setTimeRemaining(remaining);

          if (remaining <= 0) {
            clearInterval(interval);
          }
        }, 1000);

        return () => clearInterval(interval);
      }
    } else {
      // No timeout specified, clear any existing timeout display
      setTimeRemaining(null);
    }
  }, [pendingConfirmation]);

  useEffect(() => {
    setIsResolving(false);
  }, [pendingConfirmation?.request_id]);

  const handleApprove = async () => {
    if (context?.handleConfirmation && pendingConfirmation && toolCallId && !isResolving) {
      setIsResolving(true);
      try {
        await context.handleConfirmation(toolCallId, pendingConfirmation.request_id, true);
      } catch (error) {
        console.error('Failed to approve tool confirmation:', error);
        setIsResolving(false);
      }
    }
  };

  const handleReject = async () => {
    if (context?.handleConfirmation && pendingConfirmation && toolCallId && !isResolving) {
      setIsResolving(true);
      try {
        await context.handleConfirmation(toolCallId, pendingConfirmation.request_id, false);
      } catch (error) {
        console.error('Failed to reject tool confirmation:', error);
        setIsResolving(false);
      }
    }
  };

  return (
    <>
      {/* Always render the tool UI */}
      <div data-testid="tool-call" data-tool-outcome={outcome}>
        <ToolComponent
          toolName={toolName}
          args={args}
          result={result}
          status={rendererStatusForOutcome(outcome)}
          outcome={outcome}
          attachments={attachments}
        />
        {outcomeNote && (
          <div
            className={`tool-outcome-note mt-1 text-xs ${
              outcome === 'failed' ? 'text-destructive' : 'text-muted-foreground'
            }`}
            data-testid="tool-outcome-note"
          >
            {outcomeNote}
          </div>
        )}
      </div>

      {/* Show confirmation UI if there's a pending confirmation */}
      {pendingConfirmation && (
        <div className="tool-confirmation-container mt-4 p-4 bg-amber-50 border border-amber-200 rounded-lg">
          <div className="prose prose-sm max-w-none mb-4">
            <strong>Confirmation Required:</strong>
            <div className="whitespace-pre-wrap">
              {String(pendingConfirmation.confirmation_prompt ?? '')}
            </div>
          </div>
          <div className="flex gap-2 items-center">
            <Button
              onClick={handleApprove}
              size="sm"
              className="bg-green-600 hover:bg-green-700 text-white"
              disabled={isResolving}
            >
              Approve
            </Button>
            <Button
              onClick={handleReject}
              size="sm"
              variant="outline"
              className="text-red-600"
              disabled={isResolving}
            >
              Reject
            </Button>
            {timeRemaining !== null && (
              <span className="text-sm text-gray-500 ml-auto">
                {timeRemaining > 0 ? `Expires in ${timeRemaining}s` : 'Expired'}
              </span>
            )}
          </div>
        </div>
      )}
    </>
  );
};
