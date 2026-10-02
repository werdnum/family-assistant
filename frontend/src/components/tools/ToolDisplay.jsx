import React from 'react';
import { ToolFallback, toolUIsByName } from '../../chat/ToolUI';

/**
 * Shared component for displaying tool calls with their arguments.
 * Routes to tool-specific UIs when available, falls back to the chat's
 * compact ToolFallback so history and chat render unknown tools the same way.
 */
const ToolDisplay = ({ toolName, args, result, status, attachments }) => {
  const ToolComponent = toolUIsByName[toolName] || ToolFallback;

  return (
    <ToolComponent
      toolName={toolName}
      args={args}
      result={result}
      status={status}
      attachments={attachments}
    />
  );
};

export default ToolDisplay;
