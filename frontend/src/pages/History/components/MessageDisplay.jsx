import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import styles from './MessageDisplay.module.css';

const MessageDisplay = ({ message }) => {
  const [expandedToolCalls, setExpandedToolCalls] = useState(new Set());
  const [expandedTraceback, setExpandedTraceback] = useState(false);

  const toggleToolCall = (index) => {
    const newExpanded = new Set(expandedToolCalls);
    if (newExpanded.has(index)) {
      newExpanded.delete(index);
    } else {
      newExpanded.add(index);
    }
    setExpandedToolCalls(newExpanded);
  };

  const formatTimestamp = (timestamp) => {
    const date = new Date(timestamp);
    return date.toLocaleString();
  };

  const getRoleIcon = (role) => {
    switch (role) {
      case 'user':
        return '👤';
      case 'assistant':
        return '🤖';
      case 'tool':
        return '🔧';
      case 'system':
        return '⚙️';
      default:
        return '💬';
    }
  };

  const getRoleLabel = (role) => {
    switch (role) {
      case 'user':
        return 'User';
      case 'assistant':
        return 'Assistant';
      case 'tool':
        return 'Tool Result';
      case 'system':
        return 'System';
      default:
        return role.charAt(0).toUpperCase() + role.slice(1);
    }
  };

  const formatJson = (obj) => {
    try {
      return JSON.stringify(obj, null, 2);
    } catch {
      return String(obj);
    }
  };

  return (
    <div className={`${styles.message} ${styles[`message--${message.role}`]}`}>
      <div className={styles.messageHeader}>
        <div className={styles.messageRole}>
          <span className={styles.roleIcon}>{getRoleIcon(message.role)}</span>
          <span className={styles.roleLabel}>{getRoleLabel(message.role)}</span>
          {message.tool_call_id && (
            <span className={styles.toolCallId}>(Tool Call: {message.tool_call_id})</span>
          )}
        </div>
        <div className={styles.messageTimestamp}>{formatTimestamp(message.timestamp)}</div>
      </div>

      <div className={styles.messageContent}>
        {message.content && (
          <div className={styles.messageText}>
            <pre className={styles.contentPre}>{message.content}</pre>
          </div>
        )}

        {message.tool_calls && message.tool_calls.length > 0 && (
          <div className={styles.toolCalls}>
            <h4 className={styles.toolCallsHeader}>Tool Calls:</h4>
            {message.tool_calls.map((toolCall, index) => (
              <div key={toolCall.id || index} className={styles.toolCall}>
                <div className={styles.toolCallHeader}>
                  <span className={styles.toolCallName}>
                    {toolCall.function?.name || toolCall.name || 'Unknown Tool'}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => toggleToolCall(index)}
                    aria-expanded={expandedToolCalls.has(index)}
                  >
                    {expandedToolCalls.has(index) ? '▼' : '▶'}
                    {expandedToolCalls.has(index) ? 'Hide' : 'Show'} Details
                  </Button>
                </div>

                {expandedToolCalls.has(index) && (
                  <div className={styles.toolCallDetails}>
                    {toolCall.id && (
                      <div className={styles.toolCallField}>
                        <strong>ID:</strong> {toolCall.id}
                      </div>
                    )}
                    {toolCall.type && (
                      <div className={styles.toolCallField}>
                        <strong>Type:</strong> {toolCall.type}
                      </div>
                    )}
                    {(toolCall.function?.arguments || toolCall.arguments) && (
                      <div className={styles.toolCallField}>
                        <strong>Arguments:</strong>
                        <pre className={styles.jsonPre}>
                          {typeof (toolCall.function?.arguments || toolCall.arguments) === 'string'
                            ? toolCall.function?.arguments || toolCall.arguments
                            : formatJson(toolCall.function?.arguments || toolCall.arguments)}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {message.error_traceback && (
          <div className={styles.errorSection}>
            <div className={styles.errorHeader}>
              <span className={styles.errorIcon}>⚠️</span>
              <span>Error Occurred</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setExpandedTraceback(!expandedTraceback)}
                aria-expanded={expandedTraceback}
              >
                {expandedTraceback ? '▼' : '▶'}
                {expandedTraceback ? 'Hide' : 'Show'} Traceback
              </Button>
            </div>

            {expandedTraceback && (
              <div className={styles.errorTraceback}>
                <pre className={styles.tracebackPre}>{message.error_traceback}</pre>
              </div>
            )}
          </div>
        )}
      </div>

      <div className={styles.messageFooter}>
        <span className={styles.messageId}>ID: {message.internal_id}</span>
      </div>
    </div>
  );
};

export default MessageDisplay;
