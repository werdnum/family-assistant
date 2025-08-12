import React, { useState, useEffect, useCallback } from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { AlertCircleIcon, CheckCircleIcon, ClockIcon, XCircleIcon } from 'lucide-react';

interface ToolConfirmationRequest {
  request_id: string;
  tool_name: string;
  confirmation_prompt: string;
  timeout_seconds: number;
  args: Record<string, any>;
  timestamp: number;
}

interface ConfirmationDialogProps {
  confirmationRequest: ToolConfirmationRequest | null;
  onConfirm: (requestId: string, approved: boolean) => Promise<void>;
  onTimeout: (requestId: string) => void;
}

export const ConfirmationDialog: React.FC<ConfirmationDialogProps> = ({
  confirmationRequest,
  onConfirm,
  onTimeout,
}) => {
  const [isProcessing, setIsProcessing] = useState(false);
  const [timeRemaining, setTimeRemaining] = useState<number | null>(null);

  // Handle timeout countdown
  useEffect(() => {
    if (!confirmationRequest) {
      setTimeRemaining(null);
      return;
    }

    const startTime = confirmationRequest.timestamp;
    const timeoutMs = confirmationRequest.timeout_seconds * 1000;

    const updateTimer = () => {
      const elapsed = Date.now() - startTime;
      const remaining = Math.max(0, timeoutMs - elapsed);
      setTimeRemaining(Math.ceil(remaining / 1000));

      if (remaining <= 0) {
        onTimeout(confirmationRequest.request_id);
      }
    };

    // Update immediately and then every second
    updateTimer();
    const interval = setInterval(updateTimer, 1000);

    return () => clearInterval(interval);
  }, [confirmationRequest, onTimeout]);

  const handleConfirm = useCallback(
    async (approved: boolean) => {
      if (!confirmationRequest || isProcessing) {
        return;
      }

      setIsProcessing(true);
      try {
        await onConfirm(confirmationRequest.request_id, approved);
      } finally {
        setIsProcessing(false);
      }
    },
    [confirmationRequest, isProcessing, onConfirm]
  );

  const formatToolName = (name: string) => {
    // Convert snake_case to Title Case
    return name
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  };

  const formatArguments = (args: Record<string, any>) => {
    // Format tool arguments for display
    const entries = Object.entries(args);
    if (entries.length === 0) {
      return null;
    }

    return (
      <div className="mt-3 space-y-1 text-sm">
        <div className="font-medium text-muted-foreground">Parameters:</div>
        <div className="rounded-md bg-muted/50 p-3 font-mono text-xs">
          {entries.map(([key, value]) => (
            <div key={key} className="mb-1 last:mb-0">
              <span className="text-primary">{key}:</span>{' '}
              <span className="text-foreground">
                {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  };

  const getTimeoutBadge = () => {
    if (timeRemaining === null) {
      return null;
    }

    const urgencyClass =
      timeRemaining <= 10
        ? 'bg-destructive text-destructive-foreground'
        : timeRemaining <= 30
          ? 'bg-warning text-warning-foreground'
          : '';

    return (
      <Badge variant="outline" className={`gap-1 ${urgencyClass}`}>
        <ClockIcon size={12} />
        {timeRemaining}s
      </Badge>
    );
  };

  if (!confirmationRequest) {
    return null;
  }

  return (
    <AlertDialog open={true}>
      <AlertDialogContent className="max-w-lg">
        <AlertDialogHeader>
          <div className="flex items-center justify-between">
            <AlertDialogTitle className="flex items-center gap-2">
              <AlertCircleIcon className="h-5 w-5 text-warning" />
              Tool Confirmation Required
            </AlertDialogTitle>
            {getTimeoutBadge()}
          </div>
          <AlertDialogDescription className="space-y-3">
            <div>
              The assistant wants to execute:{' '}
              <span className="font-semibold text-foreground">
                {formatToolName(confirmationRequest.tool_name)}
              </span>
            </div>

            {/* Render the confirmation prompt (may contain markdown) */}
            <div className="rounded-md border border-border bg-card p-3 text-card-foreground">
              <div
                className="prose prose-sm dark:prose-invert max-w-none"
                dangerouslySetInnerHTML={{
                  __html: confirmationRequest.confirmation_prompt
                    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                    .replace(/\*(.*?)\*/g, '<em>$1</em>')
                    .replace(/`(.*?)`/g, '<code>$1</code>')
                    .replace(/\n/g, '<br />'),
                }}
              />
            </div>

            {/* Show tool arguments if needed */}
            {formatArguments(confirmationRequest.args)}

            <div className="text-sm text-muted-foreground">Do you want to allow this action?</div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel
            onClick={() => handleConfirm(false)}
            disabled={isProcessing}
            className="gap-2"
          >
            <XCircleIcon size={16} />
            Reject
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={() => handleConfirm(true)}
            disabled={isProcessing}
            className="gap-2"
          >
            <CheckCircleIcon size={16} />
            Approve
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
};
