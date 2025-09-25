import React from 'react';
import { CheckCircleIcon, ClockIcon, AlertCircleIcon, FileIcon } from 'lucide-react';

interface AttachToResponseToolProps {
  toolName: string;
  args?: Record<string, unknown>; // Keep for interface compatibility but won't use
  result?: string | Record<string, unknown>;
  status: { type: string };
  attachments?: Array<Record<string, unknown>>;
}

/**
 * Custom tool UI component for attach_to_response that renders attachments
 * as the primary visual representation instead of showing raw tool call data.
 */
export const AttachToResponseTool: React.FC<AttachToResponseToolProps> = ({
  args: _args,
  result,
  status,
  attachments: directAttachments,
}) => {
  // Extract attachments from multiple possible sources
  const extractAttachments = (): Attachment[] => {
    let attachmentData: unknown[] = [];

    // Try to get attachments from direct attachments prop first
    if (Array.isArray(directAttachments)) {
      attachmentData = directAttachments;
    }
    // Try to get from result.attachments if result is an object
    else if (result && typeof result === 'object' && 'attachments' in result) {
      const resultAttachments = (result as any).attachments;
      if (Array.isArray(resultAttachments)) {
        attachmentData = resultAttachments;
      }
    }
    // Try to parse result as JSON if it's a string
    else if (typeof result === 'string') {
      try {
        const parsedResult = JSON.parse(result);
        if (parsedResult && typeof parsedResult === 'object' && 'attachments' in parsedResult) {
          const resultAttachments = parsedResult.attachments;
          if (Array.isArray(resultAttachments)) {
            attachmentData = resultAttachments;
          }
        }
      } catch {
        // Result is not valid JSON, continue with empty attachments
      }
    }

    // Filter and validate attachments
    const validAttachments: Array<{
      id: string;
      name: string;
      url: string;
      mime_type: string;
      size: number;
    }> = [];

    attachmentData.forEach((item, index) => {
      // Try to convert attachment metadata to valid attachment format
      if (item && typeof item === 'object') {
        const itemObj = item as Record<string, unknown>;
        const converted = {
          id: (itemObj.attachment_id as string) || `attachment-${index}`,
          name: (itemObj.description as string) || 'Attachment',
          url: (itemObj.content_url as string) || (itemObj.url as string) || '',
          mime_type: (itemObj.mime_type as string) || 'application/octet-stream',
          size: (itemObj.size as number) || 0,
        };

        if (converted.url) {
          validAttachments.push(converted);
        }
      }
    });

    return validAttachments;
  };

  const attachments = extractAttachments();

  // Determine the status icon and styling
  let statusIcon = null;
  let statusClass = '';
  let statusText = '';

  if (status?.type === 'running') {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
    statusText = 'Preparing attachments...';
  } else if (status?.type === 'complete' && attachments.length > 0) {
    statusIcon = <CheckCircleIcon size={16} />;
    statusClass = 'tool-complete';
    statusText = `${attachments.length} attachment${attachments.length !== 1 ? 's' : ''} ready`;
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
    statusText = 'Failed to process attachments';
  }

  // If we're still running or there are no attachments, show a minimal status
  if (status?.type === 'running' || attachments.length === 0) {
    return (
      <div className={`tool-call-container ${statusClass}`} data-ui="tool-call-content">
        <div className="tool-call-header">
          <span className="tool-name">📎 Attachments</span>
          {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
        </div>
        {statusText && (
          <div className="tool-call-result" data-testid="tool-result">
            <div className="tool-result-text">{statusText}</div>
          </div>
        )}
      </div>
    );
  }

  // Main rendering when attachments are available
  return (
    <div className={`tool-call-container ${statusClass}`} data-ui="tool-call-content">
      <div className="tool-call-header">
        <span className="tool-name">📎 Attachments</span>
        {statusIcon && <span className="tool-status-icon">{statusIcon}</span>}
      </div>

      {statusText && (
        <div className="tool-call-result" data-testid="tool-result">
          <div className="tool-result-text">{statusText}</div>
        </div>
      )}

      {/* Render attachments with enhanced preview and download functionality */}
      <div className="flex w-full flex-col gap-3 mt-3" data-testid="assistant-attachments">
        {attachments.map((attachment, index) => {
          const isImage = attachment.mime_type.startsWith('image/');
          return (
            <div
              key={attachment.id || `attachment-${index}`}
              className="rounded-lg border bg-muted/20 overflow-hidden"
              data-testid="attachment-preview"
            >
              {/* Attachment header with name and download link */}
              <div className="flex items-center justify-between p-3 bg-muted/50 border-b">
                <div className="flex items-center gap-2 flex-1 min-w-0">
                  <FileIcon size={16} className="text-muted-foreground shrink-0" />
                  <span className="text-sm font-medium text-foreground truncate">
                    {attachment.name}
                  </span>
                  <span className="text-xs text-muted-foreground shrink-0">
                    ({isImage ? 'Image' : 'File'})
                  </span>
                </div>
                <a
                  href={attachment.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-primary hover:text-primary/80 underline shrink-0 ml-2"
                  download
                >
                  Download
                </a>
              </div>

              {/* Image preview for image attachments */}
              {isImage && (
                <div className="p-3">
                  <a
                    href={attachment.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block"
                  >
                    <img
                      src={attachment.url}
                      alt={attachment.name}
                      className="max-w-full max-h-48 rounded border hover:opacity-90 transition-opacity cursor-pointer object-contain"
                      style={{ display: 'block', margin: '0 auto' }}
                      onError={(e) => {
                        // Hide image if it fails to load
                        e.currentTarget.style.display = 'none';
                      }}
                    />
                  </a>
                </div>
              )}

              {/* File info for non-image attachments */}
              {!isImage && (
                <div className="p-3 text-center">
                  <div className="inline-flex items-center justify-center w-16 h-16 bg-muted rounded-lg mb-2">
                    <FileIcon size={24} className="text-muted-foreground" />
                  </div>
                  <p className="text-xs text-muted-foreground">{attachment.mime_type}</p>
                  {attachment.size > 0 && (
                    <p className="text-xs text-muted-foreground mt-1">
                      {(attachment.size / 1024).toFixed(1)} KB
                    </p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
