import { useMessage } from '@assistant-ui/react';
import React, { useContext, useEffect, useMemo, useRef, useState } from 'react';
import { ToolConfirmationContext } from './ToolConfirmationContext';
import { ToolGroupShell } from './ToolGroupShell';

interface ToolGroupProps {
  startIndex: number;
  endIndex: number;
  children: React.ReactNode;
}

interface ToolGroupState {
  toolNames: string[];
  toolCallIds: string[];
  hasUnfinishedTool: boolean;
}

interface MessagePartLike {
  type: string;
  toolName?: string;
  toolCallId?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  artifact?: unknown;
  attachments?: unknown[];
  status?: {
    type?: string;
  };
}

const DEFAULT_TOOL_GROUP_STATE: ToolGroupState = {
  toolNames: [],
  toolCallIds: [],
  hasUnfinishedTool: false,
};

function isTerminalToolPart(part: MessagePartLike): boolean {
  return (
    part.status?.type === 'complete' ||
    part.result !== undefined ||
    part.artifact !== undefined ||
    part.attachments !== undefined ||
    (part.toolName === 'attach_to_response' && Array.isArray(part.args?.attachment_ids))
  );
}

function getToolGroupState(
  parts: readonly MessagePartLike[],
  startIndex: number,
  endIndex: number
): ToolGroupState {
  const toolNames: string[] = [];
  const toolCallIds: string[] = [];
  let hasUnfinishedTool = false;

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
      }
    }
  }

  return { toolNames, toolCallIds, hasUnfinishedTool };
}

// Hook to safely access message state with fallback
function useSafeToolGroupState(startIndex: number, endIndex: number): ToolGroupState {
  const serializedState = useMessage<string>({
    optional: true,
    selector: (message) =>
      JSON.stringify(
        getToolGroupState(message.content as readonly MessagePartLike[], startIndex, endIndex)
      ),
  });

  return useMemo(
    () =>
      serializedState ? (JSON.parse(serializedState) as ToolGroupState) : DEFAULT_TOOL_GROUP_STATE,
    [serializedState]
  );
}

const ToolGroup: React.FC<ToolGroupProps> = ({ startIndex, endIndex, children }) => {
  const context = useContext(ToolConfirmationContext);

  const { toolNames, toolCallIds, hasUnfinishedTool } = useSafeToolGroupState(startIndex, endIndex);
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
      isExpanded={isExpanded}
      onOpenChange={handleOpenChange}
    >
      {children}
    </ToolGroupShell>
  );
};

export { ToolGroup };
