import { Check, X } from 'lucide-react';
import React, { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import type { PendingToolConfirmation } from './ToolConfirmationContext';

interface PendingConfirmationsTrayProps {
  confirmations: PendingToolConfirmation[];
  loadError?: string | null;
  onConfirm: (requestId: string, approved: boolean) => Promise<void>;
}

function formatToolArgs(args: PendingToolConfirmation['args']): string {
  if (!args || Object.keys(args).length === 0) {
    return '';
  }
  return JSON.stringify(args, null, 2);
}

function secondsUntilExpiry(confirmation: PendingToolConfirmation): number | null {
  const durationSeconds = confirmation.time_remaining_seconds ?? confirmation.timeout_seconds;
  if (typeof durationSeconds !== 'number') {
    return null;
  }

  const receivedAtValue = confirmation.received_at ?? confirmation.created_at;
  if (receivedAtValue === undefined) {
    return null;
  }
  const receivedAt = new Date(receivedAtValue).getTime();
  if (Number.isNaN(receivedAt)) {
    return null;
  }

  const expiresAt = receivedAt + durationSeconds * 1000;
  return Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
}

export const PendingConfirmationsTray: React.FC<PendingConfirmationsTrayProps> = ({
  confirmations,
  loadError,
  onConfirm,
}) => {
  const [resolvingRequestIds, setResolvingRequestIds] = useState<Set<string>>(new Set());
  const [decisionErrors, setDecisionErrors] = useState<Map<string, string>>(new Map());
  const [, setTick] = useState(0);

  useEffect(() => {
    if (confirmations.length === 0) {
      return;
    }

    const interval = window.setInterval(() => setTick((value) => value + 1), 1000);
    return () => window.clearInterval(interval);
  }, [confirmations.length]);

  const handleDecision = async (requestId: string, approved: boolean) => {
    if (resolvingRequestIds.has(requestId)) {
      return;
    }
    setResolvingRequestIds((prev) => new Set(prev).add(requestId));
    setDecisionErrors((prev) => {
      const next = new Map(prev);
      next.delete(requestId);
      return next;
    });
    try {
      await onConfirm(requestId, approved);
    } catch (error) {
      console.error('Failed to resolve pending confirmation:', error);
      setDecisionErrors((prev) => {
        const next = new Map(prev);
        next.set(requestId, 'Could not send this decision. Try again.');
        return next;
      });
    } finally {
      setResolvingRequestIds((prev) => {
        const next = new Set(prev);
        next.delete(requestId);
        return next;
      });
    }
  };

  if (confirmations.length === 0 && !loadError) {
    return null;
  }

  return (
    <section
      className="border-b bg-amber-50 px-4 py-3"
      aria-label="Pending tool confirmations"
      data-testid="pending-confirmations-tray"
    >
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-3">
        <h3 className="text-sm font-semibold text-amber-950">Pending approvals</h3>
        {loadError && (
          <div
            role="alert"
            className="rounded-md border border-red-200 bg-red-50 p-3 text-sm font-medium text-red-700"
          >
            {loadError}
          </div>
        )}
        <div className="flex flex-col gap-2">
          {confirmations.map((confirmation) => {
            const argsText = formatToolArgs(confirmation.args);
            const timeRemaining = secondsUntilExpiry(confirmation);
            const isResolving = resolvingRequestIds.has(confirmation.request_id);
            const decisionError = decisionErrors.get(confirmation.request_id);

            return (
              <div
                key={confirmation.request_id}
                className="rounded-md border border-amber-200 bg-white p-3 shadow-sm"
              >
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-slate-950">
                      {confirmation.tool_name ?? 'Tool action'}
                    </div>
                    {confirmation.confirmation_prompt && (
                      <div className="mt-1 whitespace-pre-wrap text-sm text-slate-700">
                        {confirmation.confirmation_prompt}
                      </div>
                    )}
                    {argsText && (
                      <pre className="mt-2 max-h-36 overflow-auto rounded border bg-slate-50 p-2 text-xs text-slate-700">
                        {argsText}
                      </pre>
                    )}
                    {timeRemaining !== null && (
                      <div className="mt-2 text-xs text-slate-500">
                        {timeRemaining > 0 ? `Expires in ${timeRemaining}s` : 'Expired'}
                      </div>
                    )}
                    {decisionError && (
                      <div className="mt-2 text-xs font-medium text-red-600">{decisionError}</div>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button
                      onClick={() => void handleDecision(confirmation.request_id, true)}
                      size="sm"
                      className="bg-green-600 text-white hover:bg-green-700"
                      disabled={isResolving}
                      aria-label={`Approve ${confirmation.tool_name ?? 'tool action'}`}
                    >
                      <Check className="mr-1 h-4 w-4" />
                      Approve
                    </Button>
                    <Button
                      onClick={() => void handleDecision(confirmation.request_id, false)}
                      size="sm"
                      variant="outline"
                      className="text-red-600"
                      disabled={isResolving}
                      aria-label={`Reject ${confirmation.tool_name ?? 'tool action'}`}
                    >
                      <X className="mr-1 h-4 w-4" />
                      Reject
                    </Button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
};
