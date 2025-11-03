import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import styles from './MessageDisplay.module.css';
import { getAttachmentKey } from '../../../types/attachments';
import ToolDisplay from '@/components/tools/ToolDisplay';
import { parseToolArguments } from '../../../utils/toolUtils';

const MessageDisplay = ({ message }) => {
  const [expandedToolCalls, setExpandedToolCalls] = useState(new Set());
  const [expandedTraceback, setExpandedTraceback] = useState(false);
  const [expandedDetails, setExpandedDetails] = useState(false);

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
                    <div className={styles.toolCallField}>
                      <ToolDisplay
                        toolName={toolCall.function?.name || toolCall.name || 'Unknown Tool'}
                        args={parseToolArguments(
                          toolCall.function?.arguments || toolCall.arguments
                        )}
                        result={null}
                        status={null}
                        attachments={[]}
                      />
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Display tool result attachments for tool role messages */}
        {message.role === 'tool' && message.attachments && message.attachments.length > 0 && (
          <div className={styles.toolResultAttachments}>
            <h4 className={styles.attachmentsHeader}>Tool Result Attachments:</h4>
            <div className={styles.attachmentsList}>
              {message.attachments.map((attachment, index) => (
                <div key={getAttachmentKey(attachment, index)} className={styles.attachment}>
                  {attachment.type === 'tool_result' &&
                  attachment.mime_type?.startsWith('image/') ? (
                    <div className={styles.imageAttachment}>
                      <img
                        src={attachment.content_url}
                        alt={attachment.description || 'Tool result image'}
                        className={styles.attachmentImage}
                      />
                      <div className={styles.attachmentInfo}>
                        <span className={styles.attachmentName}>
                          {attachment.description || 'Tool result image'}
                        </span>
                      </div>
                    </div>
                  ) : (
                    <div className={styles.fileAttachment}>
                      <div className={styles.attachmentIcon}>🔧</div>
                      <div className={styles.attachmentInfo}>
                        <span className={styles.attachmentName}>
                          {attachment.description || 'Tool result attachment'}
                        </span>
                        {attachment.mime_type && (
                          <span className={styles.attachmentType}>({attachment.mime_type})</span>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {message.attachments && message.attachments.length > 0 && message.role !== 'tool' && (
          <div className={styles.attachments}>
            <h4 className={styles.attachmentsHeader}>Attachments:</h4>
            <div className={styles.attachmentsList}>
              {message.attachments.map((attachment, index) => (
                <div key={getAttachmentKey(attachment, index)} className={styles.attachment}>
                  {attachment.type === 'image' && attachment.content_url ? (
                    <div className={styles.imageAttachment}>
                      <img
                        src={attachment.content_url}
                        alt={attachment.name || 'Attached image'}
                        className={styles.attachmentImage}
                      />
                      <div className={styles.attachmentInfo}>
                        <span className={styles.attachmentName}>
                          {attachment.name || 'image.jpg'}
                        </span>
                        {attachment.size && (
                          <span className={styles.attachmentSize}>
                            ({Math.round(attachment.size / 1024)} KB)
                          </span>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div className={styles.fileAttachment}>
                      <div className={styles.attachmentIcon}>📎</div>
                      <div className={styles.attachmentInfo}>
                        <a
                          href={attachment.content_url || attachment.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className={styles.attachmentLink}
                        >
                          {attachment.name || attachment.filename || 'Unknown file'}
                        </a>
                        {attachment.size && (
                          <span className={styles.attachmentSize}>
                            ({Math.round(attachment.size / 1024)} KB)
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
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

        {/* Message Details Section */}
        {(message.reasoning_info || message.processing_profile_id) && (
          <div className={styles.detailsSection}>
            <div className={styles.detailsHeader}>
              <span className={styles.detailsIcon}>ℹ️</span>
              <span>Message Details</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setExpandedDetails(!expandedDetails)}
                aria-expanded={expandedDetails}
              >
                {expandedDetails ? '▼' : '▶'}
                {expandedDetails ? 'Hide' : 'Show'} Details
              </Button>
            </div>

            {expandedDetails && (
              <div className={styles.detailsContent}>
                {message.processing_profile_id && (
                  <div className={styles.detailField}>
                    <strong>Processing Profile:</strong> {message.processing_profile_id}
                  </div>
                )}

                {message.reasoning_info && (
                  <>
                    {message.reasoning_info.model && (
                      <div className={styles.detailField}>
                        <strong>Model:</strong> {message.reasoning_info.model}
                      </div>
                    )}

                    {(message.reasoning_info.prompt_tokens !== undefined ||
                      message.reasoning_info.completion_tokens !== undefined ||
                      message.reasoning_info.total_tokens !== undefined) && (
                      <div className={styles.detailField}>
                        <strong>Token Usage:</strong>
                        <div className={styles.tokenStats}>
                          {message.reasoning_info.prompt_tokens !== undefined && (
                            <span className={styles.tokenStat}>
                              Prompt: {message.reasoning_info.prompt_tokens.toLocaleString()}
                            </span>
                          )}
                          {message.reasoning_info.completion_tokens !== undefined && (
                            <span className={styles.tokenStat}>
                              Completion:{' '}
                              {message.reasoning_info.completion_tokens.toLocaleString()}
                            </span>
                          )}
                          {message.reasoning_info.total_tokens !== undefined && (
                            <span className={styles.tokenStat}>
                              Total: {message.reasoning_info.total_tokens.toLocaleString()}
                            </span>
                          )}
                        </div>
                      </div>
                    )}

                    {message.reasoning_info.reasoning_tokens !== undefined && (
                      <div className={styles.detailField}>
                        <strong>Reasoning Tokens:</strong>{' '}
                        {message.reasoning_info.reasoning_tokens.toLocaleString()}
                      </div>
                    )}

                    {message.reasoning_info.thinking && (
                      <div className={styles.detailField}>
                        <strong>Thinking Summary:</strong>
                        <pre className={styles.thinkingPre}>{message.reasoning_info.thinking}</pre>
                      </div>
                    )}
                  </>
                )}
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
