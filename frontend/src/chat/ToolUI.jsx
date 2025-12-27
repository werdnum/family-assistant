import { AlertCircleIcon, CheckCircleIcon, ClockIcon, DownloadIcon } from 'lucide-react';
import React, { lazy, Suspense } from 'react';
import ToolParameterViewer from '@/components/tools/ToolParameterViewer';
import { Button } from '@/components/ui/button';
import { getAttachmentKey } from '../types/attachments';
import { AttachToResponseTool } from './AttachToResponseTool';

// Lazy load syntax highlighter to reduce initial bundle size
const LazyPrism = lazy(() =>
  import('react-syntax-highlighter').then((mod) => ({ default: mod.Prism }))
);

// Wrapper component for lazy-loaded syntax highlighter
const CodeHighlight = ({ code, language = 'python', showLineNumbers = true, customStyle = {} }) => {
  const [style, setStyle] = React.useState(null);

  React.useEffect(() => {
    // Dynamically import the style object
    import('react-syntax-highlighter/dist/esm/styles/prism').then((mod) => {
      setStyle(mod.vscDarkPlus);
    });
  }, []);

  return (
    <Suspense fallback={<pre className="tool-code-block">{code}</pre>}>
      <LazyPrism
        language={language}
        style={style}
        customStyle={{
          margin: 0,
          borderRadius: '4px',
          fontSize: '0.875rem',
          ...customStyle,
        }}
        showLineNumbers={showLineNumbers}
      >
        {code}
      </LazyPrism>
    </Suspense>
  );
};

// Simple attachment display component for tool results
const ToolAttachmentDisplay = ({ attachment }) => {
  const isImage = attachment.mime_type?.startsWith('image/');

  if (isImage && attachment.content_url) {
    return (
      <div className="tool-attachment-item" data-testid="attachment-preview">
        <div className="tool-attachment-image">
          <img
            src={attachment.content_url}
            alt={attachment.description || 'Tool result image'}
            className="max-w-full max-h-64 rounded border"
            style={{ width: 'auto', height: 'auto' }}
          />
          {attachment.description && (
            <p className="text-sm text-muted-foreground mt-1">{attachment.description}</p>
          )}
        </div>
      </div>
    );
  }

  // For non-image attachments, show a file link
  return (
    <div className="tool-attachment-item" data-testid="attachment-preview">
      <div className="flex items-center gap-2 p-2 border rounded">
        <DownloadIcon size={16} />
        <span className="text-sm">{attachment.description || 'Attachment'}</span>
        {attachment.content_url && (
          <a
            href={attachment.content_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 hover:underline text-sm"
          >
            Download
          </a>
        )}
      </div>
    </div>
  );
};

// Generic fallback tool UI that handles any tool call
const ToolFallback = ({ toolName, args, result, status, attachments }) => {
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
          <ToolParameterViewer data={args} toolName={toolName} />
        </div>
      )}

      {result && (
        <div className="tool-call-result" data-testid="tool-result">
          <div className="tool-section-label">Result:</div>
          {typeof result === 'string' ? (
            <div className="tool-result-text">{result}</div>
          ) : (
            <pre className="tool-code-block">{JSON.stringify(result, null, 2)}</pre>
          )}
        </div>
      )}

      {attachments && attachments.length > 0 && (
        <div className="tool-call-attachments">
          <div className="tool-section-label">Attachments:</div>
          <div className="tool-attachments-list space-y-2">
            {attachments.map((attachment, index) => (
              <ToolAttachmentDisplay
                key={getAttachmentKey(attachment, index)}
                attachment={attachment}
              />
            ))}
          </div>
        </div>
      )}

      {status?.type === 'running' && <div className="tool-running-message">Executing tool...</div>}
    </div>
  );
};

// Specific tool UI for add_or_update_note
export const AddOrUpdateNoteToolUI = ({ args, result, status }) => {
  return (
    <div className="tool-call-container tool-note" data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">📝 Note</span>
        {status?.type === 'complete' && <CheckCircleIcon size={16} className="tool-success" />}
      </div>

      <div className="tool-note-content">
        {args?.title && <h4 className="tool-note-title">{args.title}</h4>}
        {args?.content && <p className="tool-note-text">{args.content}</p>}

        <div className="tool-note-options">
          {args?.include_in_prompt !== undefined && (
            <span className="tool-note-option">
              {args.include_in_prompt ? '✅' : '❌'} Include in prompt
            </span>
          )}
          {args?.append && <span className="tool-note-option">➕ Append mode</span>}
        </div>
      </div>

      {result && (
        <div className="tool-note-result">
          {typeof result === 'string' ? result : 'Note saved successfully!'}
        </div>
      )}
    </div>
  );
};

// Tool UI for search_documents
export const SearchDocumentsToolUI = ({ args, result, status }) => {
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

      {(args?.source_types || args?.embedding_types || args?.limit) && (
        <div className="tool-search-filters">
          {args.source_types && args.source_types.length > 0 && (
            <span className="tool-search-filter">📁 Sources: {args.source_types.join(', ')}</span>
          )}
          {args.embedding_types && args.embedding_types.length > 0 && (
            <span className="tool-search-filter">🏷️ Types: {args.embedding_types.join(', ')}</span>
          )}
          {args.limit && <span className="tool-search-filter">🔢 Limit: {args.limit}</span>}
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
};

// Tool UI for get_note
export const GetNoteToolUI = ({ args, result, status }) => {
  // Parse the result JSON if it's a string
  let parsedResult = null;
  let parseError = false;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      parseError = true;
    }
  } else if (result && typeof result === 'object') {
    parsedResult = result;
  }

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div className={`tool-call-container tool-get-note ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">📖 Get Note</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-get-note-content">
        {args?.title && (
          <div className="tool-get-note-search">
            Looking for: <strong>"{args.title}"</strong>
          </div>
        )}

        {parsedResult && !parseError && (
          <div className="tool-get-note-result">
            {parsedResult.exists ? (
              <div className="tool-note-found">
                <div className="tool-note-title">
                  <strong>{parsedResult.title}</strong>
                </div>

                {parsedResult.content && (
                  <div className="tool-note-content-display">
                    <div className="tool-section-label">Content:</div>
                    <div className="tool-note-text">{parsedResult.content}</div>
                  </div>
                )}

                {parsedResult.include_in_prompt !== null && (
                  <div className="tool-note-prompt-status">
                    {parsedResult.include_in_prompt ? (
                      <span className="tool-note-included">🔄 Included in prompts</span>
                    ) : (
                      <span className="tool-note-not-included">⏸️ Not included in prompts</span>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="tool-note-not-found">❌ Note not found</div>
            )}
          </div>
        )}

        {result && parseError && (
          <div className="tool-get-note-raw-result">
            <div className="tool-section-label">Result:</div>
            <div className="tool-result-text">{result}</div>
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Searching for note...</div>
      )}
    </div>
  );
};
// Tool UI for list_notes
export const ListNotesToolUI = ({ args, result, status }) => {
  // Parse the result array if it's a string
  let parsedResult = null;
  let parseError = false;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      parseError = true;
    }
  } else if (result && Array.isArray(result)) {
    parsedResult = result;
  }

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  const notes = Array.isArray(parsedResult) ? parsedResult : [];
  const hasFilter = args?.include_in_prompt_only === true;

  return (
    <div
      className={`tool-call-container tool-list-notes ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">📋 List Notes</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-list-notes-params">
        {hasFilter && (
          <div className="tool-filter-indicator">📌 Showing only notes included in prompts</div>
        )}
      </div>

      {result && !parseError && (
        <div className="tool-list-notes-results">
          {notes.length > 0 ? (
            <div className="tool-notes-list">
              <div className="tool-results-count">
                Found {notes.length} note{notes.length !== 1 ? 's' : ''}:
              </div>
              {notes.map((note, index) => (
                <div key={index} className="tool-note-item">
                  <div className="tool-note-header">
                    <div className="tool-note-title">
                      <strong>{note.title}</strong>
                    </div>
                    <div className="tool-note-prompt-status">
                      {note.include_in_prompt ? (
                        <span className="tool-note-included">🔄 In prompts</span>
                      ) : (
                        <span className="tool-note-not-included">⏸️ Not in prompts</span>
                      )}
                    </div>
                  </div>
                  {note.content_preview && (
                    <div className="tool-note-preview">{note.content_preview}</div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="tool-no-results">
              {hasFilter ? 'No notes found that are included in prompts.' : 'No notes found.'}
            </div>
          )}
        </div>
      )}

      {result && parseError && (
        <div className="tool-list-notes-raw-result">
          <div className="tool-section-label">Result:</div>
          <div className="tool-result-text">{result}</div>
        </div>
      )}

      {status?.type === 'running' && <div className="tool-running-message">Loading notes...</div>}
    </div>
  );
};
// Tool UI for delete_note
export const DeleteNoteToolUI = ({ args, result, status }) => {
  // Parse the result JSON if it's a string
  let parsedResult = null;
  let parseError = false;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      parseError = true;
    }
  } else if (result && typeof result === 'object') {
    parsedResult = result;
  }

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    // Show success/failure based on parsed result
    if (parsedResult?.success) {
      statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
      statusClass = 'tool-complete';
    } else {
      statusIcon = <AlertCircleIcon size={16} />;
      statusClass = 'tool-error';
    }
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-delete-note ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🗑️ Delete Note</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-delete-note-content">
        {args?.title && (
          <div className="tool-delete-note-target">
            Deleting: <strong>"{args.title}"</strong>
          </div>
        )}

        {parsedResult && !parseError && (
          <div className="tool-delete-note-result">
            {parsedResult.success ? (
              <div className="tool-delete-success">
                ✅ {parsedResult.message || 'Note deleted successfully'}
              </div>
            ) : (
              <div className="tool-delete-failure">
                ❌ {parsedResult.message || 'Failed to delete note'}
              </div>
            )}
          </div>
        )}

        {result && parseError && (
          <div className="tool-delete-note-raw-result">
            <div className="tool-section-label">Result:</div>
            <div className="tool-result-text">{result}</div>
          </div>
        )}
      </div>

      {status?.type === 'running' && <div className="tool-running-message">Deleting note...</div>}
    </div>
  );
};
export const DelegateToServiceToolUI = ToolFallback;
// Tool UI for schedule_reminder
export const ScheduleReminderToolUI = ({ args, result, status }) => {
  // Format the time in a human-readable way
  const formatTime = (isoString) => {
    if (!isoString) {
      return 'Unknown time';
    }
    try {
      const date = new Date(isoString);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return isoString; // fallback to raw string if parsing fails
    }
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div className={`tool-call-container tool-reminder ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">⏰ Schedule Reminder</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-reminder-content">
        {args?.message && (
          <div className="tool-reminder-message">
            <strong>{args.message}</strong>
          </div>
        )}

        {args?.time && <div className="tool-reminder-time">📅 {formatTime(args.time)}</div>}

        {args?.recurring && <div className="tool-reminder-recurring">🔄 Recurring reminder</div>}
      </div>

      {result && (
        <div className="tool-reminder-result">
          {typeof result === 'string' ? result : 'Reminder scheduled successfully!'}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Scheduling reminder...</div>
      )}
    </div>
  );
};
// Tool UI for schedule_future_callback
export const ScheduleFutureCallbackToolUI = ({ args, result, status }) => {
  // Format the time in a human-readable way
  const formatTime = (isoString) => {
    if (!isoString) {
      return 'Unknown time';
    }
    try {
      const date = new Date(isoString);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return isoString; // fallback to raw string if parsing fails
    }
  };

  // Helper function to display callback data in a readable format
  const formatCallbackData = (callbackData) => {
    if (!callbackData || typeof callbackData !== 'object') {
      return null;
    }

    const entries = Object.entries(callbackData);
    if (entries.length === 0) {
      return null;
    }

    return entries.map(([key, value]) => (
      <div key={key} className="tool-callback-data-item">
        <strong>{key}:</strong> {typeof value === 'object' ? JSON.stringify(value) : String(value)}
      </div>
    ));
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div className={`tool-call-container tool-callback ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">⏳ Schedule Future Callback</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-callback-content">
        {args?.description && (
          <div className="tool-callback-description">
            <strong>{args.description}</strong>
          </div>
        )}

        {args?.callback_time && (
          <div className="tool-callback-time">🕐 {formatTime(args.callback_time)}</div>
        )}

        {args?.callback_data && Object.keys(args.callback_data).length > 0 && (
          <div className="tool-callback-data">
            <div className="tool-section-label">Callback Data:</div>
            <div className="tool-callback-data-list">{formatCallbackData(args.callback_data)}</div>
          </div>
        )}
      </div>

      {result && (
        <div className="tool-callback-result">
          {typeof result === 'string' ? result : 'Callback scheduled successfully!'}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Scheduling callback...</div>
      )}
    </div>
  );
};
// Tool UI for schedule_recurring_task
export const ScheduleRecurringTaskToolUI = ({ args, result, status }) => {
  // Helper function to display task parameters in a readable format
  const formatTaskParameters = (taskParams) => {
    if (!taskParams || typeof taskParams !== 'object') {
      return null;
    }

    const entries = Object.entries(taskParams);
    if (entries.length === 0) {
      return null;
    }

    return entries.map(([key, value]) => (
      <div key={key} className="tool-task-param-item">
        <strong>{key}:</strong> {typeof value === 'object' ? JSON.stringify(value) : String(value)}
      </div>
    ));
  };

  // Helper function to format schedule pattern for display
  const formatSchedulePattern = (pattern) => {
    if (!pattern) {
      return 'Unknown schedule';
    }

    // Common patterns to make more readable
    const readablePatterns = {
      '0 9 * * 1-5': 'Weekdays at 9:00 AM',
      '0 0 * * 0': 'Every Sunday at midnight',
      '0 0 1 * *': 'First day of every month',
      '0 12 * * *': 'Daily at noon',
      '*/15 * * * *': 'Every 15 minutes',
      '0 */4 * * *': 'Every 4 hours',
      '30 8 * * 1': 'Every Monday at 8:30 AM',
    };

    return readablePatterns[pattern] || `Cron: ${pattern}`;
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-recurring-task ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🔄 Schedule Recurring Task</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-recurring-task-content">
        {args?.task_name && (
          <div className="tool-task-name">
            <strong>{args.task_name}</strong>
          </div>
        )}

        {args?.schedule_pattern && (
          <div className="tool-task-schedule">
            📅 {formatSchedulePattern(args.schedule_pattern)}
          </div>
        )}

        {args?.description && <div className="tool-task-description">{args.description}</div>}

        {args?.task_parameters && Object.keys(args.task_parameters).length > 0 && (
          <div className="tool-task-parameters">
            <div className="tool-section-label">Parameters:</div>
            <div className="tool-task-parameters-list">
              {formatTaskParameters(args.task_parameters)}
            </div>
          </div>
        )}

        {args?.enabled === false && (
          <div className="tool-task-disabled">⏸️ Task scheduled as disabled</div>
        )}
      </div>

      {result && (
        <div className="tool-recurring-task-result">
          {typeof result === 'string' ? result : 'Recurring task scheduled successfully!'}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Creating recurring task...</div>
      )}
    </div>
  );
};
// Tool UI for list_pending_callbacks
export const ListPendingCallbacksToolUI = ({ args, result, status }) => {
  // Parse the result array if it's a string
  let parsedResult = null;
  let parseError = false;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      parseError = true;
    }
  } else if (result && Array.isArray(result)) {
    parsedResult = result;
  }

  // Helper function to format timestamp
  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Unknown time';
    }
    try {
      const date = new Date(timestamp);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return timestamp; // fallback to raw string if parsing fails
    }
  };

  // Helper function to get status icon for callback
  const getCallbackStatusIcon = (callback) => {
    if (callback.status === 'pending') {
      return '⏳';
    }
    if (callback.status === 'completed') {
      return '✅';
    }
    if (callback.status === 'failed') {
      return '❌';
    }
    if (callback.status === 'cancelled') {
      return '🚫';
    }
    return '📌';
  };

  // Helper function to truncate long descriptions
  const truncateDescription = (description, maxLength = 100) => {
    if (!description || description.length <= maxLength) {
      return description;
    }
    return description.substring(0, maxLength) + '...';
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  const callbacks = Array.isArray(parsedResult) ? parsedResult : [];

  return (
    <div
      className={`tool-call-container tool-list-callbacks ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">📋 Pending Callbacks</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-list-callbacks-params">
        {args?.limit && (
          <div className="tool-filter-indicator">📊 Limit: {args.limit} callbacks</div>
        )}
      </div>

      {result && !parseError && (
        <div className="tool-list-callbacks-results">
          {callbacks.length > 0 ? (
            <div className="tool-callbacks-list">
              <div className="tool-results-count">
                Found {callbacks.length} callback{callbacks.length !== 1 ? 's' : ''}:
              </div>
              {callbacks.map((callback, index) => (
                <div key={index} className="tool-callback-item">
                  <div className="tool-callback-header">
                    <div className="tool-callback-status">
                      {getCallbackStatusIcon(callback)}{' '}
                      <strong>{callback.status || 'pending'}</strong>
                    </div>
                    <div className="tool-callback-id">
                      ID: {callback.id || callback.callback_id || `#${index + 1}`}
                    </div>
                  </div>

                  {callback.description && (
                    <div className="tool-callback-description">
                      {truncateDescription(callback.description)}
                    </div>
                  )}

                  {callback.callback_time && (
                    <div className="tool-callback-time">
                      🕐 {formatTimestamp(callback.callback_time)}
                    </div>
                  )}

                  {callback.created_at && (
                    <div className="tool-callback-created">
                      📅 Created: {formatTimestamp(callback.created_at)}
                    </div>
                  )}

                  {callback.callback_data && Object.keys(callback.callback_data).length > 0 && (
                    <div className="tool-callback-data-preview">
                      📄 Has callback data ({Object.keys(callback.callback_data).length} keys)
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="tool-no-results">No pending callbacks found.</div>
          )}
        </div>
      )}

      {result && parseError && (
        <div className="tool-list-callbacks-raw-result">
          <div className="tool-section-label">Result:</div>
          <div className="tool-result-text">{result}</div>
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Loading pending callbacks...</div>
      )}
    </div>
  );
};
// Tool UI for modify_pending_callback
export const ModifyPendingCallbackToolUI = ({ args, result, status }) => {
  // Format the time in a human-readable way
  const formatTime = (isoString) => {
    if (!isoString) {
      return 'Unknown time';
    }
    try {
      const date = new Date(isoString);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return isoString; // fallback to raw string if parsing fails
    }
  };

  // Helper function to display changes in a before/after format
  const formatChanges = (changes) => {
    if (!changes || typeof changes !== 'object') {
      return null;
    }

    const entries = Object.entries(changes);
    if (entries.length === 0) {
      return null;
    }

    return entries.map(([key, value]) => {
      // Handle special formatting for time fields
      if (key.includes('time') && typeof value === 'string') {
        return (
          <div key={key} className="tool-modify-change-item">
            <strong>{key}:</strong> {formatTime(value)}
          </div>
        );
      }

      return (
        <div key={key} className="tool-modify-change-item">
          <strong>{key}:</strong>{' '}
          {typeof value === 'object' ? JSON.stringify(value) : String(value)}
        </div>
      );
    });
  };

  // Parse result to check if it contains old/new values comparison
  let parsedResult = null;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      // Result is not valid JSON, will be handled as string
    }
  } else if (result && typeof result === 'object') {
    parsedResult = result;
  }

  // Check if the operation was successful
  const isSuccess =
    parsedResult?.success !== false &&
    (!result || !result.toString().toLowerCase().includes('error'));

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    if (isSuccess) {
      statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
      statusClass = 'tool-complete';
    } else {
      statusIcon = <AlertCircleIcon size={16} />;
      statusClass = 'tool-error';
    }
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-modify-callback ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">✏️ Modify Callback</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-modify-callback-content">
        {args?.callback_id && (
          <div className="tool-callback-id">
            Callback ID: <strong>{args.callback_id}</strong>
          </div>
        )}

        {args?.changes && Object.keys(args.changes).length > 0 && (
          <div className="tool-modify-changes">
            <div className="tool-section-label">Changes:</div>
            <div className="tool-modify-changes-list">{formatChanges(args.changes)}</div>
          </div>
        )}

        {/* Show old vs new values if available in result */}
        {parsedResult && parsedResult.old_values && parsedResult.new_values && (
          <div className="tool-modify-comparison">
            <div className="tool-modify-before">
              <div className="tool-section-label">Before:</div>
              <div className="tool-modify-values">{formatChanges(parsedResult.old_values)}</div>
            </div>
            <div className="tool-modify-after">
              <div className="tool-section-label">After:</div>
              <div className="tool-modify-values">{formatChanges(parsedResult.new_values)}</div>
            </div>
          </div>
        )}
      </div>

      {result && (
        <div className="tool-modify-callback-result">
          {isSuccess ? (
            <div className="tool-modify-success">
              ✅ {typeof result === 'string' ? result : 'Callback modified successfully!'}
            </div>
          ) : (
            <div className="tool-modify-error">
              ❌ {typeof result === 'string' ? result : 'Failed to modify callback'}
            </div>
          )}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Modifying callback...</div>
      )}
    </div>
  );
};
// Tool UI for cancel_pending_callback
export const CancelPendingCallbackToolUI = ({ args, result, status }) => {
  // Parse result to check if it contains callback details
  let parsedResult = null;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      // Result is not valid JSON, will be handled as string
    }
  } else if (result && typeof result === 'object') {
    parsedResult = result;
  }

  // Check if the operation was successful
  const isSuccess =
    parsedResult?.success !== false &&
    (!result || !result.toString().toLowerCase().includes('error'));

  // Format the time in a human-readable way
  const formatTime = (isoString) => {
    if (!isoString) {
      return 'Unknown time';
    }
    try {
      const date = new Date(isoString);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return isoString; // fallback to raw string if parsing fails
    }
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    if (isSuccess) {
      statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
      statusClass = 'tool-complete';
    } else {
      statusIcon = <AlertCircleIcon size={16} />;
      statusClass = 'tool-error';
    }
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-cancel-callback ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🚫 Cancel Callback</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-cancel-callback-content">
        {args?.callback_id && (
          <div className="tool-callback-id">
            Cancelling Callback ID: <strong>{args.callback_id}</strong>
          </div>
        )}

        {/* Show callback details if available in result */}
        {parsedResult && parsedResult.callback && (
          <div className="tool-cancelled-callback-details">
            <div className="tool-section-label">Cancelled Callback Details:</div>
            <div className="tool-callback-details">
              {parsedResult.callback.description && (
                <div className="tool-callback-description">
                  📝 {parsedResult.callback.description}
                </div>
              )}
              {parsedResult.callback.callback_time && (
                <div className="tool-callback-time">
                  🕐 Was scheduled for: {formatTime(parsedResult.callback.callback_time)}
                </div>
              )}
              {parsedResult.callback.status && (
                <div className="tool-callback-original-status">
                  📊 Previous status: {parsedResult.callback.status}
                </div>
              )}
            </div>
          </div>
        )}

        {args?.reason && (
          <div className="tool-cancel-reason">
            <div className="tool-section-label">Reason:</div>
            <div className="tool-reason-text">{args.reason}</div>
          </div>
        )}
      </div>

      {result && (
        <div className="tool-cancel-callback-result">
          {isSuccess ? (
            <div className="tool-cancel-success">
              ✅ {typeof result === 'string' ? result : 'Callback cancelled successfully!'}
            </div>
          ) : (
            <div className="tool-cancel-error">
              ❌ {typeof result === 'string' ? result : 'Failed to cancel callback'}
            </div>
          )}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Cancelling callback...</div>
      )}
    </div>
  );
};
// Tool UI for schedule_action
export const ScheduleActionToolUI = ({ args, result, status }) => {
  // Format the time in a human-readable way
  const formatTime = (isoString) => {
    if (!isoString) {
      return 'Unknown time';
    }
    try {
      const date = new Date(isoString);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return isoString; // fallback to raw string if parsing fails
    }
  };

  // Helper function to display action data in a readable format
  const formatActionData = (actionData) => {
    if (!actionData || typeof actionData !== 'object') {
      return null;
    }

    // For simple objects, display key-value pairs nicely
    const entries = Object.entries(actionData);
    if (entries.length === 0) {
      return null;
    }

    return entries.map(([key, value]) => (
      <div key={key} className="tool-action-data-item">
        <strong>{key}:</strong> {typeof value === 'object' ? JSON.stringify(value) : String(value)}
      </div>
    ));
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div className={`tool-call-container tool-action ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">⚡ Schedule Action</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-action-content">
        {args?.action_name && (
          <div className="tool-action-name">
            <strong>{args.action_name}</strong>
          </div>
        )}

        {args?.execution_time && (
          <div className="tool-action-time">🕐 {formatTime(args.execution_time)}</div>
        )}

        {args?.action_data && Object.keys(args.action_data).length > 0 && (
          <div className="tool-action-data">
            <div className="tool-section-label">Parameters:</div>
            <div className="tool-action-data-list">{formatActionData(args.action_data)}</div>
          </div>
        )}

        {args?.recurring && <div className="tool-action-recurring">🔄 Recurring action</div>}
      </div>

      {result && (
        <div className="tool-action-result">
          {typeof result === 'string' ? result : 'Action scheduled successfully!'}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Scheduling action...</div>
      )}
    </div>
  );
};
// Tool UI for schedule_recurring_action
export const ScheduleRecurringActionToolUI = ({ args, result, status }) => {
  // Helper function to format date-time
  const formatDateTime = (isoString) => {
    if (!isoString) {
      return 'Unknown time';
    }
    try {
      return new Date(isoString).toLocaleString();
    } catch (_e) {
      return isoString;
    }
  };

  // Helper function to format recurrence rule
  const formatRecurrenceRule = (rrule) => {
    if (!rrule) {
      return 'Unknown recurrence';
    }

    // Simple formatting for common patterns
    if (rrule.includes('FREQ=DAILY')) {
      const intervalMatch = rrule.match(/INTERVAL=(\d+)/);
      const interval = intervalMatch ? parseInt(intervalMatch[1]) : 1;
      return interval === 1 ? 'Daily' : `Every ${interval} days`;
    } else if (rrule.includes('FREQ=WEEKLY')) {
      const dayMatch = rrule.match(/BYDAY=([^;]+)/);
      const days = dayMatch ? dayMatch[1] : '';
      return days ? `Weekly on ${days.replace(/,/g, ', ')}` : 'Weekly';
    } else if (rrule.includes('FREQ=HOURLY')) {
      const intervalMatch = rrule.match(/INTERVAL=(\d+)/);
      const interval = intervalMatch ? parseInt(intervalMatch[1]) : 1;
      return interval === 1 ? 'Hourly' : `Every ${interval} hours`;
    }

    return rrule;
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-schedule-recurring-action ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🔄 Schedule Recurring Action</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-schedule-recurring-action-content">
        <div className="tool-schedule-details">
          {args?.start_time && (
            <div className="tool-schedule-start-time">
              📅 Start Time: <strong>{formatDateTime(args.start_time)}</strong>
            </div>
          )}

          {args?.recurrence_rule && (
            <div className="tool-schedule-recurrence">
              🔁 Recurrence: <strong>{formatRecurrenceRule(args.recurrence_rule)}</strong>
              <div className="tool-schedule-rrule">
                <small>RRULE: {args.recurrence_rule}</small>
              </div>
            </div>
          )}

          {args?.action_type && (
            <div className="tool-schedule-action-type">
              ⚡ Action Type: <strong>{args.action_type}</strong>
            </div>
          )}

          {args?.task_name && (
            <div className="tool-schedule-task-name">
              🏷️ Task Name: <strong>{args.task_name}</strong>
            </div>
          )}

          {args?.action_config && Object.keys(args.action_config).length > 0 && (
            <div className="tool-schedule-action-config">
              <div className="tool-section-label">Action Configuration:</div>
              <pre className="tool-code-block">{JSON.stringify(args.action_config, null, 2)}</pre>
            </div>
          )}
        </div>

        {result && (
          <div className="tool-schedule-recurring-action-result">
            {status?.type === 'incomplete' ? (
              <div className="tool-error-message">
                <AlertCircleIcon size={16} />
                <span>{result}</span>
              </div>
            ) : (
              <div className="tool-success-message">
                <CheckCircleIcon size={16} />
                <span>{result}</span>
              </div>
            )}
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Scheduling recurring action...</div>
      )}
    </div>
  );
};
// Tool UI for get_full_document_content
export const GetFullDocumentContentToolUI = ({ args, result, status }) => {
  // Helper function to count words and characters
  const getContentStats = (content) => {
    if (!content || typeof content !== 'string') {
      return { chars: 0, words: 0 };
    }
    const chars = content.length;
    const words = content.trim() ? content.trim().split(/\s+/).length : 0;
    return { chars, words };
  };

  // Helper function to format file size
  const formatSize = (chars) => {
    if (chars < 1024) {
      return `${chars} chars`;
    }
    if (chars < 1024 * 1024) {
      return `${(chars / 1024).toFixed(1)}KB`;
    }
    return `${(chars / (1024 * 1024)).toFixed(1)}MB`;
  };

  // State for content expansion (for long content)
  const [isExpanded, setIsExpanded] = React.useState(false);
  const MAX_PREVIEW_CHARS = 500;
  const shouldTruncate = result && typeof result === 'string' && result.length > MAX_PREVIEW_CHARS;

  // Check if result indicates an error or not found
  const isError =
    result &&
    typeof result === 'string' &&
    (result.toLowerCase().includes('error') ||
      result.toLowerCase().includes('not found') ||
      result.toLowerCase().includes('failed'));

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    if (isError) {
      statusIcon = <AlertCircleIcon size={16} />;
      statusClass = 'tool-error';
    } else {
      statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
      statusClass = 'tool-complete';
    }
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  const stats =
    result && typeof result === 'string' ? getContentStats(result) : { chars: 0, words: 0 };
  const displayContent =
    shouldTruncate && !isExpanded ? result.substring(0, MAX_PREVIEW_CHARS) + '...' : result;

  return (
    <div className={`tool-call-container tool-document ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">📄 Get Document Content</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-document-content">
        {args?.document_id && (
          <div className="tool-document-id">
            Document ID: <strong>{args.document_id}</strong>
          </div>
        )}

        {result && (
          <div className="tool-document-result">
            {isError ? (
              <div className="tool-document-error">❌ {result}</div>
            ) : (
              <div className="tool-document-success">
                <div className="tool-document-stats">
                  📊 {formatSize(stats.chars)} • {stats.words.toLocaleString()} words
                </div>

                <div className="tool-document-text">
                  <div className="tool-section-label">Content:</div>
                  <div className="tool-document-content-display">
                    <pre>{displayContent}</pre>
                    {shouldTruncate && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setIsExpanded(!isExpanded)}
                        type="button"
                      >
                        {isExpanded ? '🔼 Show less' : '🔽 Show more'}
                      </Button>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Retrieving document content...</div>
      )}
    </div>
  );
};
// Tool UI for ingest_document_from_url
export const IngestDocumentFromUrlToolUI = ({ args, result, status }) => {
  // Helper function to truncate long URLs
  const truncateUrl = (url, maxLength = 60) => {
    if (!url || url.length <= maxLength) {
      return url;
    }

    // Try to keep the domain and end of the path visible
    try {
      const urlObj = new window.URL(url);
      const domain = urlObj.hostname;
      const path = urlObj.pathname + urlObj.search;

      if (domain.length + path.length <= maxLength) {
        return `${domain}${path}`;
      }

      const availablePathLength = maxLength - domain.length - 3; // 3 for "..."
      if (availablePathLength > 10) {
        const endPath = path.slice(-availablePathLength);
        return `${domain}...${endPath}`;
      }
    } catch (_e) {
      // Fallback for invalid URLs
    }

    // Fallback: simple truncation
    return url.substring(0, maxLength - 3) + '...';
  };

  // Helper function to extract domain/hostname for display
  const getDomainFromUrl = (url) => {
    try {
      return new window.URL(url).hostname;
    } catch (_e) {
      return 'unknown domain';
    }
  };

  // Helper function to get file type from URL
  const getFileTypeFromUrl = (url) => {
    try {
      const urlObj = new window.URL(url);
      const pathname = urlObj.pathname.toLowerCase();

      if (pathname.endsWith('.pdf')) {
        return 'PDF';
      }
      if (pathname.endsWith('.doc') || pathname.endsWith('.docx')) {
        return 'Word';
      }
      if (pathname.endsWith('.txt')) {
        return 'Text';
      }
      if (pathname.endsWith('.md')) {
        return 'Markdown';
      }
      if (pathname.endsWith('.html') || pathname.endsWith('.htm')) {
        return 'Web page';
      }
      if (pathname.includes('/')) {
        return 'Web page';
      }

      return 'Document';
    } catch (_e) {
      return 'Document';
    }
  };

  // Check if result indicates success or failure
  const isSuccess =
    result &&
    typeof result === 'string' &&
    (result.toLowerCase().includes('success') ||
      result.toLowerCase().includes('ingested') ||
      result.toLowerCase().includes('processed') ||
      result.toLowerCase().includes('added'));

  const isError =
    result &&
    typeof result === 'string' &&
    (result.toLowerCase().includes('error') ||
      result.toLowerCase().includes('failed') ||
      result.toLowerCase().includes('unable'));

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <DownloadIcon size={16} className="animate-bounce" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    if (isError) {
      statusIcon = <AlertCircleIcon size={16} />;
      statusClass = 'tool-error';
    } else {
      statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
      statusClass = 'tool-complete';
    }
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  const fileType = args?.url ? getFileTypeFromUrl(args.url) : 'Document';
  const domain = args?.url ? getDomainFromUrl(args.url) : '';
  const truncatedUrl = args?.url ? truncateUrl(args.url) : '';

  return (
    <div
      className={`tool-call-container tool-ingest-url ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">📥 Ingest Document from URL</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-ingest-url-content">
        {args?.url && (
          <div className="tool-ingest-url-details">
            <div className="tool-ingest-url-main">
              <div className="tool-ingest-url-type">📄 {fileType}</div>
              <div className="tool-ingest-url-address" title={args.url}>
                🌐 {truncatedUrl}
              </div>
              {domain && <div className="tool-ingest-url-domain">from {domain}</div>}
            </div>
          </div>
        )}

        {args?.metadata && Object.keys(args.metadata).length > 0 && (
          <div className="tool-ingest-metadata">
            <div className="tool-section-label">Metadata:</div>
            <div className="tool-metadata-items">
              {Object.entries(args.metadata).map(([key, value]) => (
                <div key={key} className="tool-metadata-item">
                  <strong>{key}:</strong> {String(value)}
                </div>
              ))}
            </div>
          </div>
        )}

        {result && (
          <div className="tool-ingest-result">
            {isError ? (
              <div className="tool-ingest-error">❌ {result}</div>
            ) : isSuccess ? (
              <div className="tool-ingest-success">✅ {result}</div>
            ) : (
              <div className="tool-ingest-info">{result}</div>
            )}
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Downloading and ingesting document...</div>
      )}
    </div>
  );
};
// Tool UI for get_user_documentation_content
export const GetUserDocumentationContentToolUI = ({ args, result, status }) => {
  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-user-docs ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">📖 User Documentation</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-user-docs-content">
        {args?.filename && (
          <div className="tool-user-docs-request">
            Reading documentation file: <strong>{args.filename}</strong>
          </div>
        )}

        {result && (
          <div className="tool-user-docs-result">
            {status?.type === 'incomplete' ? (
              <div className="tool-error-message">
                <AlertCircleIcon size={16} />
                <span>{result}</span>
              </div>
            ) : (
              <div className="tool-user-docs-content-display">
                <div className="tool-section-label">Documentation Content:</div>
                <div className="tool-result-text">
                  {result.length > 1000 ? (
                    <>
                      <div>{result.substring(0, 1000)}...</div>
                      <div className="tool-content-truncated">
                        Content truncated (showing first 1000 characters of {result.length} total)
                      </div>
                    </>
                  ) : (
                    result
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Reading documentation file...</div>
      )}
    </div>
  );
};
// Tool UI for query_recent_events
export const QueryRecentEventsToolUI = ({ args, result, status }) => {
  // Parse the result JSON if it's a string
  let parsedResult = null;
  let parseError = false;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      parseError = true;
    }
  } else if (result && typeof result === 'object') {
    parsedResult = result;
  }

  // Helper function to format timestamp
  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Unknown time';
    }
    try {
      const date = new Date(timestamp);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return timestamp; // fallback to raw string if parsing fails
    }
  };

  // Helper function to truncate long event data
  const truncateEventData = (data, maxLength = 200) => {
    const str = JSON.stringify(data, null, 2);
    if (str.length <= maxLength) {
      return str;
    }
    return str.substring(0, maxLength) + '...';
  };

  // Helper function to get source icon
  const getSourceIcon = (source) => {
    switch (source?.toLowerCase()) {
      case 'home_assistant':
        return '🏠';
      case 'indexing':
        return '📚';
      case 'webhook':
        return '🔗';
      default:
        return '📡';
    }
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  const events = parsedResult?.events || [];
  const hasError = status?.type === 'incomplete';

  return (
    <div
      className={`tool-call-container tool-query-events ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">📡 Query Recent Events</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-query-events-params">
        {args?.source_id && (
          <div className="tool-filter-indicator">
            {getSourceIcon(args.source_id)} Source: <strong>{args.source_id}</strong>
          </div>
        )}
        {args?.hours && (
          <div className="tool-filter-indicator">
            🕐 Hours: <strong>{args.hours}</strong>
          </div>
        )}
        {args?.limit && (
          <div className="tool-filter-indicator">
            📊 Limit: <strong>{args.limit}</strong> events
          </div>
        )}
      </div>

      {result && !parseError && (
        <div className="tool-query-events-results">
          {hasError ? (
            <div className="tool-error-message">❌ {result}</div>
          ) : parsedResult?.message ? (
            <div className="tool-no-results">{parsedResult.message}</div>
          ) : events.length > 0 ? (
            <div className="tool-events-list">
              <div className="tool-results-count">
                Found {parsedResult.count || events.length} event{events.length !== 1 ? 's' : ''}:
              </div>
              {events.map((event, index) => (
                <div key={index} className="tool-event-item">
                  <div className="tool-event-header">
                    <div className="tool-event-source">
                      {getSourceIcon(event.source_id)} <strong>{event.source_id}</strong>
                    </div>
                    <div className="tool-event-id">ID: {event.event_id}</div>
                  </div>

                  <div className="tool-event-timestamp">🕐 {formatTimestamp(event.timestamp)}</div>

                  {event.event_data && (
                    <div className="tool-event-data">
                      <div className="tool-section-label">Event Data:</div>
                      <pre className="tool-code-block">{truncateEventData(event.event_data)}</pre>
                    </div>
                  )}

                  {event.triggered_listeners && event.triggered_listeners.length > 0 && (
                    <div className="tool-event-listeners">
                      🎯 Triggered {event.triggered_listeners.length} listener
                      {event.triggered_listeners.length !== 1 ? 's' : ''}
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="tool-no-results">No events found in the specified time range.</div>
          )}
        </div>
      )}

      {result && parseError && (
        <div className="tool-query-events-raw-result">
          <div className="tool-section-label">Result:</div>
          <div className="tool-result-text">{result}</div>
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Querying recent events...</div>
      )}
    </div>
  );
};
// Tool UI for test_event_listener
export const TestEventListenerToolUI = ({ args, result, status }) => {
  // Parse the result JSON if it's a string
  let parsedResult = null;
  let parseError = false;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      parseError = true;
    }
  } else if (result && typeof result === 'object') {
    parsedResult = result;
  }

  // Helper function to get source icon
  const getSourceIcon = (source) => {
    switch (source?.toLowerCase()) {
      case 'home_assistant':
        return '🏠';
      case 'indexing':
        return '📚';
      case 'webhook':
        return '🔗';
      default:
        return '📡';
    }
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    if (parsedResult?.error || parseError) {
      statusIcon = <AlertCircleIcon size={16} />;
      statusClass = 'tool-error';
    } else {
      statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
      statusClass = 'tool-complete';
    }
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-test-listener ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🧪 Test Event Listener</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-test-listener-content">
        {args?.source && (
          <div className="tool-listener-source">
            {getSourceIcon(args.source)} Testing source: <strong>{args.source}</strong>
          </div>
        )}

        {args?.hours && (
          <div className="tool-test-timerange">
            ⏱️ Looking back: <strong>{args.hours} hours</strong>
          </div>
        )}

        {args?.match_conditions && Object.keys(args.match_conditions).length > 0 && (
          <div className="tool-test-conditions">
            <div className="tool-section-label">Match Conditions:</div>
            <pre className="tool-code-block">{JSON.stringify(args.match_conditions, null, 2)}</pre>
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Testing event listener conditions...</div>
      )}

      {result && (
        <div className="tool-test-results">
          {parseError ? (
            <div className="tool-result-text">{result}</div>
          ) : parsedResult ? (
            <div>
              {parsedResult.error ? (
                <div className="tool-error-result">
                  <strong>Error:</strong> {parsedResult.message || parsedResult.error}
                </div>
              ) : (
                <div className="tool-test-success">
                  <div className="tool-test-summary">
                    📊 <strong>Results:</strong> {parsedResult.matched_count || 0} matched out of{' '}
                    {parsedResult.total_tested || 0} events tested
                  </div>

                  {parsedResult.message && (
                    <div className="tool-test-message">{parsedResult.message}</div>
                  )}

                  {parsedResult.analysis && parsedResult.analysis.length > 0 && (
                    <div className="tool-test-analysis">
                      <div className="tool-section-label">Analysis:</div>
                      {parsedResult.analysis.map((item, index) => (
                        <div key={index} className="tool-analysis-item">
                          {item}
                        </div>
                      ))}
                    </div>
                  )}

                  {parsedResult.matched_events && parsedResult.matched_events.length > 0 && (
                    <div className="tool-matched-events">
                      <div className="tool-section-label">Matched Events:</div>
                      {parsedResult.matched_events.slice(0, 3).map((event, index) => (
                        <div key={index} className="tool-event-item">
                          <div className="tool-event-timestamp">
                            {new Date(event.timestamp).toLocaleString()}
                          </div>
                          {event.event_data && (
                            <pre className="tool-code-block tool-event-data">
                              {JSON.stringify(event.event_data, null, 2)}
                            </pre>
                          )}
                        </div>
                      ))}
                      {parsedResult.matched_events.length > 3 && (
                        <div className="tool-more-events">
                          ... and {parsedResult.matched_events.length - 3} more events
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          ) : (
            <div className="tool-result-text">{result}</div>
          )}
        </div>
      )}
    </div>
  );
};
// Tool UI for render_home_assistant_template
export const RenderHomeAssistantTemplateToolUI = ({ args, result, status }) => {
  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-ha-template ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🏠 Home Assistant Template</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-ha-template-content">
        {args?.template && (
          <div className="tool-ha-template-input">
            <div className="tool-section-label">Template:</div>
            <pre className="tool-code-block">{args.template}</pre>
          </div>
        )}

        {result && (
          <div className="tool-ha-template-result">
            {status?.type === 'incomplete' ? (
              <div className="tool-error-message">
                <AlertCircleIcon size={16} />
                <span>{result}</span>
              </div>
            ) : (
              <div className="tool-ha-template-output">
                <div className="tool-section-label">Template Result:</div>
                <div className="tool-result-text">
                  {result === 'Template rendered to empty result' ? (
                    <em className="tool-empty-result">Template rendered to empty result</em>
                  ) : (
                    result
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Rendering Home Assistant template...</div>
      )}
    </div>
  );
};
// Tool UI for add_calendar_event
export const AddCalendarEventToolUI = ({ args, result, status }) => {
  // Format the time in a human-readable way
  const formatTime = (isoString) => {
    if (!isoString) {
      return 'Unknown time';
    }
    try {
      const date = new Date(isoString);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return isoString; // fallback to raw string if parsing fails
    }
  };

  // Format date only (for all-day events)
  const formatDate = (isoString) => {
    if (!isoString) {
      return 'Unknown date';
    }
    try {
      const date = new Date(isoString);
      return date.toLocaleDateString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch (_e) {
      return isoString; // fallback to raw string if parsing fails
    }
  };

  // Check if it's a multi-day all-day event
  const isMultiDayEvent = (startTime, endTime, allDay) => {
    if (!allDay || !startTime || !endTime) {
      return false;
    }
    try {
      const start = new Date(startTime);
      const end = new Date(endTime);
      return start.toDateString() !== end.toDateString();
    } catch (_e) {
      return false;
    }
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div className={`tool-call-container tool-calendar ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">📅 Calendar Event</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-calendar-content">
        {args?.summary && (
          <div className="tool-calendar-summary">
            <strong>{args.summary}</strong>
          </div>
        )}

        {args?.start_time && (
          <div className="tool-calendar-time">
            {args?.all_day ? (
              // All-day event display
              isMultiDayEvent(args.start_time, args.end_time, args.all_day) ? (
                <span>
                  📅 {formatDate(args.start_time)} - {formatDate(args.end_time)} (All day)
                </span>
              ) : (
                <span>📅 {formatDate(args.start_time)} (All day)</span>
              )
            ) : // Timed event display
            args?.end_time ? (
              <span>
                🕐 {formatTime(args.start_time)} - {formatTime(args.end_time)}
              </span>
            ) : (
              <span>🕐 {formatTime(args.start_time)}</span>
            )}
          </div>
        )}

        {args?.description && <div className="tool-calendar-description">{args.description}</div>}

        {args?.recurrence_rule && (
          <div className="tool-calendar-recurrence">🔄 Recurring event</div>
        )}

        {args?.calendar_url && (
          <div className="tool-calendar-location">
            📍 Calendar: {args.calendar_url.split('/').pop() || 'Unknown'}
          </div>
        )}
      </div>

      {result && (
        <div className="tool-calendar-result">
          {typeof result === 'string' ? result : 'Event created successfully!'}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Creating calendar event...</div>
      )}
    </div>
  );
};
// Tool UI for search_calendar_events
export const SearchCalendarEventsToolUI = ({ args, result, status }) => {
  // Helper function to parse events from the result string
  const parseEvents = (resultStr) => {
    if (!resultStr || typeof resultStr !== 'string') {
      return [];
    }

    // Check if it's an error message
    if (resultStr.includes('Error:') || resultStr.includes('No events found')) {
      return [];
    }

    const lines = resultStr.split('\n');
    const events = [];
    let currentEvent = null;

    for (const line of lines) {
      const trimmed = line.trim();

      // Event number lines like "1. Meeting Title"
      if (/^\d+\.\s/.test(trimmed)) {
        if (currentEvent) {
          events.push(currentEvent);
        }
        currentEvent = {
          summary: trimmed.replace(/^\d+\.\s/, ''),
          start: '',
          end: '',
          uid: '',
          calendar_url: '',
        };
      } else if (currentEvent) {
        // Parse event details
        if (trimmed.startsWith('Start:')) {
          currentEvent.start = trimmed.replace('Start:', '').trim();
        } else if (trimmed.startsWith('End:')) {
          currentEvent.end = trimmed.replace('End:', '').trim();
        } else if (trimmed.startsWith('UID:')) {
          currentEvent.uid = trimmed.replace('UID:', '').trim();
        } else if (trimmed.startsWith('Calendar:')) {
          currentEvent.calendar_url = trimmed.replace('Calendar:', '').trim();
        }
      }
    }

    if (currentEvent) {
      events.push(currentEvent);
    }

    return events;
  };

  // Helper function to format date/time for display
  const formatDateTime = (dateTimeStr) => {
    if (!dateTimeStr || dateTimeStr === 'No start time' || dateTimeStr === 'No end time') {
      return dateTimeStr || 'Unknown time';
    }

    try {
      // Try to parse and format the date
      const date = new Date(dateTimeStr);
      if (isNaN(date.getTime())) {
        return dateTimeStr; // Return as-is if can't parse
      }

      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return dateTimeStr; // fallback to raw string if parsing fails
    }
  };

  // Helper function to get calendar name from URL
  const getCalendarName = (calendarUrl) => {
    if (!calendarUrl) {
      return 'Unknown calendar';
    }
    try {
      const urlParts = calendarUrl.split('/');
      return urlParts[urlParts.length - 1] || 'Calendar';
    } catch (_e) {
      return 'Calendar';
    }
  };

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  const events = result ? parseEvents(result) : [];
  const hasError = result && typeof result === 'string' && result.includes('Error:');
  const noResults = result && typeof result === 'string' && result.includes('No events found');

  return (
    <div
      className={`tool-call-container tool-calendar-search ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🔍📅 Search Calendar Events</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-calendar-search-params">
        {args?.search_text && (
          <div className="tool-search-query">
            🔎 <strong>"{args.search_text}"</strong>
          </div>
        )}

        {(args?.date_range_start || args?.date_range_end) && (
          <div className="tool-search-daterange">
            📅 {args.date_range_start || 'Today'} → {args.date_range_end || '3 months'}
          </div>
        )}

        {args?.max_results && (
          <div className="tool-search-limit">📊 Max results: {args.max_results}</div>
        )}
      </div>

      {result && (
        <div className="tool-calendar-search-results">
          {hasError ? (
            <div className="tool-error-message">{result}</div>
          ) : noResults ? (
            <div className="tool-no-results">No events found matching the search criteria.</div>
          ) : events.length > 0 ? (
            <div className="tool-events-list">
              <div className="tool-results-count">
                Found {events.length} event{events.length !== 1 ? 's' : ''}:
              </div>
              {events.map((event, index) => (
                <div key={index} className="tool-event-item">
                  <div className="tool-event-summary">
                    <strong>{event.summary}</strong>
                  </div>
                  <div className="tool-event-time">
                    🕐 {formatDateTime(event.start)}
                    {event.end && ` → ${formatDateTime(event.end)}`}
                  </div>
                  <div className="tool-event-calendar">
                    📍 {getCalendarName(event.calendar_url)}
                  </div>
                  {event.uid && <div className="tool-event-uid">🔑 {event.uid}</div>}
                </div>
              ))}
            </div>
          ) : (
            <div className="tool-result-text">{result}</div>
          )}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Searching calendar events...</div>
      )}
    </div>
  );
};
// Tool UI for modify_calendar_event
export const ModifyCalendarEventToolUI = ({ args, result, status }) => {
  // Format the time in a human-readable way
  const formatTime = (isoString) => {
    if (!isoString) {
      return 'Unknown time';
    }
    try {
      const date = new Date(isoString);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return isoString; // fallback to raw string if parsing fails
    }
  };

  // Get calendar name from URL
  const getCalendarName = (calendarUrl) => {
    if (!calendarUrl) {
      return 'Unknown calendar';
    }
    try {
      const urlParts = calendarUrl.split('/');
      return urlParts[urlParts.length - 1] || 'Calendar';
    } catch (_e) {
      return 'Calendar';
    }
  };

  // Check if result indicates success or error
  const isSuccess = result && typeof result === 'string' && !result.toLowerCase().includes('error');
  const isError = result && typeof result === 'string' && result.toLowerCase().includes('error');

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div className={`tool-call-container tool-calendar ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">✏️📅 Modify Calendar Event</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-calendar-content">
        {args?.uid && (
          <div className="tool-calendar-uid">
            <strong>Event ID:</strong> {args.uid}
          </div>
        )}

        {args?.calendar_url && (
          <div className="tool-calendar-location">
            📍 Calendar: {getCalendarName(args.calendar_url)}
          </div>
        )}

        {/* Show what changes are being made */}
        <div className="tool-calendar-changes">
          {args?.new_summary && (
            <div className="tool-calendar-change">
              <strong>New Title:</strong> {args.new_summary}
            </div>
          )}

          {args?.new_start_time && (
            <div className="tool-calendar-change">
              <strong>New Start:</strong> 🕐 {formatTime(args.new_start_time)}
            </div>
          )}

          {args?.new_end_time && (
            <div className="tool-calendar-change">
              <strong>New End:</strong> 🕐 {formatTime(args.new_end_time)}
            </div>
          )}

          {args?.new_description !== undefined && (
            <div className="tool-calendar-change">
              <strong>New Description:</strong> {args.new_description || '(removed)'}
            </div>
          )}

          {args?.recurrence_rule !== undefined && (
            <div className="tool-calendar-change">
              <strong>Recurrence:</strong>{' '}
              {args.recurrence_rule ? `🔄 ${args.recurrence_rule}` : '(removed)'}
            </div>
          )}
        </div>
      </div>

      {result && (
        <div
          className={`tool-calendar-result ${isError ? 'tool-error-message' : isSuccess ? 'tool-success-message' : ''}`}
        >
          {typeof result === 'string' ? result : 'Event modified successfully!'}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Modifying calendar event...</div>
      )}
    </div>
  );
};

// Tool UI for delete_calendar_event
export const DeleteCalendarEventToolUI = ({ args, result, status }) => {
  // Get calendar name from URL
  const getCalendarName = (calendarUrl) => {
    if (!calendarUrl) {
      return 'Unknown calendar';
    }
    try {
      const urlParts = calendarUrl.split('/');
      return urlParts[urlParts.length - 1] || 'Calendar';
    } catch (_e) {
      return 'Calendar';
    }
  };

  // Check if result indicates success or error
  const isSuccess = result && typeof result === 'string' && !result.toLowerCase().includes('error');
  const isError = result && typeof result === 'string' && result.toLowerCase().includes('error');

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div className={`tool-call-container tool-calendar ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">🗑️📅 Delete Calendar Event</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-calendar-content">
        {args?.uid && (
          <div className="tool-calendar-uid">
            <strong>Event ID:</strong> {args.uid}
          </div>
        )}

        {args?.calendar_url && (
          <div className="tool-calendar-location">
            📍 Calendar: {getCalendarName(args.calendar_url)}
          </div>
        )}

        <div className="tool-delete-warning">
          ⚠️ This event will be permanently deleted from the calendar.
        </div>
      </div>

      {result && (
        <div
          className={`tool-calendar-result ${isError ? 'tool-error-message' : isSuccess ? 'tool-success-message' : ''}`}
        >
          {typeof result === 'string' ? result : 'Event deleted successfully!'}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Deleting calendar event...</div>
      )}
    </div>
  );
};
// Tool UI for get_message_history
export const GetMessageHistoryToolUI = ({ args, result, status }) => {
  // Parse the result array if it's a string
  let parsedResult = null;
  let parseError = false;

  if (result && typeof result === 'string') {
    try {
      parsedResult = JSON.parse(result);
    } catch (_e) {
      parseError = true;
    }
  } else if (result && Array.isArray(result)) {
    parsedResult = result;
  }

  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  // Helper function to format timestamp
  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Unknown time';
    }
    try {
      const date = new Date(timestamp);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return timestamp; // fallback to raw string if parsing fails
    }
  };

  // Helper function to truncate long content
  const truncateContent = (content, maxLength = 150) => {
    if (!content || content.length <= maxLength) {
      return content;
    }
    return content.substring(0, maxLength) + '...';
  };

  // Helper function to get role icon
  const getRoleIcon = (role) => {
    switch (role?.toLowerCase()) {
      case 'user':
        return '👤';
      case 'assistant':
        return '🤖';
      case 'system':
        return '⚙️';
      default:
        return '💬';
    }
  };

  // Helper function to get interface icon
  const getInterfaceIcon = (interfaceType) => {
    switch (interfaceType?.toLowerCase()) {
      case 'telegram':
        return '📱';
      case 'web':
        return '🌐';
      case 'email':
        return '📧';
      default:
        return '💬';
    }
  };

  const messages = Array.isArray(parsedResult) ? parsedResult : [];

  return (
    <div
      className={`tool-call-container tool-message-history ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">💬 Message History</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-message-history-params">
        {args?.interface_type && (
          <div className="tool-filter-indicator">
            {getInterfaceIcon(args.interface_type)} Interface:{' '}
            <strong>{args.interface_type}</strong>
          </div>
        )}
        {args?.user_name && (
          <div className="tool-filter-indicator">
            👤 User: <strong>{args.user_name}</strong>
          </div>
        )}
        {args?.limit && (
          <div className="tool-filter-indicator">📊 Limit: {args.limit} messages</div>
        )}
      </div>

      {result && !parseError && (
        <div className="tool-message-history-results">
          {messages.length > 0 ? (
            <div className="tool-messages-list">
              <div className="tool-results-count">
                Found {messages.length} message{messages.length !== 1 ? 's' : ''}:
              </div>
              {messages.map((message, index) => (
                <div
                  key={index}
                  className={`tool-message-item tool-message-${message.role?.toLowerCase() || 'unknown'}`}
                >
                  <div className="tool-message-header">
                    <div className="tool-message-role">
                      {getRoleIcon(message.role)} <strong>{message.role || 'Unknown'}</strong>
                    </div>
                    <div className="tool-message-timestamp">
                      🕐 {formatTimestamp(message.timestamp)}
                    </div>
                  </div>

                  {message.content && (
                    <div className="tool-message-content">{truncateContent(message.content)}</div>
                  )}

                  <div className="tool-message-meta">
                    {message.interface_type && (
                      <span className="tool-message-interface">
                        {getInterfaceIcon(message.interface_type)} {message.interface_type}
                      </span>
                    )}
                    {message.user_name && (
                      <span className="tool-message-user">👤 {message.user_name}</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="tool-no-results">No messages found matching the criteria.</div>
          )}
        </div>
      )}

      {result && parseError && (
        <div className="tool-message-history-raw-result">
          <div className="tool-section-label">Result:</div>
          <div className="tool-result-text">{result}</div>
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Loading message history...</div>
      )}
    </div>
  );
};
// Tool UI for send_message_to_user
export const SendMessageToUserToolUI = ({ args, result, status }) => {
  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-send-message ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">💬 Send Message</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-send-message-content">
        <div className="tool-message-details">
          {args?.target_chat_id && (
            <div className="tool-message-recipient">
              📱 To Chat ID: <strong>{args.target_chat_id}</strong>
            </div>
          )}

          {args?.message_content && (
            <div className="tool-message-content">
              <div className="tool-section-label">Message:</div>
              <div className="tool-message-text">
                {args.message_content.length > 200 ? (
                  <>
                    <div>"{args.message_content.substring(0, 200)}..."</div>
                    <div className="tool-content-truncated">
                      Message truncated (showing first 200 characters of{' '}
                      {args.message_content.length} total)
                    </div>
                  </>
                ) : (
                  `"${args.message_content}"`
                )}
              </div>
            </div>
          )}
        </div>

        {result && (
          <div className="tool-send-message-result">
            {status?.type === 'incomplete' ? (
              <div className="tool-error-message">
                <AlertCircleIcon size={16} />
                <span>{result}</span>
              </div>
            ) : (
              <div className="tool-success-message">
                <CheckCircleIcon size={16} />
                <span>{result}</span>
              </div>
            )}
          </div>
        )}
      </div>

      {status?.type === 'running' && <div className="tool-running-message">Sending message...</div>}
    </div>
  );
};
// Tool UI for execute_script
export const ExecuteScriptToolUI = ({ args, result, status }) => {
  // Determine status icon and classes
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-execute-script ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">⚡ Execute Script</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-execute-script-content">
        {args?.script && (
          <div className="tool-script-input">
            <div className="tool-section-label">Starlark Script:</div>
            <CodeHighlight
              code={
                args.script.length > 2000
                  ? `${args.script.substring(0, 2000)}\n\n... Script truncated (showing first 2000 characters of ${args.script.length} total)`
                  : args.script
              }
              language="python"
              showLineNumbers={true}
              customStyle={{
                maxHeight: '400px',
                overflow: 'auto',
              }}
            />
          </div>
        )}

        {args?.globals && Object.keys(args.globals).length > 0 && (
          <div className="tool-script-globals">
            <div className="tool-section-label">Global Variables:</div>
            <pre className="tool-code-block">{JSON.stringify(args.globals, null, 2)}</pre>
          </div>
        )}

        {result && (
          <div className="tool-script-result">
            {status?.type === 'incomplete' ? (
              <div className="tool-error-message">
                <AlertCircleIcon size={16} />
                <div className="tool-section-label">Script Error:</div>
                <pre className="tool-error-text">{result}</pre>
              </div>
            ) : (
              <div className="tool-script-output">
                <div className="tool-section-label">Script Output:</div>
                <div className="tool-result-text">
                  {result.trim() === '' ? (
                    <em className="tool-empty-result">Script executed successfully (no output)</em>
                  ) : (
                    <pre className="tool-script-result-block">{result}</pre>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Executing Starlark script...</div>
      )}
    </div>
  );
};

// Tool UI for create_automation
export const CreateAutomationToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  const automationType = args?.automation_type === 'schedule' ? '🕐' : '🔔';
  const actionType = args?.action?.type === 'wake_llm' ? 'Wake LLM' : 'Script';

  return (
    <div
      className={`tool-call-container tool-automation ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">{automationType} Create Automation</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-automation-content">
        {args?.automation_name && (
          <div className="tool-automation-item">
            <span className="tool-label">Name:</span>
            <span className="tool-value">{args.automation_name}</span>
          </div>
        )}

        {args?.automation_type && (
          <div className="tool-automation-item">
            <span className="tool-label">Type:</span>
            <span className={`tool-badge tool-badge-${args.automation_type}`}>
              {args.automation_type}
            </span>
          </div>
        )}

        {args?.trigger && (
          <div className="tool-automation-trigger">
            <div className="tool-section-label">Trigger Configuration:</div>
            {args.automation_type === 'event' ? (
              <div className="tool-trigger-details">
                {args.trigger?.event_source && (
                  <div className="tool-detail-row">
                    <span className="tool-label">Event Source:</span>
                    <code className="tool-code-inline">{args.trigger.event_source}</code>
                  </div>
                )}
                {args.trigger?.event_filter && (
                  <div className="tool-detail-row">
                    <span className="tool-label">Event Filter:</span>
                    <code className="tool-code-inline">
                      {JSON.stringify(args.trigger.event_filter)}
                    </code>
                  </div>
                )}
              </div>
            ) : (
              <div className="tool-trigger-details">
                {args.trigger?.recurrence_rule && (
                  <div className="tool-detail-row">
                    <span className="tool-label">Schedule:</span>
                    <code className="tool-code-inline" style={{ fontFamily: 'monospace' }}>
                      {args.trigger.recurrence_rule}
                    </code>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {args?.action && (
          <div className="tool-automation-action">
            <div className="tool-section-label">Action:</div>
            <div className="tool-action-details">
              <div className="tool-detail-row">
                <span className="tool-label">Type:</span>
                <span className="tool-badge tool-badge-action">{actionType}</span>
              </div>

              {args.action.type === 'wake_llm' && args.action?.context && (
                <div className="tool-detail-row">
                  <span className="tool-label">Context:</span>
                  <div className="tool-code-block">{args.action.context}</div>
                </div>
              )}

              {args.action.type === 'script' && args.action?.script_code && (
                <div className="tool-script-content">
                  <div className="tool-section-label">Script:</div>
                  <CodeHighlight
                    code={
                      args.action.script_code.length > 2000
                        ? `${args.action.script_code.substring(0, 2000)}\n\n... Script truncated (showing first 2000 characters of ${args.action.script_code.length} total)`
                        : args.action.script_code
                    }
                    language="python"
                    showLineNumbers={true}
                    customStyle={{
                      maxHeight: '400px',
                      overflow: 'auto',
                    }}
                  />
                </div>
              )}
            </div>
          </div>
        )}

        {args?.description && (
          <div className="tool-automation-description">
            <span className="tool-label">Description:</span>
            <span className="tool-value">{args.description}</span>
          </div>
        )}

        {result && (
          <div className="tool-automation-result">
            <div className="tool-section-label">Result:</div>
            {typeof result === 'string' ? (
              <div className="tool-result-text">{result}</div>
            ) : (
              <pre className="tool-code-block">{JSON.stringify(result, null, 2)}</pre>
            )}
          </div>
        )}
      </div>

      {status?.type === 'running' && (
        <div className="tool-running-message">Creating automation...</div>
      )}
    </div>
  );
};

// Tool UI for list_automations
export const ListAutomationsToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  let automations = [];
  try {
    if (typeof result === 'string') {
      automations = JSON.parse(result);
    } else if (Array.isArray(result)) {
      automations = result;
    }
  } catch (_e) {
    // Unable to parse result
  }

  return (
    <div
      className={`tool-call-container tool-automations-list ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">📋 List Automations</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {(args?.automation_type || args?.enabled_only !== undefined) && (
        <div className="tool-automations-filters">
          <div className="tool-section-label">Filters:</div>
          {args?.automation_type && (
            <div className="tool-filter-item">
              <span className="tool-label">Type:</span>
              <span className={`tool-badge tool-badge-${args.automation_type}`}>
                {args.automation_type}
              </span>
            </div>
          )}
          {args?.enabled_only !== undefined && (
            <div className="tool-filter-item">
              <span className="tool-label">Enabled Only:</span>
              <span className="tool-value">{args.enabled_only ? 'Yes' : 'No'}</span>
            </div>
          )}
        </div>
      )}

      {result && (
        <div className="tool-automations-result">
          <div className="tool-section-label">Automations ({automations.length}):</div>
          {automations.length === 0 ? (
            <div className="tool-empty-result">No automations found</div>
          ) : (
            <div className="tool-automations-table">
              {automations.map((automation, index) => (
                <div key={index} className="tool-automation-row">
                  <div className="tool-automation-name">{automation.name}</div>
                  <div className="tool-automation-meta">
                    <span className={`tool-badge tool-badge-${automation.type}`}>
                      {automation.type}
                    </span>
                    <span
                      className={`tool-badge ${
                        automation.enabled ? 'tool-badge-enabled' : 'tool-badge-disabled'
                      }`}
                    >
                      {automation.enabled ? '✓ Enabled' : '✗ Disabled'}
                    </span>
                    <span className="tool-badge tool-badge-action">
                      {automation.action?.type === 'wake_llm' ? 'Wake LLM' : 'Script'}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Fetching automations...</div>
      )}
    </div>
  );
};

// Tool UI for get_automation
export const GetAutomationToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  let automation = null;
  try {
    if (typeof result === 'string') {
      automation = JSON.parse(result);
    } else if (result && typeof result === 'object') {
      automation = result;
    }
  } catch (_e) {
    // Unable to parse result
  }

  const automationType = automation?.type === 'schedule' ? '🕐' : '🔔';

  return (
    <div
      className={`tool-call-container tool-automation-detail ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">{automationType} Get Automation</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {args?.automation_id && (
        <div className="tool-automation-item">
          <span className="tool-label">Automation ID:</span>
          <code className="tool-code-inline">{args.automation_id}</code>
        </div>
      )}

      {automation && (
        <div className="tool-automation-detail-content">
          {automation.name && (
            <div className="tool-automation-item">
              <span className="tool-label">Name:</span>
              <span className="tool-value">{automation.name}</span>
            </div>
          )}

          {automation.type && (
            <div className="tool-automation-item">
              <span className="tool-label">Type:</span>
              <span className={`tool-badge tool-badge-${automation.type}`}>{automation.type}</span>
            </div>
          )}

          {automation.enabled !== undefined && (
            <div className="tool-automation-item">
              <span className="tool-label">Status:</span>
              <span
                className={`tool-badge ${
                  automation.enabled ? 'tool-badge-enabled' : 'tool-badge-disabled'
                }`}
              >
                {automation.enabled ? '✓ Enabled' : '✗ Disabled'}
              </span>
            </div>
          )}

          {automation.trigger && (
            <div className="tool-automation-trigger">
              <div className="tool-section-label">Trigger Configuration:</div>
              {automation.type === 'event' ? (
                <div className="tool-trigger-details">
                  {automation.trigger?.event_source && (
                    <div className="tool-detail-row">
                      <span className="tool-label">Event Source:</span>
                      <code className="tool-code-inline">{automation.trigger.event_source}</code>
                    </div>
                  )}
                  {automation.trigger?.event_filter && (
                    <div className="tool-detail-row">
                      <span className="tool-label">Event Filter:</span>
                      <code className="tool-code-inline">
                        {JSON.stringify(automation.trigger.event_filter)}
                      </code>
                    </div>
                  )}
                </div>
              ) : (
                <div className="tool-trigger-details">
                  {automation.trigger?.recurrence_rule && (
                    <div className="tool-detail-row">
                      <span className="tool-label">Schedule:</span>
                      <code className="tool-code-inline" style={{ fontFamily: 'monospace' }}>
                        {automation.trigger.recurrence_rule}
                      </code>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {automation.action && (
            <div className="tool-automation-action">
              <div className="tool-section-label">Action:</div>
              <div className="tool-action-details">
                {automation.action.type && (
                  <div className="tool-detail-row">
                    <span className="tool-label">Type:</span>
                    <span className="tool-badge tool-badge-action">
                      {automation.action.type === 'wake_llm' ? 'Wake LLM' : 'Script'}
                    </span>
                  </div>
                )}

                {automation.action.type === 'wake_llm' && automation.action?.context && (
                  <div className="tool-detail-row">
                    <span className="tool-label">Context:</span>
                    <div className="tool-code-block">{automation.action.context}</div>
                  </div>
                )}

                {automation.action.type === 'script' && automation.action?.script_code && (
                  <div className="tool-script-content">
                    <div className="tool-section-label">Script:</div>
                    <CodeHighlight
                      code={
                        automation.action.script_code.length > 2000
                          ? `${automation.action.script_code.substring(0, 2000)}\n\n... Script truncated (showing first 2000 characters of ${automation.action.script_code.length} total)`
                          : automation.action.script_code
                      }
                      language="python"
                      showLineNumbers={true}
                      customStyle={{
                        maxHeight: '400px',
                        overflow: 'auto',
                      }}
                    />
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Fetching automation details...</div>
      )}
    </div>
  );
};

// Tool UI for update_automation
export const UpdateAutomationToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-automation-update ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">✏️ Update Automation</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {args?.automation_id && (
        <div className="tool-automation-item">
          <span className="tool-label">Automation ID:</span>
          <code className="tool-code-inline">{args.automation_id}</code>
        </div>
      )}

      {args?.trigger_config && (
        <div className="tool-automation-trigger">
          <div className="tool-section-label">New Trigger Configuration:</div>
          <pre className="tool-code-block">{JSON.stringify(args.trigger_config, null, 2)}</pre>
        </div>
      )}

      {args?.action_config && (
        <div className="tool-automation-action">
          <div className="tool-section-label">New Action Configuration:</div>
          {args.action_config?.script_code ? (
            <div className="tool-script-content">
              <CodeHighlight
                code={
                  args.action_config.script_code.length > 2000
                    ? `${args.action_config.script_code.substring(0, 2000)}\n\n... Script truncated (showing first 2000 characters of ${args.action_config.script_code.length} total)`
                    : args.action_config.script_code
                }
                language="python"
                showLineNumbers={true}
                customStyle={{
                  maxHeight: '400px',
                  overflow: 'auto',
                }}
              />
            </div>
          ) : (
            <pre className="tool-code-block">{JSON.stringify(args.action_config, null, 2)}</pre>
          )}
        </div>
      )}

      {result && (
        <div className="tool-automation-result">
          <div className="tool-section-label">Result:</div>
          {typeof result === 'string' ? (
            <div className="tool-result-text">{result}</div>
          ) : (
            <pre className="tool-code-block">{JSON.stringify(result, null, 2)}</pre>
          )}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Updating automation...</div>
      )}
    </div>
  );
};

// Tool UI for enable_automation
export const EnableAutomationToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-automation-enable ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">✓ Enable Automation</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {args?.automation_id && (
        <div className="tool-automation-item">
          <span className="tool-label">Automation ID:</span>
          <code className="tool-code-inline">{args.automation_id}</code>
        </div>
      )}

      {result && (
        <div className="tool-automation-result">
          <div className="tool-success-message">
            <CheckCircleIcon size={16} />
            <span>{typeof result === 'string' ? result : 'Automation enabled'}</span>
          </div>
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Enabling automation...</div>
      )}
    </div>
  );
};

// Tool UI for disable_automation
export const DisableAutomationToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-automation-disable ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">✕ Disable Automation</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {args?.automation_id && (
        <div className="tool-automation-item">
          <span className="tool-label">Automation ID:</span>
          <code className="tool-code-inline">{args.automation_id}</code>
        </div>
      )}

      {result && (
        <div className="tool-automation-result">
          <div className="tool-result-text">
            {typeof result === 'string' ? result : 'Automation disabled'}
          </div>
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Disabling automation...</div>
      )}
    </div>
  );
};

// Tool UI for delete_automation
export const DeleteAutomationToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  return (
    <div
      className={`tool-call-container tool-automation-delete ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🗑️ Delete Automation</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {args?.automation_id && (
        <div className="tool-automation-item">
          <span className="tool-label">Automation ID:</span>
          <code className="tool-code-inline">{args.automation_id}</code>
        </div>
      )}

      {result && (
        <div className="tool-automation-result">
          {status?.type === 'incomplete' ? (
            <div className="tool-error-message">
              <AlertCircleIcon size={16} />
              <span>{typeof result === 'string' ? result : 'Error deleting automation'}</span>
            </div>
          ) : (
            <div className="tool-warning-message">
              <span>{typeof result === 'string' ? result : 'Automation deleted'}</span>
            </div>
          )}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Deleting automation...</div>
      )}
    </div>
  );
};

// Tool UI for get_automation_stats
export const GetAutomationStatsToolUI = ({ result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  let stats = null;
  try {
    if (typeof result === 'string') {
      stats = JSON.parse(result);
    } else if (result && typeof result === 'object') {
      stats = result;
    }
  } catch (_e) {
    // Unable to parse result
  }

  return (
    <div
      className={`tool-call-container tool-automation-stats ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">📊 Automation Statistics</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {stats && (
        <div className="tool-automation-stats-content">
          {stats.total !== undefined && (
            <div className="tool-stat-item">
              <span className="tool-stat-label">Total Automations:</span>
              <span className="tool-stat-value">{stats.total}</span>
            </div>
          )}

          {stats.enabled !== undefined && (
            <div className="tool-stat-item">
              <span className="tool-stat-label">Enabled:</span>
              <span className="tool-stat-value tool-stat-enabled">{stats.enabled}</span>
            </div>
          )}

          {stats.disabled !== undefined && (
            <div className="tool-stat-item">
              <span className="tool-stat-label">Disabled:</span>
              <span className="tool-stat-value tool-stat-disabled">{stats.disabled}</span>
            </div>
          )}

          {stats.by_type && typeof stats.by_type === 'object' && (
            <div className="tool-stat-section">
              <div className="tool-section-label">By Type:</div>
              <div className="tool-stat-by-type">
                {Object.entries(stats.by_type).map(([type, count]) => (
                  <div key={type} className="tool-stat-type-item">
                    <span className={`tool-badge tool-badge-${type}`}>{type}</span>
                    <span className="tool-stat-count">{count}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {!stats && result && (
        <div className="tool-automation-stats-content">
          <pre className="tool-code-block">{JSON.stringify(result, null, 2)}</pre>
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Fetching automation statistics...</div>
      )}
    </div>
  );
};

// ============================================
// Camera Tool UIs
// ============================================

// Tool UI for list_cameras
export const ListCamerasToolUI = ({ result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  let parsed = null;
  try {
    if (typeof result === 'string') {
      parsed = JSON.parse(result);
    } else if (result && typeof result === 'object') {
      parsed = result;
    }
  } catch (_e) {
    // Unable to parse result
  }

  const cameras = parsed?.cameras || [];
  const hasError = parsed?.error || (status?.type === 'incomplete' && status?.reason === 'error');

  return (
    <div className={`tool-call-container tool-camera ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">📹 List Cameras</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {hasError && (
        <div className="tool-error-message">❌ {parsed?.error || 'Failed to list cameras'}</div>
      )}

      {!hasError && cameras.length > 0 && (
        <div className="tool-camera-list">
          <div className="tool-results-count">
            Found {cameras.length} camera{cameras.length !== 1 ? 's' : ''}:
          </div>
          {cameras.map((camera, index) => (
            <div key={index} className="tool-camera-item">
              <div className="tool-camera-name">
                <span
                  className={`tool-camera-status-dot ${camera.status === 'online' ? 'online' : 'offline'}`}
                />
                <strong>{camera.name || camera.id}</strong>
              </div>
              <div className="tool-camera-details">
                <span className="tool-camera-id">ID: {camera.id}</span>
                <span className={`tool-badge tool-badge-${camera.status}`}>{camera.status}</span>
                <span className="tool-camera-backend">{camera.backend}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {!hasError && cameras.length === 0 && result && (
        <div className="tool-no-results">No cameras configured</div>
      )}

      {status?.type === 'running' && <div className="tool-running-message">Listing cameras...</div>}
    </div>
  );
};

// Tool UI for search_camera_events
export const SearchCameraEventsToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  let parsed = null;
  try {
    if (typeof result === 'string') {
      parsed = JSON.parse(result);
    } else if (result && typeof result === 'object') {
      parsed = result;
    }
  } catch (_e) {
    // Unable to parse result
  }

  const events = parsed?.events || [];
  const hasError = parsed?.error || (status?.type === 'incomplete' && status?.reason === 'error');

  const getEventIcon = (eventType) => {
    switch (eventType?.toLowerCase()) {
      case 'person':
        return '🚶';
      case 'vehicle':
        return '🚗';
      case 'pet':
        return '🐕';
      case 'motion':
        return '💨';
      default:
        return '📷';
    }
  };

  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Unknown time';
    }
    try {
      const date = new Date(timestamp);
      return date.toLocaleString(undefined, {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
    } catch (_e) {
      return timestamp;
    }
  };

  return (
    <div
      className={`tool-call-container tool-camera-events ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🔍 Search Camera Events</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-camera-search-params">
        {args?.camera_id && (
          <div className="tool-filter-indicator">
            📹 Camera: <strong>{args.camera_id}</strong>
          </div>
        )}
        {args?.start_time && (
          <div className="tool-filter-indicator">
            🕐 From: <strong>{args.start_time}</strong>
          </div>
        )}
        {args?.end_time && (
          <div className="tool-filter-indicator">
            🕐 To: <strong>{args.end_time}</strong>
          </div>
        )}
        {args?.event_types && args.event_types.length > 0 && (
          <div className="tool-filter-indicator">
            🏷️ Types: <strong>{args.event_types.join(', ')}</strong>
          </div>
        )}
      </div>

      {hasError && (
        <div className="tool-error-message">❌ {parsed?.error || 'Failed to search events'}</div>
      )}

      {parsed?.warning && <div className="tool-warning-message">⚠️ {parsed.warning}</div>}

      {!hasError && events.length > 0 && (
        <div className="tool-events-list">
          <div className="tool-results-count">
            Found {parsed?.count || events.length} event{events.length !== 1 ? 's' : ''}:
          </div>
          {events.map((event, index) => (
            <div key={index} className="tool-event-item">
              <div className="tool-event-header">
                <span className="tool-event-type">
                  {getEventIcon(event.event_type)} <strong>{event.event_type}</strong>
                </span>
                {event.confidence !== null && (
                  <span className="tool-event-confidence">
                    {Math.round(event.confidence * 100)}% confidence
                  </span>
                )}
              </div>
              <div className="tool-event-timestamp">🕐 {formatTimestamp(event.start_time)}</div>
            </div>
          ))}
        </div>
      )}

      {!hasError && events.length === 0 && result && (
        <div className="tool-no-results">No events found in the specified time range.</div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Searching camera events...</div>
      )}
    </div>
  );
};

// Tool UI for get_camera_frame
export const GetCameraFrameToolUI = ({ args, result, status, attachments }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  let parsed = null;
  try {
    if (typeof result === 'string') {
      parsed = JSON.parse(result);
    } else if (result && typeof result === 'object') {
      parsed = result;
    }
  } catch (_e) {
    // Result might be just text
  }

  const hasError = parsed?.error || (status?.type === 'incomplete' && status?.reason === 'error');

  return (
    <div
      className={`tool-call-container tool-camera-frame ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">📸 Camera Frame</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-camera-frame-params">
        {args?.camera_id && (
          <div className="tool-filter-indicator">
            📹 Camera: <strong>{args.camera_id}</strong>
          </div>
        )}
        {args?.timestamp && (
          <div className="tool-filter-indicator">
            🕐 Timestamp: <strong>{args.timestamp}</strong>
          </div>
        )}
      </div>

      {hasError && (
        <div className="tool-error-message">❌ {parsed?.error || 'Failed to get frame'}</div>
      )}

      {attachments && attachments.length > 0 && (
        <div className="tool-camera-frames">
          {attachments.map((attachment, index) => (
            <ToolAttachmentDisplay
              key={getAttachmentKey(attachment, index)}
              attachment={attachment}
            />
          ))}
        </div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Extracting frame...</div>
      )}
    </div>
  );
};

// Tool UI for get_camera_frames_batch
export const GetCameraFramesBatchToolUI = ({ args, result, status, attachments }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  let parsed = null;
  try {
    if (typeof result === 'string') {
      parsed = JSON.parse(result);
    } else if (result && typeof result === 'object') {
      parsed = result;
    }
  } catch (_e) {
    // Result might be just text
  }

  const hasError = parsed?.error || (status?.type === 'incomplete' && status?.reason === 'error');
  const frameCount = parsed?.count || attachments?.length || 0;

  return (
    <div
      className={`tool-call-container tool-camera-batch ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🎞️ Camera Frames Batch</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-camera-batch-params">
        {args?.camera_id && (
          <div className="tool-filter-indicator">
            📹 Camera: <strong>{args.camera_id}</strong>
          </div>
        )}
        {args?.start_time && (
          <div className="tool-filter-indicator">
            🕐 From: <strong>{args.start_time}</strong>
          </div>
        )}
        {args?.end_time && (
          <div className="tool-filter-indicator">
            🕐 To: <strong>{args.end_time}</strong>
          </div>
        )}
        {args?.interval_minutes && (
          <div className="tool-filter-indicator">
            ⏱️ Interval: <strong>{args.interval_minutes} min</strong>
          </div>
        )}
        {args?.max_frames && (
          <div className="tool-filter-indicator">
            📊 Max frames: <strong>{args.max_frames}</strong>
          </div>
        )}
      </div>

      {hasError && (
        <div className="tool-error-message">❌ {parsed?.error || 'Failed to get frames'}</div>
      )}

      {!hasError && frameCount > 0 && (
        <div className="tool-results-count">
          Retrieved {frameCount} frame{frameCount !== 1 ? 's' : ''}
        </div>
      )}

      {attachments && attachments.length > 0 && (
        <div className="tool-camera-frames-grid">
          {attachments.map((attachment, index) => (
            <ToolAttachmentDisplay
              key={getAttachmentKey(attachment, index)}
              attachment={attachment}
            />
          ))}
        </div>
      )}

      {!hasError && frameCount === 0 && result && (
        <div className="tool-no-results">No frames available in the specified time range.</div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Extracting frames...</div>
      )}
    </div>
  );
};

// Tool UI for get_camera_recordings
export const GetCameraRecordingsToolUI = ({ args, result, status }) => {
  let statusIcon = null;
  let statusClass = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
  } else if (status?.type === 'complete') {
    statusIcon = <CheckCircleIcon size={16} className="tool-success" />;
    statusClass = 'tool-complete';
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
  }

  let parsed = null;
  try {
    if (typeof result === 'string') {
      parsed = JSON.parse(result);
    } else if (result && typeof result === 'object') {
      parsed = result;
    }
  } catch (_e) {
    // Unable to parse result
  }

  const recordings = parsed?.recordings || [];
  const hasError = parsed?.error || (status?.type === 'incomplete' && status?.reason === 'error');

  const formatTimestamp = (timestamp) => {
    if (!timestamp) {
      return 'Unknown time';
    }
    try {
      const date = new Date(timestamp);
      return date.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_e) {
      return timestamp;
    }
  };

  const formatDuration = (seconds) => {
    if (!seconds) {
      return '';
    }
    const mins = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    if (mins === 0) {
      return `${secs}s`;
    }
    return `${mins}m ${secs}s`;
  };

  const formatSize = (bytes) => {
    if (!bytes) {
      return '';
    }
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    if (bytes < 1024 * 1024) {
      return `${(bytes / 1024).toFixed(1)} KB`;
    }
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <div
      className={`tool-call-container tool-camera-recordings ${statusClass}`}
      data-ui="tool-call-content"
    >
      <div className="tool-call-header">
        <span className="tool-name">🎬 Camera Recordings</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      <div className="tool-camera-recordings-params">
        {args?.camera_id && (
          <div className="tool-filter-indicator">
            📹 Camera: <strong>{args.camera_id}</strong>
          </div>
        )}
        {args?.start_time && (
          <div className="tool-filter-indicator">
            🕐 From: <strong>{args.start_time}</strong>
          </div>
        )}
        {args?.end_time && (
          <div className="tool-filter-indicator">
            🕐 To: <strong>{args.end_time}</strong>
          </div>
        )}
      </div>

      {hasError && (
        <div className="tool-error-message">❌ {parsed?.error || 'Failed to get recordings'}</div>
      )}

      {!hasError && recordings.length > 0 && (
        <div className="tool-recordings-list">
          <div className="tool-results-count">
            Found {parsed?.count || recordings.length} recording
            {recordings.length !== 1 ? 's' : ''}
            {parsed?.total_duration_hours !== null &&
              parsed?.total_duration_hours !== undefined && (
                <span className="tool-total-duration">
                  {' '}
                  ({parsed.total_duration_hours} hours total)
                </span>
              )}
          </div>
          {recordings.map((rec, index) => (
            <div key={index} className="tool-recording-item">
              <div className="tool-recording-time">
                📼 {formatTimestamp(rec.start_time)} → {formatTimestamp(rec.end_time)}
              </div>
              <div className="tool-recording-details">
                {rec.duration_seconds && (
                  <span className="tool-recording-duration">
                    ⏱️ {formatDuration(rec.duration_seconds)}
                  </span>
                )}
                {rec.size_bytes && (
                  <span className="tool-recording-size">💾 {formatSize(rec.size_bytes)}</span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {!hasError && recordings.length === 0 && result && (
        <div className="tool-no-results">No recordings found in the specified time range.</div>
      )}

      {status?.type === 'running' && (
        <div className="tool-running-message">Fetching recordings...</div>
      )}
    </div>
  );
};

// Create a map of tool UIs by name for easier access
export const toolUIsByName = {
  // Implemented tool UIs
  add_or_update_note: AddOrUpdateNoteToolUI,
  search_documents: SearchDocumentsToolUI,
  attach_to_response: AttachToResponseTool,

  // Placeholder tool UIs (using ToolFallback)
  get_note: GetNoteToolUI,
  list_notes: ListNotesToolUI,
  delete_note: DeleteNoteToolUI,
  delegate_to_service: DelegateToServiceToolUI,
  schedule_reminder: ScheduleReminderToolUI,
  schedule_future_callback: ScheduleFutureCallbackToolUI,
  schedule_recurring_task: ScheduleRecurringTaskToolUI,
  list_pending_callbacks: ListPendingCallbacksToolUI,
  modify_pending_callback: ModifyPendingCallbackToolUI,
  cancel_pending_callback: CancelPendingCallbackToolUI,
  schedule_action: ScheduleActionToolUI,
  schedule_recurring_action: ScheduleRecurringActionToolUI,
  get_full_document_content: GetFullDocumentContentToolUI,
  ingest_document_from_url: IngestDocumentFromUrlToolUI,
  get_user_documentation_content: GetUserDocumentationContentToolUI,
  query_recent_events: QueryRecentEventsToolUI,
  test_event_listener: TestEventListenerToolUI,
  render_home_assistant_template: RenderHomeAssistantTemplateToolUI,
  add_calendar_event: AddCalendarEventToolUI,
  search_calendar_events: SearchCalendarEventsToolUI,
  modify_calendar_event: ModifyCalendarEventToolUI,
  delete_calendar_event: DeleteCalendarEventToolUI,
  get_message_history: GetMessageHistoryToolUI,
  send_message_to_user: SendMessageToUserToolUI,
  execute_script: ExecuteScriptToolUI,
  create_automation: CreateAutomationToolUI,
  list_automations: ListAutomationsToolUI,
  get_automation: GetAutomationToolUI,
  update_automation: UpdateAutomationToolUI,
  enable_automation: EnableAutomationToolUI,
  disable_automation: DisableAutomationToolUI,
  delete_automation: DeleteAutomationToolUI,
  get_automation_stats: GetAutomationStatsToolUI,

  // Camera tool UIs
  list_cameras: ListCamerasToolUI,
  search_camera_events: SearchCameraEventsToolUI,
  get_camera_frame: GetCameraFrameToolUI,
  get_camera_frames_batch: GetCameraFramesBatchToolUI,
  get_camera_recordings: GetCameraRecordingsToolUI,
};

// Export ToolFallback separately
export { ToolFallback };
