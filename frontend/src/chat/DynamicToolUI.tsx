import React from 'react';
import { toolUIsByName, ToolFallback } from './ToolUI';
import { ToolWithConfirmation } from './ToolWithConfirmation';
import { Attachment, isAttachment } from '../types/attachments';

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
  status: { type: string };
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
    status,
    artifact,
    attachments: directAttachments,
  } = props;

  // Extract and validate attachments from artifact if present, otherwise use direct attachments prop
  const extractAttachments = (attachmentsData: unknown): Attachment[] => {
    if (!Array.isArray(attachmentsData)) {
      return [];
    }

    const validAttachments: Attachment[] = [];
    for (const item of attachmentsData) {
      if (isAttachment(item)) {
        validAttachments.push(item);
      } else {
        // Log invalid attachments for debugging
        console.warn('Invalid attachment structure:', item);
      }
    }
    return validAttachments;
  };

  const attachments = extractAttachments(artifact?.attachments || directAttachments);

  // Get the specific tool UI component or fallback
  const ToolComponent = toolUIsByName[toolName] || ToolFallback;

  // Always wrap with ToolWithConfirmation which will conditionally show confirmation UI
  return (
    <ToolWithConfirmation
      toolName={toolName}
      toolCallId={toolCallId}
      args={args}
      result={result}
      status={status}
      attachments={attachments}
      ToolComponent={ToolComponent}
    />
  );
};
