import React from 'react';
import { toolUIsByName } from '../../chat/ToolUI';
import ToolParameterViewer from './ToolParameterViewer';

/**
 * Shared component for displaying tool calls with their arguments.
 * Routes to tool-specific UIs when available, falls back to ToolParameterViewer.
 *
 * This is used in both chat UI and message history for consistent tool display.
 */
const ToolDisplay = ({ toolName, args, result, status, attachments }) => {
  // Get the specific tool UI component if registered
  const ToolComponent = toolUIsByName[toolName];

  if (ToolComponent) {
    // Use the tool-specific UI
    return (
      <ToolComponent
        toolName={toolName}
        args={args}
        result={result}
        status={status}
        attachments={attachments}
      />
    );
  }

  // Fall back to ToolParameterViewer for unregistered tools
  return (
    <div>
      <div style={{ marginBottom: '0.5rem', fontWeight: 'bold' }}>{toolName}</div>
      {args && Object.keys(args).length > 0 && (
        <ToolParameterViewer data={args} toolName={toolName} />
      )}
      {result && (
        <div style={{ marginTop: '0.5rem' }}>
          <div style={{ fontWeight: 'bold', marginBottom: '0.25rem' }}>Result:</div>
          {typeof result === 'string' ? (
            <div>{result}</div>
          ) : (
            <ToolParameterViewer data={result} toolName={`${toolName} result`} />
          )}
        </div>
      )}
    </div>
  );
};

export default ToolDisplay;
