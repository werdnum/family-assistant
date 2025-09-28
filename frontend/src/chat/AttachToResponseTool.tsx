import React, { useState, useEffect } from 'react';
import { CheckCircleIcon, ClockIcon, AlertCircleIcon, FileIcon } from 'lucide-react';

interface AttachToResponseToolProps {
  toolName: string;
  args?: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  status: { type: string };
  attachments?: Array<Record<string, unknown>>;
}

interface Attachment {
  id: string;
  name: string;
  url: string;
  mime_type: string;
  size: number;
}

/**
 * Custom tool UI component for attach_to_response that renders attachments
 * as the primary visual representation instead of showing raw tool call data.
 */
export const AttachToResponseTool: React.FC<AttachToResponseToolProps> = ({
  args,
  result,
  status,
  attachments: directAttachments,
}) => {
  const [fetchedAttachments, setFetchedAttachments] = useState<Attachment[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  // Component lifecycle logging
  useEffect(() => {
    console.log(
      `[ATTACH-COMP] MOUNT | hasArgs=${!!args} | hasResult=${!!result} | hasDirectAttachments=${!!directAttachments} | statusType=${status?.type} | ts=${Date.now()}`
    );

    return () => {
      console.log(`[ATTACH-COMP] UNMOUNT | ts=${Date.now()}`);
    };
  }, []);

  // Props change logging
  useEffect(() => {
    console.log(
      `[ATTACH-COMP] Props changed | statusType=${status?.type} | hasResult=${!!result} | directAttachmentsCount=${directAttachments?.length || 0} | ts=${Date.now()}`
    );
  }, [status, result, directAttachments]);

  // Extract attachment IDs from args for cases where result doesn't have enriched data
  const getAttachmentIdsFromArgs = (): string[] => {
    if (!args) {
      return [];
    }

    try {
      // Handle both parsed JSON args and string args
      let parsedArgs = args;
      if (typeof args === 'string') {
        parsedArgs = JSON.parse(args);
      }

      const attachmentIds = (parsedArgs as Record<string, unknown>).attachment_ids;
      return Array.isArray(attachmentIds) ? attachmentIds : [];
    } catch {
      return [];
    }
  };

  // Fetch attachment data from API when we only have IDs
  useEffect(() => {
    const attachmentIds = getAttachmentIdsFromArgs();

    // Check if we already have enriched attachment data
    const hasEnrichedData = Array.isArray(directAttachments) && directAttachments.length > 0;
    const hasResultAttachments = result && typeof result === 'object' && 'attachments' in result;

    console.log(
      `[ATTACH-COMP] Effect running | attachmentIdCount=${attachmentIds.length} | hasEnrichedData=${hasEnrichedData} | hasResultAttachments=${hasResultAttachments} | ts=${Date.now()}`
    );

    if (attachmentIds.length > 0 && !hasEnrichedData && !hasResultAttachments) {
      console.log(
        `[ATTACH-COMP] Starting fallback fetch | attachmentIdCount=${attachmentIds.length} | ts=${Date.now()}`
      );
      // Log for debugging - this fallback should not be needed if data flow works correctly
      console.warn('AttachToResponseTool: Using fallback attachment metadata fetching');

      setIsLoading(true);

      // Fetch attachment info from API
      Promise.all(
        attachmentIds.map(async (attachmentId) => {
          try {
            // Use GET request to get metadata (HEAD not supported by attachment API)
            const response = await fetch(`/api/attachments/${attachmentId}`, {
              method: 'GET',
            });

            if (!response.ok) {
              throw new Error(
                `HTTP ${response.status}: Failed to fetch attachment for ${attachmentId}`
              );
            }

            return {
              id: attachmentId,
              name:
                response.headers.get('content-disposition')?.match(/filename="(.+)"/)?.[1] ||
                'Attachment',
              url: `/api/attachments/${attachmentId}`,
              mime_type: response.headers.get('content-type') || 'application/octet-stream',
              size: parseInt(response.headers.get('content-length') || '0', 10),
            };
          } catch (error) {
            console.warn(`Failed to fetch attachment ${attachmentId}:`, error);
            return {
              id: attachmentId,
              name: 'Attachment (failed to load)',
              url: `/api/attachments/${attachmentId}`,
              mime_type: 'application/octet-stream',
              size: 0,
            };
          }
        })
      )
        .then((attachments) => {
          console.log(
            `[ATTACH-COMP] Fallback fetch complete | fetchedCount=${attachments.length} | ts=${Date.now()}`
          );
          setFetchedAttachments(attachments);
          setIsLoading(false);
        })
        .catch((error) => {
          console.log(
            `[ATTACH-COMP] Fallback fetch failed | error=${error.message} | ts=${Date.now()}`
          );
          console.warn('Failed to fetch attachments:', error);
          setIsLoading(false);
        });
    }
  }, [args, result, directAttachments]);

  // Extract attachments from multiple possible sources
  const extractAttachments = (): Attachment[] => {
    console.log(
      `[ATTACH-COMP] extractAttachments called | fetchedCount=${fetchedAttachments.length} | ts=${Date.now()}`
    );

    // If we have fetched attachments, use those first
    if (fetchedAttachments.length > 0) {
      return fetchedAttachments;
    }

    let attachmentData: unknown[] = [];

    // Try to get attachments from direct attachments prop first
    if (Array.isArray(directAttachments)) {
      attachmentData = directAttachments;
    }
    // Try to get from result.attachments if result is an object
    else if (result && typeof result === 'object' && 'attachments' in result) {
      const resultAttachments = (result as Record<string, unknown>).attachments;
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

    const result = validAttachments;
    console.log(
      `[ATTACH-COMP] extractAttachments result | count=${result.length} | ts=${Date.now()}`
    );
    return result;
  };

  const attachments = extractAttachments();

  // Determine the status icon and styling
  let statusIcon = null;
  let statusClass = '';
  let statusText = '';

  if (status?.type === 'running' || isLoading) {
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
    statusText = isLoading ? 'Loading attachments...' : 'Preparing attachments...';
  } else if (status?.type === 'complete' && attachments.length > 0) {
    statusIcon = <CheckCircleIcon size={16} />;
    statusClass = 'tool-complete';
    statusText = `${attachments.length} attachment${attachments.length !== 1 ? 's' : ''} ready`;
  } else if (status?.type === 'incomplete' && status?.reason === 'error') {
    statusIcon = <AlertCircleIcon size={16} />;
    statusClass = 'tool-error';
    statusText = 'Failed to process attachments';
  } else {
    // Default case for undefined status or unrecognized status types
    // This ensures statusText is always set so tool-result div always renders
    statusIcon = <ClockIcon size={16} className="animate-spin" />;
    statusClass = 'tool-running';
    statusText = 'Initializing...';
  }

  // Render decision logging
  console.log(
    `[ATTACH-COMP] Render decision | statusType=${status?.type} | attachmentCount=${attachments.length} | isLoading=${isLoading} | willRenderMinimal=${status?.type === 'running' || attachments.length === 0} | ts=${Date.now()}`
  );

  // If we're still running or there are no attachments, show a minimal status
  if (status?.type === 'running' || attachments.length === 0) {
    console.log(
      `[ATTACH-COMP] Rendering minimal UI | statusText="${statusText}" | ts=${Date.now()}`
    );
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

  console.log(
    `[ATTACH-COMP] Rendering full UI | attachmentCount=${attachments.length} | ts=${Date.now()}`
  );
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
