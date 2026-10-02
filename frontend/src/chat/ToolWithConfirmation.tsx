import React, { useContext } from 'react';
import { ConfirmationCard } from './ConfirmationCard';
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

      {pendingConfirmation && toolCallId && context && (
        <div className="mt-3">
          <ConfirmationCard
            key={pendingConfirmation.request_id}
            confirmation={pendingConfirmation}
            onDecision={(approved) =>
              context.handleConfirmation(toolCallId, pendingConfirmation.request_id, approved)
            }
          />
        </div>
      )}
    </>
  );
};
