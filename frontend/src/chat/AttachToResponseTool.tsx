import React from 'react';
import { CheckCircleIcon, ClockIcon, AlertCircleIcon, FileIcon } from 'lucide-react';
import { Avatar, AvatarImage, AvatarFallback } from '@/components/ui/avatar';

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

      {/* Render attachments using a custom attachment display */}
      <div className="flex w-full flex-row gap-3 mt-3" data-testid="assistant-attachments">
        {attachments.map((attachment, index) => (
          <div
            key={attachment.id || `attachment-${index}`}
            className="flex h-12 w-40 items-center justify-center gap-2 rounded-lg border p-1"
            data-testid="attachment-preview"
          >
            <Avatar className="bg-muted flex size-10 items-center justify-center rounded border text-sm">
              <AvatarFallback>
                <FileIcon />
              </AvatarFallback>
              {attachment.mime_type.startsWith('image/') && <AvatarImage src={attachment.url} />}
            </Avatar>
            <div className="flex-grow basis-0">
              <p className="text-muted-foreground line-clamp-1 text-ellipsis break-all text-xs font-bold">
                {attachment.name}
              </p>
              <p className="text-muted-foreground text-xs">
                {attachment.mime_type.startsWith('image/') ? 'Image' : 'File'}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
