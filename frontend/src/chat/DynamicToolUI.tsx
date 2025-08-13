import React from 'react';
import { toolUIsByName, ToolFallback } from './ToolUI';
import { ToolWithConfirmation } from './ToolWithConfirmation';

/**
 * Dynamic tool UI component that automatically wraps tools with confirmation UI
 * when a confirmation request is received via SSE.
 */
export const DynamicToolUI: React.FC<{
  toolName: string;
  toolCallId?: string;
  args: any;
  result?: any;
  status?: any;
}> = (props) => {
  const { toolName } = props;

  // Get the specific tool UI component or fallback
  const ToolComponent = toolUIsByName[toolName] || ToolFallback;

  // Always wrap with ToolWithConfirmation which will conditionally show confirmation UI
  return <ToolWithConfirmation {...props} ToolComponent={ToolComponent} />;
};
