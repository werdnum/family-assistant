import React, { useContext, useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { ToolConfirmationContext } from './ToolConfirmationContext';

interface ToolWithConfirmationProps {
  toolName: string;
  toolCallId?: string;
  args: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  status?: { type: string };
  attachments?: Array<Record<string, unknown>>;
  ToolComponent: React.ComponentType<{
    toolName: string;
    args: Record<string, unknown>;
    result?: string | Record<string, unknown>;
    status?: { type: string };
    attachments?: Array<Record<string, unknown>>;
  }>;
}

export const ToolWithConfirmation: React.FC<ToolWithConfirmationProps> = ({
  toolName,
  toolCallId,
  args,
  result,
  status,
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

  useEffect(() => {
    if (typeof pendingConfirmation?.timeout_seconds === 'number') {
      const createdAt = pendingConfirmation.created_at;
      // created_at is assigned when this client receives the SSE event, so the
      // countdown is not affected by server/client clock skew.
      const parsedStartedAt =
        typeof createdAt === 'string' || typeof createdAt === 'number'
          ? new Date(createdAt).getTime()
          : Number.NaN;
      const startedAt = Number.isNaN(parsedStartedAt) ? Date.now() : parsedStartedAt;
      const timeoutMs = pendingConfirmation.timeout_seconds * 1000;

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
      <div data-testid="tool-call">
        <ToolComponent
          toolName={toolName}
          args={args}
          result={result}
          status={status}
          attachments={attachments}
        />
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
