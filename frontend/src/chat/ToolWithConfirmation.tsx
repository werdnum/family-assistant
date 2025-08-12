import React, { useContext, useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { ToolConfirmationContext } from './ToolConfirmationContext';

interface ToolWithConfirmationProps {
  toolName: string;
  args: any;
  result?: any;
  status?: any;
  ToolComponent: React.ComponentType<any>;
}

export const ToolWithConfirmation: React.FC<ToolWithConfirmationProps> = ({
  toolName,
  args,
  result,
  status,
  ToolComponent,
}) => {
  const context = useContext(ToolConfirmationContext);
  const [timeRemaining, setTimeRemaining] = useState<number | null>(null);

  // Create a key to match the confirmation request
  const confirmationKey = `${toolName}:${JSON.stringify(args)}`;
  const pendingConfirmation = context?.pendingConfirmations?.get(confirmationKey);

  useEffect(() => {
    if (pendingConfirmation?.timeout_seconds) {
      const createdAt = new Date(pendingConfirmation.created_at);
      const expiresAt = new Date(createdAt.getTime() + pendingConfirmation.timeout_seconds * 1000);

      const interval = setInterval(() => {
        const now = new Date();
        const remaining = Math.max(0, Math.floor((expiresAt.getTime() - now.getTime()) / 1000));
        setTimeRemaining(remaining);

        if (remaining <= 0) {
          clearInterval(interval);
        }
      }, 1000);

      return () => clearInterval(interval);
    }
  }, [pendingConfirmation]);

  const handleApprove = async () => {
    if (context?.handleConfirmation && pendingConfirmation) {
      await context.handleConfirmation(
        '', // toolCallId not needed since we match by request_id
        pendingConfirmation.request_id,
        true
      );
    }
  };

  const handleReject = async () => {
    if (context?.handleConfirmation && pendingConfirmation) {
      await context.handleConfirmation(
        '', // toolCallId not needed since we match by request_id
        pendingConfirmation.request_id,
        false
      );
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
