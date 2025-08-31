import React, { useContext, useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { ToolConfirmationContext } from './ToolConfirmationContext';

interface ToolWithConfirmationProps {
  toolName: string;
  toolCallId?: string;
  args: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  status?: { type: string };
  ToolComponent: React.ComponentType<{
    toolName: string;
    args: Record<string, unknown>;
    result?: string | Record<string, unknown>;
    status?: { type: string };
  }>;
}

export const ToolWithConfirmation: React.FC<ToolWithConfirmationProps> = ({
  toolName,
  toolCallId,
  args,
  result,
  status,
  ToolComponent,
}) => {
  const context = useContext(ToolConfirmationContext);
  const [timeRemaining, setTimeRemaining] = useState<number | null>(null);

  // Get the confirmation by tool_call_id
  const pendingConfirmation = toolCallId
    ? context?.pendingConfirmations?.get(toolCallId)
    : undefined;

  useEffect(() => {
    if (pendingConfirmation?.timeout_seconds && pendingConfirmation.created_at) {
      const createdAt = new Date(pendingConfirmation.created_at);
      const expiresAt = new Date(createdAt.getTime() + pendingConfirmation.timeout_seconds * 1000);

      // Calculate initial time remaining immediately
      const calculateTimeRemaining = () => {
        const now = new Date();
        return Math.max(0, Math.floor((expiresAt.getTime() - now.getTime()) / 1000));
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

  const handleApprove = async () => {
    if (context?.handleConfirmation && pendingConfirmation && toolCallId) {
      await context.handleConfirmation(toolCallId, pendingConfirmation.request_id, true);
    }
  };

  const handleReject = async () => {
    if (context?.handleConfirmation && pendingConfirmation && toolCallId) {
      await context.handleConfirmation(toolCallId, pendingConfirmation.request_id, false);
    }
  };

  return (
    <>
      {/* Always render the tool UI */}
      <ToolComponent toolName={toolName} args={args} result={result} status={status} />

      {/* Show confirmation UI if there's a pending confirmation */}
      {pendingConfirmation && (
        <div className="tool-confirmation-container mt-4 p-4 bg-amber-50 border border-amber-200 rounded-lg">
          <div className="prose prose-sm max-w-none mb-4">
            <strong>Confirmation Required:</strong>
            <div dangerouslySetInnerHTML={{ __html: pendingConfirmation.confirmation_prompt }} />
          </div>
          <div className="flex gap-2 items-center">
            <Button
              onClick={handleApprove}
              size="sm"
              className="bg-green-600 hover:bg-green-700 text-white"
            >
              Approve
            </Button>
            <Button onClick={handleReject} size="sm" variant="outline" className="text-red-600">
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
