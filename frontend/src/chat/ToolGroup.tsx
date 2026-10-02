import { useAuiState } from '@assistant-ui/react';
import React, { useContext, useEffect, useMemo, useRef, useState } from 'react';
import { ToolConfirmationContext } from './ToolConfirmationContext';
import { ToolGroupShell } from './ToolGroupShell';
import { isTerminalToolOutcome, resolveToolOutcome } from './toolOutcome';

interface ToolGroupProps {
  startIndex: number;
  endIndex: number;
  children: React.ReactNode;
}

interface ToolGroupState {
  toolNames: string[];
  toolCallIds: string[];
  hasUnfinishedTool: boolean;
  /** Calls that ended without succeeding: failed, not run, or no result. */
  unsuccessfulCount: number;
}

interface MessagePartLike {
  type: string;
  toolName?: string;
  toolCallId?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  artifact?: unknown;
  attachments?: unknown[];
  outcome?: unknown;
  isError?: boolean;
  awaitingResult?: boolean;
  status?: {
    type?: string;
    reason?: string;
  };
}

const DEFAULT_TOOL_GROUP_STATE: ToolGroupState = {
  toolNames: [],
  toolCallIds: [],
  hasUnfinishedTool: false,
  unsuccessfulCount: 0,
};

// `status` is the runtime's part status (from `message.parts`), which it derives
// from the message status for a tool call without a result: so a historical
// call whose result was never recorded is complete, not running.
function isTerminalToolPart(part: MessagePartLike): boolean {
  return (
    (part.status?.type !== 'running' && part.status?.type !== 'requires-action') ||
    part.result !== undefined ||
    part.artifact !== undefined ||
    part.attachments !== undefined ||
    (part.toolName === 'attach_to_response' && Array.isArray(part.args?.attachment_ids))
  );
}

// A finished group collapses, so a call that did not succeed is counted in the
// group's header rather than left for the user to find by opening it.
function isUnsuccessfulToolPart(part: MessagePartLike): boolean {
  const outcome = resolveToolOutcome({
    status: part.status?.type ? { type: part.status.type, reason: part.status.reason } : undefined,
    result: part.result,
    outcome: isTerminalToolOutcome(part.outcome) ? part.outcome : undefined,
    isError: part.isError,
    awaitingResult: part.awaitingResult,
  });
  return outcome === 'failed' || outcome === 'rejected' || outcome === 'unknown';
}

function getToolGroupState(
  parts: readonly MessagePartLike[],
  startIndex: number,
  endIndex: number
): ToolGroupState {
  const toolNames: string[] = [];
  const toolCallIds: string[] = [];
  let hasUnfinishedTool = false;
  let unsuccessfulCount = 0;

  for (let i = startIndex; i <= endIndex && i < parts.length; i++) {
    const part = parts[i];
    if (part.type === 'tool-call') {
      if (part.toolName) {
        toolNames.push(part.toolName);
      }
      if (part.toolCallId) {
        toolCallIds.push(part.toolCallId);
      }

      if (!isTerminalToolPart(part)) {
        hasUnfinishedTool = true;
      } else if (isUnsuccessfulToolPart(part)) {
        unsuccessfulCount += 1;
      }
    }
  }

  return { toolNames, toolCallIds, hasUnfinishedTool, unsuccessfulCount };
}

// Hook to safely access message state with fallback
function useSafeToolGroupState(startIndex: number, endIndex: number): ToolGroupState {
  const serializedState = useAuiState((s) => {
    const message = s.optional.message;
    if (!message) {
      return undefined;
    }
    return JSON.stringify(
      getToolGroupState(message.parts as readonly MessagePartLike[], startIndex, endIndex)
    );
  });

  return useMemo(
    () =>
      serializedState ? (JSON.parse(serializedState) as ToolGroupState) : DEFAULT_TOOL_GROUP_STATE,
    [serializedState]
  );
}

const ToolGroup: React.FC<ToolGroupProps> = ({ startIndex, endIndex, children }) => {
  const context = useContext(ToolConfirmationContext);

  const { toolNames, toolCallIds, hasUnfinishedTool, unsuccessfulCount } = useSafeToolGroupState(
    startIndex,
    endIndex
  );
  const hasPendingConfirmation = toolCallIds.some((toolCallId) =>
    context?.pendingConfirmations?.has(toolCallId)
  );
  const shouldAutoExpand = hasUnfinishedTool || hasPendingConfirmation;
  const [isExpanded, setIsExpanded] = useState(shouldAutoExpand);
  const hasUserToggled = useRef(false);

  useEffect(() => {
    if (!hasUserToggled.current) {
      setIsExpanded(shouldAutoExpand);
    }
  }, [shouldAutoExpand]);

  const handleOpenChange = (open: boolean) => {
    hasUserToggled.current = true;
    setIsExpanded(open);
  };

  return (
    <ToolGroupShell
      toolNames={toolNames}
      toolCount={endIndex - startIndex + 1}
      unsuccessfulCount={unsuccessfulCount}
      isExpanded={isExpanded}
      onOpenChange={handleOpenChange}
    >
      {children}
    </ToolGroupShell>
  );
};

export { ToolGroup };
