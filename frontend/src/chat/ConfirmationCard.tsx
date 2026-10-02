import { Check, Clock, Loader2, ShieldQuestion, X } from 'lucide-react';
import React, { useEffect, useId, useState } from 'react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { MarkdownText } from './MarkdownText';
import type { PendingToolConfirmation } from './ToolConfirmationContext';
import { humanizeToolName } from './toolSummary';

interface ConfirmationCardProps {
  confirmation: PendingToolConfirmation;
  onDecision: (approved: boolean) => Promise<void>;
  /** Shown in the header; omitted inline, where the tool call above already names it. */
  showToolName?: boolean;
  /** Where the request came from, shown under the header (tray only). */
  origin?: React.ReactNode;
  /** Caps the prompt lower so the actions stay in view inside the tray. */
  compact?: boolean;
}

function parseTimestamp(value: unknown): number | null {
  if (typeof value !== 'string' && typeof value !== 'number') {
    return null;
  }
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? null : timestamp;
}

function expiryTimestamp(confirmation: PendingToolConfirmation, fallbackAnchor: number) {
  const durationSeconds = confirmation.time_remaining_seconds ?? confirmation.timeout_seconds;
  if (typeof durationSeconds !== 'number') {
    return null;
  }
  const anchor =
    parseTimestamp(confirmation.received_at) ??
    parseTimestamp(confirmation.created_at) ??
    fallbackAnchor;
  return anchor + durationSeconds * 1000;
}

function secondsUntil(expiresAt: number | null): number | null {
  return expiresAt === null ? null : Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
}

/** Whole seconds left before the request expires, or null when it has no deadline. */
function useSecondsRemaining(confirmation: PendingToolConfirmation): number | null {
  const [fallbackAnchor] = useState(() => Date.now());
  const expiresAt = expiryTimestamp(confirmation, fallbackAnchor);
  const [secondsRemaining, setSecondsRemaining] = useState(() => secondsUntil(expiresAt));

  useEffect(() => {
    setSecondsRemaining(secondsUntil(expiresAt));
    if (expiresAt === null) {
      return;
    }
    const interval = window.setInterval(() => {
      const remaining = secondsUntil(expiresAt);
      setSecondsRemaining(remaining);
      if (remaining === 0) {
        window.clearInterval(interval);
      }
    }, 1000);
    return () => window.clearInterval(interval);
  }, [expiresAt]);

  return secondsRemaining;
}

export function formatTimeRemaining(seconds: number): string {
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatArgs(args: PendingToolConfirmation['args']): string | null {
  if (!args || Object.keys(args).length === 0) {
    return null;
  }
  return JSON.stringify(args, null, 2);
}

/**
 * Approve/reject card for one pending tool confirmation, shared by the inline
 * view under a tool call and the pending-approvals tray.
 */
export const ConfirmationCard: React.FC<ConfirmationCardProps> = ({
  confirmation,
  onDecision,
  showToolName = false,
  origin,
  compact = false,
}) => {
  const titleId = useId();
  const [pendingDecision, setPendingDecision] = useState<boolean | null>(null);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const secondsRemaining = useSecondsRemaining(confirmation);
  const expired = secondsRemaining === 0;
  const prompt = confirmation.confirmation_prompt?.trim();
  // The rendered prompt already lists what will run; the raw arguments are only
  // needed when there is no prompt to show.
  const argsText = prompt ? null : formatArgs(confirmation.args);

  const decide = async (approved: boolean) => {
    if (pendingDecision !== null || expired) {
      return;
    }
    setPendingDecision(approved);
    setDecisionError(null);
    try {
      await onDecision(approved);
    } catch (error) {
      console.error('Failed to resolve tool confirmation:', error);
      setDecisionError('Could not send this decision. Try again.');
      setPendingDecision(null);
    }
  };

  const busy = pendingDecision !== null;

  return (
    <div
      role="group"
      aria-labelledby={titleId}
      data-testid="confirmation-card"
      className="tool-confirmation-container shrink-0 overflow-hidden rounded-lg border bg-card text-card-foreground shadow-sm"
    >
      <div className="flex items-start gap-3 px-4 pt-3">
        <ShieldQuestion
          className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400"
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div id={titleId} className="text-sm font-medium">
            {showToolName
              ? `Approve ${humanizeToolName(confirmation.tool_name)}?`
              : 'Approval needed'}
          </div>
          {origin && <div className="mt-0.5 text-xs text-muted-foreground">{origin}</div>}
        </div>
        {secondsRemaining !== null && (
          <div
            className={cn(
              'flex shrink-0 items-center gap-1 text-xs tabular-nums',
              secondsRemaining > 0 && secondsRemaining < 30
                ? 'text-amber-700 dark:text-amber-400'
                : 'text-muted-foreground'
            )}
          >
            <Clock className="h-3 w-3" aria-hidden="true" />
            <span>
              {expired ? 'Expired' : `Expires in ${formatTimeRemaining(secondsRemaining)}`}
            </span>
          </div>
        )}
      </div>

      <div
        className={cn(
          'confirmation-prompt overflow-y-auto px-4 py-2 text-sm',
          compact ? 'max-h-48' : 'max-h-80'
        )}
      >
        {prompt ? (
          <MarkdownText text={prompt} />
        ) : (
          <p className="text-muted-foreground">
            {humanizeToolName(confirmation.tool_name)} is waiting for your approval.
          </p>
        )}
        {argsText && (
          <pre className="mt-2 whitespace-pre-wrap break-words rounded-md border bg-muted p-2 text-xs">
            {argsText}
          </pre>
        )}
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2 border-t bg-muted/40 px-4 py-2">
        {decisionError && (
          <div role="alert" className="mr-auto text-xs font-medium text-destructive">
            {decisionError}
          </div>
        )}
        {expired && !decisionError && (
          <div className="mr-auto text-xs text-muted-foreground">
            This request expired without a decision.
          </div>
        )}
        <Button
          onClick={() => void decide(false)}
          size="sm"
          variant="outline"
          className="hover:bg-destructive/10 hover:text-destructive"
          disabled={busy || expired}
        >
          {pendingDecision === false ? (
            <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <X className="mr-1 h-4 w-4" aria-hidden="true" />
          )}
          Reject
        </Button>
        <Button onClick={() => void decide(true)} size="sm" disabled={busy || expired}>
          {pendingDecision === true ? (
            <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <Check className="mr-1 h-4 w-4" aria-hidden="true" />
          )}
          Approve
        </Button>
      </div>
    </div>
  );
};
