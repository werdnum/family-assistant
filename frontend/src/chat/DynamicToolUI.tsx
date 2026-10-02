import React from 'react';
import { Attachment, isAttachment } from '../types/attachments';
import type { TerminalToolOutcome } from './toolOutcome';
import { ToolFallback, toolUIsByName } from './ToolUI';
import { ToolWithConfirmation } from './ToolWithConfirmation';

/**
 * Dynamic tool UI component that automatically wraps tools with confirmation UI
 * when a confirmation request is received via SSE.
 */
// Props interface matching @assistant-ui/react tool component props
interface AssistantUIToolProps {
  type: 'tool-call';
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
  argsText: string;
  result?: string | Record<string, unknown>;
  isError?: boolean;
  outcome?: TerminalToolOutcome;
  awaitingResult?: boolean;
  status: { type: string; reason?: string };
  addResult?: (result: unknown) => void;
  artifact?: Record<string, unknown>;
  attachments?: Array<Record<string, unknown>>;
}

export const DynamicToolUI: React.FC<AssistantUIToolProps> = (props) => {
  const {
    toolName,
    toolCallId,
    args,
    result,
    isError,
    outcome,
    awaitingResult,
    status,
    artifact,
    attachments: directAttachments,
  } = props;

  // Extract and validate attachments from artifact if present, otherwise use direct attachments prop
  const extractAttachments = (attachmentsData: unknown): Attachment[] => {
    if (!Array.isArray(attachmentsData)) {
      return [];
    }

    // Log invalid attachments for debugging
    attachmentsData.forEach((item) => {
      if (!isAttachment(item)) {
        console.warn('Invalid attachment structure:', item);
      }
    });

    return attachmentsData.filter(isAttachment);
  };

  const attachments = extractAttachments(artifact?.attachments || directAttachments);

  // Get the specific tool UI component or fallback
  const ToolComponent =
    (toolUIsByName as Record<string, React.ComponentType<unknown>>)[toolName] || ToolFallback;

  // Always wrap with ToolWithConfirmation which will conditionally show confirmation UI
  return (
    <ToolWithConfirmation
      toolName={toolName}
      toolCallId={toolCallId}
      args={args}
      result={result}
      status={status}
      outcome={outcome}
      isError={isError}
      awaitingResult={awaitingResult}
      attachments={attachments as unknown as Array<Record<string, unknown>>}
      // @ts-expect-error - ToolComponent type mismatch with dynamic lookup
      ToolComponent={ToolComponent}
    />
  );
};
