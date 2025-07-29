import React from 'react';
import { makeAssistantToolUI } from '@assistant-ui/react';
import { CheckCircleIcon, ClockIcon, AlertCircleIcon } from 'lucide-react';

// Generic fallback tool UI that handles any tool call
const ToolFallback = ({ toolName, args, result, status }) => {
  // Determine the icon and styling based on status
  let statusIcon = null;
  let statusClass = '';
  
  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete' && result) {
    statusIcon = <CheckCircleIcon size={16} />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div className={`tool-call-container ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">{toolName}</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>
      
      {args && Object.keys(args).length > 0 && (
        <div className="tool-call-args">
          <div className="tool-section-label">Arguments:</div>
          <pre className="tool-code-block">{JSON.stringify(args, null, 2)}</pre>
        </div>
      )}
      
      {result && (
        <div className="tool-call-result">
          <div className="tool-section-label">Result:</div>
          {typeof result === 'string' ? (
            <div className="tool-result-text">{result}</div>
          ) : (
            <pre className="tool-code-block">{JSON.stringify(result, null, 2)}</pre>
          )}
        </div>
      )}
      
      {status?.type === 'running' && (
        <div className="tool-running-message">Executing tool...</div>
      )}
    </div>
  );
};

// Specific tool UI for add_or_update_note
export const AddOrUpdateNoteToolUI = makeAssistantToolUI({
  toolName: 'add_or_update_note',
  render: ({ args, result, status }) => {
    return (
      <div className="tool-call-container tool-note" data-ui="tool-call-content">
        <div className="tool-call-header">
          <span className="tool-name">📝 Note</span>
          {status?.type === 'complete' && <CheckCircleIcon size={16} className="tool-success" />}
        </div>
        
        <div className="tool-note-content">
          {args?.title && <h4 className="tool-note-title">{args.title}</h4>}
          {args?.content && <p className="tool-note-text">{args.content}</p>}
        </div>
        
        {result && (
          <div className="tool-note-result">
            {typeof result === 'string' ? result : 'Note saved successfully!'}
          </div>
        )}
      </div>
    );
  },
});

// Tool UI for search_documents
export const SearchDocumentsToolUI = makeAssistantToolUI({
  toolName: 'search_documents',
  render: ({ args, result, status }) => {
    return (
      <div className="tool-call-container tool-search" data-ui="tool-call-content">
        <div className="tool-call-header">
          <span className="tool-name">🔍 Search Documents</span>
          {status?.type === 'running' && <ClockIcon size={16} className="animate-spin" />}
        </div>
        
        {args?.query && (
          <div className="tool-search-query">
            Searching for: <strong>{args.query}</strong>
          </div>
        )}
        
        {result && (
          <div className="tool-search-results">
            {Array.isArray(result) ? (
              <div>Found {result.length} results</div>
            ) : (
              <div>{typeof result === 'string' ? result : JSON.stringify(result)}</div>
            )}
          </div>
        )}
      </div>
    );
  },
});

// Create a map of tool UIs by name for easier access
export const toolUIsByName = {
  'add_or_update_note': AddOrUpdateNoteToolUI,
  'search_documents': SearchDocumentsToolUI,
};

// Export all tool UIs as an array
export const toolUIs = [
  AddOrUpdateNoteToolUI,
  SearchDocumentsToolUI,
];

// Export ToolFallback separately
export { ToolFallback };
